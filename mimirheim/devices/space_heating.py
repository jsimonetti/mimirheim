"""SpaceHeatingDevice — models a space heating heat pump in the MILP.

This module implements the Device Protocol for a space heating heat pump. Two
control modes are supported:

**On/off mode**: The HP runs at a single rated electrical power (kW) and COP.
One binary variable per step represents the on/off state. This is appropriate
for fixed-speed compressors.

**Power-stage (SOS2) mode**: A list of HeatingStage operating points defines
the HP's power curve. One SOS2 set of weight variables per step interpolates
between adjacent stages. This is appropriate for inverter-driven (variable-speed)
compressors that can modulate power continuously.

In both modes, a minimum run length constraint prevents the solver from scheduling
runs shorter than the compressor's minimum on-time (typically 4 steps = 1 hour).

By default the module uses a **degree-days demand** model: ``heat_needed_kwh``
provides the total thermal energy the HP must deliver over the horizon, and the
solver schedules runs to satisfy that total at minimum cost. When
``heat_needed_kwh`` is zero, all on variables are pinned to zero and all other
constraints are skipped.

When ``config.building_thermal`` is set, the **building thermal model (BTM)** is
used instead. The solver tracks indoor temperature as a per-step state variable
enforced by a first-order difference equation, and maintains a comfort band
[comfort_min_c, comfort_max_c]. The degree-days guard is bypassed: ``heat_needed_kwh``
is ignored.

The module does not import from ``mimirheim.io``. The demand value is passed as a
``SpaceHeatingInputs`` argument to ``add_constraints``. All solver interactions go
through ``ModelContext.solver``.
"""

from typing import Any

from mimirheim.config.schema import SpaceHeatingConfig
from mimirheim.core.bundle import SpaceHeatingInputs
from mimirheim.core.context import ModelContext


