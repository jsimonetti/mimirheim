"""PV device — fixed forecast or solver-controlled generation.

In fixed mode (no capabilities enabled) mimirheim does not control PV output: the
per-step power forecast is treated as a given constant, and dispatchable devices
schedule around it.

When ``capabilities.power_limit`` is True, mimirheim adds a continuous decision
variable ``pv_kw[t]`` bounded by the forecast, allowing the solver to curtail
generation when exporting would be costly.

When ``capabilities.on_off`` is True, mimirheim adds a binary variable
``pv_curtailed[t]``: 0 means the array is running (produces the full forecast),
1 means it is switched off. Curtailment is penalised with a negligible objective
term (1e-6 EUR per step) so the solver defaults to on when indifferent.

Both capabilities may be enabled together: ``pv_kw[t]`` is bounded by
``forecast[t] * (1 - pv_curtailed[t])``.

When ``production_stages`` is provided in config, mimirheim adds one binary variable
``stage_active[t, s]`` per step per stage. Exactly one stage is active at each
step, and the effective output is the precomputed ``min(forecast[t], stage_kw[s])``.
This mode is mutually exclusive with ``power_limit`` and ``on_off``.

``PvInputs`` is defined here because it is a direct input to this device's
``add_constraints`` method. It is not a runtime MQTT model with staleness checks
— the forecast list arrives as a single decoded payload each solve cycle.

This module does not import from ``mimirheim.io``. It does not import ``python-mip``.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from mimirheim.config.schema import PvConfig
from mimirheim.core.context import ModelContext


class PvInputs(BaseModel):
    """Runtime PV forecast delivered to the device each solve cycle.

    Attributes:
        forecast_kw: Per-step PV generation forecast in kW. Must contain at
            least one value. Negative values (caused by sensor noise or
            calibration drift) are silently clipped to zero in ``net_power``.
    """

    model_config = ConfigDict(extra="forbid")

    forecast_kw: list[float] = Field(min_length=1, description="Per-step PV forecast in kW.")


class PvDevice:
    """Models a PV array as a fixed or solver-controlled generation source.

    In fixed mode (no capabilities), PV output is not a decision variable. All
    forecast generation enters the power balance as a constant, and other devices
    must absorb or export any surplus.

    When ``power_limit`` is enabled, ``pv_kw[t]`` is a continuous variable
    bounded by the forecast; the solver may curtail below the forecast value.

    When ``on_off`` is enabled, ``pv_on[t]`` is a binary variable; the array
    either produces the full forecast or nothing.

    Both modes may be active simultaneously: ``pv_kw[t] <= forecast[t] * pv_on[t]``.

    Attributes:
        name: Device name matching the key in ``config.pv``. Used by the power
            balance assembler to identify this source.
        config: Static PV configuration.
    """

    def __init__(self, name: str, config: PvConfig) -> None:
        """Initialise the PV device.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.pv``.
            config: Validated static PV configuration.
        """
        self.name = name
        self.config = config
        self._forecast: list[float] = []
        # Keyed by time step t. In fixed mode these dicts remain empty and
        # _net_power holds plain floats. In variable modes they hold solver
        # variable handles returned by ctx.solver.add_var.
        self._net_power: dict[int, Any] = {}
        self._pv_curtailed: dict[int, Any] = {}
        self._pv_kw: dict[int, Any] = {}
        # Keyed by (t, stage_index). Populated only in staged mode.
        # stage_active[(t, s)] is 1 when stage s is selected at step t, else 0.
        self._stage_active: dict[tuple[int, int], Any] = {}
        # stage_kw[s] holds the kW value for stage s, for use in chosen_stage_kw.
        self._stage_kw: list[float] = []
        # Solver context retained from add_constraints so that chosen_stage_kw
        # can read variable values without requiring the caller to pass ctx again.
        self._ctx: ModelContext | None = None

    def add_variables(self, ctx: ModelContext) -> None:
        """No-op — PV variables are created in add_constraints where the forecast is available.

        The calling convention in model_builder requires add_variables(ctx) to
        accept only the context. Because the forecast is not yet known at that
        point, variables are deferred to add_constraints.

        Args:
            ctx: The current solve context (unused).
        """

    def add_constraints(self, ctx: ModelContext, inputs: PvInputs) -> None:
        """Store the forecast and create any required solver variables.

        In fixed mode (no capabilities) no variables or constraints are added.
        The forecast is clipped to zero and stored as plain floats.

        In power_limit mode, a continuous variable ``pv_kw[t]`` is added for
        each time step with upper bound equal to the (clipped) forecast. The
        solver may choose any value in ``[0, forecast[t]]``.

        In on_off mode, a binary variable ``pv_on[t]`` is added for each step.
        Production is ``forecast[t] * pv_on[t]``: either the full forecast or
        zero, with no intermediate values.

        When both modes are active, both variables are created and the Big-M
        constraint ``pv_kw[t] <= forecast[t] * pv_on[t]`` couples them.
        ``forecast[t]`` plays the role of Big-M because it is the tightest
        valid upper bound: ``pv_kw[t]`` cannot exceed the forecast regardless
        of ``pv_on``.

        Args:
            ctx: The current solve context.
            inputs: The per-step PV forecast for this solve cycle.
        """
        self._forecast = inputs.forecast_kw
        caps = self.config.capabilities
        stages = self.config.production_stages
        # Retain the context so chosen_stage_kw can evaluate variable values
        # without requiring a ctx argument at call sites.
        self._ctx = ctx

        for t in ctx.T:
            # Clip forecast to zero. Negative values arise from sensor noise or
            # calibration drift and must not pull the power balance negative.
            f = max(0.0, inputs.forecast_kw[t])

            if stages is not None:
                # Staged mode. The inverter only accepts the specific kW values
                # listed in production_stages. The solver picks exactly one stage
                # per step using binary variables.
                #
                # For each stage s with registered level stage_kw[s]:
                #   stage_active[t, s] ∈ {0, 1}
                #
                # Exactly-one constraint: Σ_s stage_active[t, s] = 1
                # This replaces an SOS1 set; an explicit equality constraint is
                # simpler to express and equally effective for small stage counts.
                #
                # Effective output at step t:
                #   pv_kw[t] = Σ_s min(f, stage_kw[s]) * stage_active[t, s]
                #
                # The min() is a scalar precomputed in Python. It ensures that
                # if the inverter is set to stage 3.0 kW but the forecast is
                # only 2.2 kW, the actual AC output entering the power balance
                # is 2.2, not 3.0. No nonlinear terms are introduced.
                if not self._stage_kw:
                    # Populate stage_kw once (same values for every step).
                    self._stage_kw = list(stages)

                stage_vars = []
                for s, stage_kw_val in enumerate(stages):
                    effective_kw = min(f, stage_kw_val)
                    var = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
                    self._stage_active[(t, s)] = var
                    stage_vars.append((var, effective_kw))

                # Exactly one stage active per step.
                ctx.solver.add_constraint(sum(v for v, _ in stage_vars) == 1)

                # net_power is a linear combination of binary vars with scalar
                # coefficients — a valid linear expression for CBC.
                self._net_power[t] = sum(eff * v for v, eff in stage_vars)

            elif caps.on_off:
                # Binary curtailment flag. pv_curtailed[t] = 0 means the array
                # is running (produces the full forecast); pv_curtailed[t] = 1
                # means the inverter is switched off.
                #
                # net_power[t] = f * (1 - pv_curtailed[t])
                #
                # Modelling curtailment rather than "on" has a key advantage:
                # when the forecast is negligible the variable is effectively
                # free (no effect on the objective or power balance). The solver
                # will assign it to its lower bound (0), which means "not
                # curtailed" — the correct default. A pv_on variable would
                # default to 0 in the same situation, which means "off",
                # producing a spurious off command to the inverter.
                pv_curtailed = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
                self._pv_curtailed[t] = pv_curtailed
                self._net_power[t] = f * (1 - pv_curtailed)

            elif caps.power_limit:
                # Continuous curtailment only. The solver may produce anywhere
                # in [0, forecast[t]].
                pv_kw = ctx.solver.add_var(lb=0.0, ub=f)
                self._pv_kw[t] = pv_kw
                self._net_power[t] = pv_kw

            else:
                # Fixed mode. No solver variables. Use the clipped forecast
                # directly as a constant in the power balance.
                self._net_power[t] = f

    def net_power(self, t: int) -> Any:
        """Return the PV generation at step ``t``.

        In fixed mode returns a ``float`` (the clipped forecast). In variable
        modes returns a solver variable handle or linear expression that the
        solver can incorporate into constraints and objectives.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            PV generation in kW at step ``t``: a ``float`` in fixed mode, or a
            CBC variable / linear expression in variable modes.
        """
        return self._net_power[t]

    def objective_terms(self, t: int) -> Any:
        """Return a negligible curtailment penalty to break solver ties.

        When ``capabilities.on_off`` is enabled, this returns
        ``1e-6 * pv_curtailed[t]``. This tiny weight gives the solver
        a reason to prefer ``pv_curtailed=0`` (array running) whenever
        the binary variable is otherwise free — most notably when the
        forecast is zero and curtailment has no effect on the power balance
        or the real cost objective.

        The weight (1e-6 EUR per kW-step) is five to six orders of magnitude
        smaller than any real electricity price term and cannot influence
        economically meaningful decisions.

        When on_off is not enabled, returns 0.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            A solver linear expression or 0.
        """
        if t in self._pv_curtailed:
            return 1e-6 * self._pv_curtailed[t]
        return 0

    def is_on(self, t: int) -> bool:
        """Return True if the array should be switched on at step ``t``.

        Reads the ``pv_curtailed[t]`` binary variable set by the solver.
        ``pv_curtailed[t] = 0`` means the array is running (on); ``1`` means
        it has been switched off.

        When the forecast is negligible the variable is free and defaults to
        its lower bound (0 = not curtailed), so no spurious off command is
        ever sent for low-production steps.

        Must only be called after the solver has run and only when
        ``capabilities.on_off`` is True.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            True when the array is running, False when the solver curtailed it.

        Raises:
            RuntimeError: If called before ``add_constraints`` has run.
        """
        if self._ctx is None or t not in self._pv_curtailed:
            raise RuntimeError(
                f"is_on called on PvDevice '{self.name}' before add_constraints "
                "or when capabilities.on_off is False."
            )
        return round(self._ctx.solver.var_value(self._pv_curtailed[t])) == 0

    def chosen_stage_kw(self, t: int) -> float:
        """Return the kW register value of the stage selected at step ``t``.

        This is the value the solver instructs the inverter register to hold,
        not the effective AC output. When the forecast is below the stage's
        rated power, ``chosen_stage_kw(t) >= net_power(t)`` (after solving).

        Must only be called after the solver has run and only when
        ``config.production_stages`` is not None.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            The kW level of the selected stage, in kW.

        Raises:
            RuntimeError: If called when staged mode was not configured or the
                device's constraints have not yet been added.
        """
        if not self._stage_kw or self._ctx is None:
            raise RuntimeError(
                f"chosen_stage_kw called on PvDevice '{self.name}' before "
                "add_constraints or when production_stages is None."
            )
        for s, stage_kw_val in enumerate(self._stage_kw):
            var = self._stage_active[(t, s)]
            if round(self._ctx.solver.var_value(var)) == 1:
                return stage_kw_val
        # Fallback: return stage 0 (off). Reached only if no variable rounded to 1,
        # which indicates a solver issue (e.g. fractional binary due to gap tolerance).
        return self._stage_kw[0]