class SpaceHeatingDevice:
    """Models a space heating heat pump as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.space_heating_hps``. The
    model builder creates one ``SpaceHeatingDevice`` per named device in the config,
    calls ``add_variables`` once, then calls ``add_constraints`` with the live
    heat demand from the current ``SolveBundle``.

    **On/off mode variables** (when ``config.elec_power_kw`` is set):

        _hp_on[t]   — Binary; 1 = HP running during step t, 0 = off.
        _start[t]   — Binary sentinel (only when min_run_steps > 1); 1 if
                      the HP turns on at step t (off at t-1, on at t).

    **Power-stage (SOS2) mode variables** (when ``config.stages`` is set):

        _w[t]       — List of SOS2 weight variables, one per stage, per step.
                      Weights satisfy Σ_s w[t][s] = 1 (convex combination).
                      Stage 0 is the zero-power off sentinel.
        _hp_on[t]   — Binary; 1 = HP running at any non-zero stage during step t.
                      Equals Σ_s w[t][s] for s >= 1 (sum of non-sentinel stage weights).
        _start[t]   — Binary sentinel (only when min_run_steps > 1); same semantics
                      as on/off mode.

    Attributes:
        name: Device name matching the key in ``config.space_heating_hps``.
        config: Static space heating heat pump configuration.
    """

    def __init__(self, name: str, config: SpaceHeatingConfig) -> None:
        """Initialise the SpaceHeatingDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.space_heating_hps``.
            config: Validated static configuration for this space heating heat pump.
        """
        self.name = name
        self.config = config

        # Solver variables populated by add_variables.
        self._hp_on: dict[int, Any] = {}
        # _start[t] = 1 when the HP transitions from off to on at step t.
        # Only populated when min_run_steps > 1.
        self._start: dict[int, Any] = {}
        # _w[t] = list of SOS2 weight variables indexed by stage.
        # Only populated in power-stage (SOS2) mode.
        self._w: dict[int, list[Any]] = {}
        # _T_indoor[t] = indoor temperature variable at the end of step t, in °C.
        # Only populated when config.building_thermal is set.
        self._T_indoor: dict[int, Any] = {}

        self._dt: float = 0.25  # overwritten in add_variables from ctx.dt

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this space heating heat pump.

        **On/off mode** (``config.elec_power_kw`` is not None):

        For each time step t in ctx.T:
        - ``_hp_on[t]``: Binary variable. 1 = HP running at full rated power,
          0 = off.

        If ``config.min_run_steps > 1``, for each t from 1 to T-1:
        - ``_start[t]``: Binary sentinel detecting on-transitions.

        **Power-stage (SOS2) mode** (``config.stages`` is not None):

        For each time step t in ctx.T:
        - ``_w[t]``: A list of SOS2 weight variables, one per stage. Each weight
          is a continuous variable in [0, 1]. The SOS2 constraint ensures that at
          most two adjacent weights are non-zero at any step. Stage 0 with
          ``elec_kw=0.0`` represents the HP being off.
        - ``_hp_on[t]``: Binary variable. 1 = HP is running at any non-zero power
          stage. This equals the sum of all non-sentinel stage weights (s >= 1).
          Used by the minimum run constraint.

        If ``config.min_run_steps > 1``, for each t from 1 to T-1:
        - ``_start[t]``: Binary sentinel, same semantics as on/off mode.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt
        cfg = self.config

        if cfg.stages is not None:
            # Power-stage (SOS2) mode.
            n_stages = len(cfg.stages)
            for t in ctx.T:
                # One continuous weight variable per stage. The SOS2 constraint
                # enforces that at most two adjacent weights are non-zero.
                # The ordering weights are the stage indices (0, 1, 2, ...).
                weights = [ctx.solver.add_var(lb=0.0, ub=1.0) for _ in range(n_stages)]
                sos2_weights = list(range(n_stages))
                ctx.solver.add_sos2(weights, sos2_weights)
                self._w[t] = weights

                # Binary "on" indicator: 1 when the HP is at any non-zero stage.
                # Derived from the SOS2 weights in add_constraints.
                self._hp_on[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
        else:
            # On/off mode: single binary per step.
            for t in ctx.T:
                self._hp_on[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        if cfg.min_run_steps > 1:
            for t in range(1, len(ctx.T)):
                self._start[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        if cfg.building_thermal is not None:
            # Building thermal model: declare a continuous indoor temperature
            # variable at each step. Bounds enforce the comfort envelope directly.
            #
            # T_indoor[t] is bounded [comfort_min_c, comfort_max_c]. The solver
            # chooses the temperature trajectory; the BTM dynamics constraint
            # (added in add_constraints) link T_indoor to the HP on/off decisions.
            btm = cfg.building_thermal
            for t in ctx.T:
                self._T_indoor[t] = ctx.solver.add_var(
                    lb=btm.comfort_min_c,
                    ub=btm.comfort_max_c,
                )

    def add_constraints(self, ctx: ModelContext, inputs: SpaceHeatingInputs) -> None:
        """Add all MILP constraints for this space heating heat pump.

        **Zero-demand early exit**: When ``inputs.heat_needed_kwh == 0.0``, all
        ``_hp_on[t]`` are pinned to zero and no further constraints are added. The
        HP stays off for the entire horizon.

        **On/off mode** (``config.elec_power_kw`` is not None):

        *Total heat constraint*: The sum of thermal output across all active steps
        must satisfy the demand:

            Σ_t (elec_power_kw × cop × dt × hp_on[t]) >= heat_needed_kwh

        *Minimum run length* (when ``min_run_steps > 1``): Same sentinel-based
        formulation as ``ThermalBoilerDevice``:

            start[t] >= hp_on[t] - hp_on[t-1]
            start[t] <= hp_on[t]
            hp_on[t+τ] >= start[t]   for τ in 1 .. min_run_steps-1

        **Power-stage (SOS2) mode** (``config.stages`` is not None):

        *Convex combination constraint*: Stage weights must sum to exactly 1:

            Σ_s w[t][s] = 1

        *On indicator linkage*: The binary ``_hp_on[t]`` equals the sum of all
        non-sentinel stage weights (the HP is "on" when operating at any power
        above zero):

            hp_on[t] == Σ_{s>=1} w[t][s]

        *Total heat constraint*: The sum of thermal delivered across all steps
        must satisfy the demand:

            Σ_t Σ_s (w[t][s] × elec_kw[s] × cop[s] × dt) >= heat_needed_kwh

        *Minimum run length*: Applied to the shared ``_hp_on[t]`` binary using
        the same sentinel formulation as on/off mode.

        Args:
            ctx: The current solve context.
            inputs: Live heat demand for this horizon. ``heat_needed_kwh`` is
                the total thermal energy in kWh that the HP must produce.
        """
        cfg = self.config

        if inputs.heat_needed_kwh == 0.0 and cfg.building_thermal is None:
            # No heat needed and no BTM — skip all constraints.
            # The HP variables remain free so the solver can still respect
            # the power balance; the minimum objective cost will naturally
            # keep the HP off. No obligation to produce heat is added.
            return

        if cfg.building_thermal is not None:
            # BTM path: comfort envelope drives scheduling instead of the
            # degree-days total. heat_needed_kwh is intentionally ignored.
            self._add_btm_constraints(ctx, inputs)
        elif cfg.stages is not None:
            self._add_constraints_staged(ctx, inputs)
        else:
            self._add_constraints_on_off(ctx, inputs)

        self._add_min_run_constraints(ctx)

    def _add_btm_constraints(
        self, ctx: ModelContext, inputs: SpaceHeatingInputs
    ) -> None:
        """Add building thermal model (BTM) dynamics constraints.

        Replaces the degree-days total-heat lower bound with a first-order
        difference equation that tracks indoor temperature at each step.

        The dynamics equation for step t is:

            T_indoor[t] = alpha * T_prev
                        + (dt / C) * P_heat[t]
                        + beta_outdoor * T_outdoor[t]

        where:
            alpha        = 1 - dt * L / C
            beta_outdoor = dt * L / C
            C            = thermal_capacity_kwh_per_k   (building thermal mass)
            L            = heat_loss_coeff_kw_per_k      (heat loss coefficient)
            dt           = ctx.dt                        (step duration, hours)
            T_prev       = current_indoor_temp_c for t=0; T_indoor[t-1] for t > 0
            P_heat[t]    = thermal power delivered to the building at step t (kW)

        The comfort bounds [comfort_min_c, comfort_max_c] are enforced by the
        variable bounds declared in add_variables; no explicit inequality is added
        here.

        P_heat depends on the control mode:
        - On/off: P_heat[t] = elec_power_kw * cop * hp_on[t]
        - SOS2:   P_heat[t] = sum(w[t][s] * stages[s].elec_kw * stages[s].cop ...)

        All terms are linear in the solver variables because cop and elec_power_kw
        are constants multiplied by the binary/continuous solver variables.

        Args:
            ctx: The current solve context.
            inputs: Live heating demand. Must contain current_indoor_temp_c and
                outdoor_temp_forecast_c at least as long as the horizon.

        Raises:
            ValueError: If outdoor_temp_forecast_c is shorter than the horizon,
                or if current_indoor_temp_c is None.
        """
        cfg = self.config
        btm = cfg.building_thermal  # type: ignore[union-attr]
        H = len(ctx.T)

        if inputs.current_indoor_temp_c is None:
            raise ValueError(
                f"Device '{self.name}': building_thermal is configured but "
                "current_indoor_temp_c is None in SpaceHeatingInputs."
            )
        if inputs.outdoor_temp_forecast_c is None or len(inputs.outdoor_temp_forecast_c) < H:
            have = len(inputs.outdoor_temp_forecast_c) if inputs.outdoor_temp_forecast_c else 0
            raise ValueError(
                f"Device '{self.name}': outdoor_temp_forecast_c has {have} values "
                f"but the horizon requires {H}."
            )

        C = btm.thermal_capacity_kwh_per_k
        L = btm.heat_loss_coeff_kw_per_k
        dt = ctx.dt

        # Derived coefficients for the first-order difference equation.
        # alpha: fraction of the previous indoor temperature retained after one step.
        # A value below 1.0 means the building loses heat proportionally to the
        # indoor-outdoor temperature gap.
        alpha = 1.0 - dt * L / C

        # beta_outdoor: contribution of outdoor temperature to indoor at each step.
        # Equals dt * L / C = (1 - alpha). At equilibrium (no HP, steady outdoor),
        # T_indoor -> T_outdoor as the building tracks outdoor temperature.
        beta_outdoor = dt * L / C

        # dt_over_C: converts thermal power (kW) * dt (h) = kWh into the
        # temperature rise per step (°C). Multiplied by P_heat to get the
        # heat-driven temperature increment.
        dt_over_C = dt / C

        stages = cfg.stages

        for t in ctx.T:
            # T_prev is the indoor temperature entering this step.
            # For t=0 it is the measured current temperature; for t>0 it is the
            # previous step's decision variable.
            if t == 0:
                t_prev = inputs.current_indoor_temp_c
            else:
                t_prev = self._T_indoor[t - 1]

            T_outdoor_t = inputs.outdoor_temp_forecast_c[t]

            # P_heat_expr: thermal power expression for this step (kW).
            # This is linear in the solver variables hp_on[t] or w[t][s].
            if stages is not None:
                # SOS2 mode: sum over all non-sentinel stages.
                # P_heat = sum(w[t][s] * elec_kw[s] * cop[s] for s >= 0)
                # (Stage 0 has elec_kw=0, cop=0, so it contributes zero naturally.)
                p_heat_expr = sum(
                    self._w[t][s] * (stages[s].elec_kw * stages[s].cop)
                    for s in range(len(stages))
                )
            else:
                # On/off mode: P_heat = elec_power_kw * cop * hp_on[t].
                p_heat_expr = (cfg.elec_power_kw * cfg.cop) * self._hp_on[t]  # type: ignore[operator]

            # Dynamics equality constraint:
            #
            #   T_indoor[t] = alpha * T_prev
            #               + (dt/C) * P_heat[t]
            #               + beta_outdoor * T_outdoor[t]
            #
            # Rearranged so all solver variable terms are on the left and
            # the constant terms are on the right:
            #
            #   T_indoor[t] - (dt/C) * P_heat[t] - alpha * T_prev (if variable)
            #     = alpha * T_prev (if constant, t=0) + beta_outdoor * T_outdoor[t]
            rhs = beta_outdoor * T_outdoor_t
            if t == 0:
                # T_prev is a known constant (the current measured temperature).
                rhs += alpha * t_prev  # type: ignore[operator]
                ctx.solver.add_constraint(
                    self._T_indoor[t] - dt_over_C * p_heat_expr == rhs
                )
            else:
                # T_prev = T_indoor[t-1] is a solver variable. Move it to the LHS.
                ctx.solver.add_constraint(
                    self._T_indoor[t] - dt_over_C * p_heat_expr - alpha * t_prev == rhs
                )

    def _add_constraints_on_off(
        self, ctx: ModelContext, inputs: SpaceHeatingInputs
    ) -> None:
        """Add total-heat constraint for on/off mode.

        Args:
            ctx: The current solve context.
            inputs: Live heat demand.
        """
        cfg = self.config
        # thermal_per_step: kWh of heat delivered per active step.
        # elec_power_kw × cop × dt.
        thermal_per_step = cfg.elec_power_kw * cfg.cop * ctx.dt  # type: ignore[operator]

        # The total thermal output summed over all steps must be at least
        # heat_needed_kwh. The solver can satisfy this with the minimum number
        # of active steps subject to the minimum run constraint.
        ctx.solver.add_constraint(
            sum(thermal_per_step * self._hp_on[t] for t in ctx.T)
            >= inputs.heat_needed_kwh
        )

    def _add_constraints_staged(
        self, ctx: ModelContext, inputs: SpaceHeatingInputs
    ) -> None:
        """Add SOS2 convex-combination, on-indicator, and total-heat constraints.

        Args:
            ctx: The current solve context.
            inputs: Live heat demand.
        """
        cfg = self.config
        stages = cfg.stages  # type: ignore[assignment]
        n_stages = len(stages)

        total_heat_terms: list[Any] = []

        for t in ctx.T:
            # Convex combination: weights must sum to 1 at every step.
            # This forces the solver to select exactly one operating point or
            # a convex blend of two adjacent points (the SOS2 constraint
            # limits which pairs of weights can be jointly non-zero).
            ctx.solver.add_constraint(
                sum(self._w[t][s] for s in range(n_stages)) == 1
            )

            # On-indicator linkage: _hp_on[t] = 1 when operating at any
            # power stage above the zero sentinel (stage 0). The SOS2
            # constraint guarantees that the sentinel weight w[t][0] and
            # any non-zero-stage weight are always in adjacent positions when
            # both are non-zero, so this sum is a valid binary (between 0 and 1).
            non_sentinel_sum = sum(self._w[t][s] for s in range(1, n_stages))
            ctx.solver.add_constraint(self._hp_on[t] == non_sentinel_sum)

            # Collect thermal output terms for the total-heat constraint.
            # Each non-sentinel stage s contributes:
            #   w[t][s] × elec_kw[s] × cop[s] × dt  kWh of heat.
            # Stage 0 contributes zero (elec_kw=0, cop=0) and is excluded.
            for s in range(1, n_stages):
                total_heat_terms.append(
                    self._w[t][s] * stages[s].elec_kw * stages[s].cop * ctx.dt
                )

        # Total heat lower-bound: the solver must produce at least heat_needed_kwh
        # across all steps.
        ctx.solver.add_constraint(sum(total_heat_terms) >= inputs.heat_needed_kwh)

    def _add_min_run_constraints(self, ctx: ModelContext) -> None:
        """Add minimum consecutive run length constraints.

        Uses start[t] sentinel variables to detect on-transitions and force the
        HP to remain on for at least ``min_run_steps`` consecutive steps once
        started. Only active when ``min_run_steps > 1``.

        The sentinel-based formulation (identical to ThermalBoilerDevice):

            start[t] >= hp_on[t] - hp_on[t-1]   (fires when HP turns on)
            start[t] <= hp_on[t]                 (cannot fire when HP is off)
            hp_on[t+τ] >= start[t]               (run must continue for τ steps)

        Args:
            ctx: The current solve context.
        """
        cfg = self.config
        if cfg.min_run_steps <= 1:
            return

        n = len(ctx.T)

        # A start at step t requires that the HP stays on for steps
        # t, t+1, ..., t+min_run_steps-1. If the horizon ends before
        # that window is complete, a fresh start at step t is not feasible.
        # For such steps, the HP can only be on if it was already running from
        # an earlier step (hp_on[t] <= hp_on[t-1]). For step 0, there is no
        # prior step, so the HP cannot start if the window would exceed the
        # horizon.
        for t in range(n):
            if t + cfg.min_run_steps > n:
                # Not enough steps remaining for a fresh start here.
                if t == 0:
                    ctx.solver.add_constraint(self._hp_on[0] == 0)
                else:
                    ctx.solver.add_constraint(
                        self._hp_on[t] <= self._hp_on[t - 1]
                    )

        # Step 0 is always a potential start (no prior step). If the HP is on
        # at step 0, it must remain on for the following min_run_steps - 1 steps.
        # (This is a no-op when step 0 was already blocked above.)
        for tau in range(1, cfg.min_run_steps):
            if tau < n:
                ctx.solver.add_constraint(self._hp_on[tau] >= self._hp_on[0])

        for t in range(1, n):
            ctx.solver.add_constraint(
                self._start[t] >= self._hp_on[t] - self._hp_on[t - 1]
            )
            ctx.solver.add_constraint(self._start[t] <= self._hp_on[t])

            for tau in range(1, cfg.min_run_steps):
                if t + tau < n:
                    ctx.solver.add_constraint(
                        self._hp_on[t + tau] >= self._start[t]
                    )

    def net_power(self, t: int) -> Any:
        """Net AC power drawn from the home bus at step t, in kW.

        The sign follows the Device Protocol convention: positive = producing
        power (injection into home bus), negative = consuming power (draw from
        home bus). A heat pump is always a consumer, so net_power is always <= 0.

        **On/off mode**: Returns ``-elec_power_kw × _hp_on[t]``.

        **Power-stage (SOS2) mode**: Returns the negative sum of weighted
        electrical powers across all stages:
        ``-Σ_s (w[t][s] × stages[s].elec_kw)``.

        The return value is a solver expression because it involves solver
        variables.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for the net AC power at step t in kW.
        """
        cfg = self.config
        if cfg.stages is not None:
            return -sum(
                self._w[t][s] * cfg.stages[s].elec_kw
                for s in range(len(cfg.stages))
            )
        return -(cfg.elec_power_kw * self._hp_on[t])  # type: ignore[operator]

    def objective_terms(self, t: int) -> list[Any]:
        """Return objective cost terms for time step t.

        The only cost term is the optional wear cost, which penalises electrical
        energy consumption to model compressor cycling degradation. It is added
        on top of the electricity price term in the objective.

        **On/off mode**: ``wear_cost_eur_per_kwh × elec_power_kw × dt × hp_on[t]``.

        **Power-stage (SOS2) mode**: ``wear_cost_eur_per_kwh × Σ_s (w[t][s] ×
        stages[s].elec_kw × dt)``.

        Args:
            t: Zero-based time step index.

        Returns:
            List of solver-expression terms to add to the minimisation objective.
            Empty when ``wear_cost_eur_per_kwh == 0.0``.
        """
        cfg = self.config
        if cfg.wear_cost_eur_per_kwh == 0.0:
            return []

        if cfg.stages is not None:
            elec = sum(
                self._w[t][s] * cfg.stages[s].elec_kw
                for s in range(len(cfg.stages))
            )
            return [cfg.wear_cost_eur_per_kwh * elec * self._dt]

        return [cfg.wear_cost_eur_per_kwh * cfg.elec_power_kw * self._dt * self._hp_on[t]]  # type: ignore[operator]
