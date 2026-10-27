"""HeatStoreDevice — models the boost of a heat store in the MILP.

A heat store (DHW tank, pool, hot tub) has its own normal program that keeps
it at the normal setpoint without mimirheim. This module models only the boost:
extra heating on top of the normal program, with a continuous power per step
and no binary variables. The normal program itself is not modelled as a load;
its consumption stays in the base load forecast. Its effect enters the model
only through the normal-program electricity that boost heat saves.

The device reports that saved electricity in kWh_e through
``saved_electricity_kwh`` and ``terminal_saved_electricity_kwh``. It does not
know about prices or strategies; ``ObjectiveBuilder`` prices the quantities.

The module does not import from ``mimirheim.io``. The current store temperature
is passed as a ``HeatStoreInputs`` argument to ``add_constraints``. All solver
interactions go through ``ModelContext.solver``.
"""

from typing import Any

from mimirheim.config.schema import HeatStoreConfig
from mimirheim.core.bundle import HeatStoreInputs
from mimirheim.core.context import ModelContext
from mimirheim.devices.thermal_boiler import _WATER_THERMAL_CAP_KWH_PER_LITRE_K


class HeatStoreDevice:
    """Models the boost of one heat store as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.heat_stores``. The model
    builder creates one ``HeatStoreDevice`` per named store, calls
    ``add_variables`` once, then ``add_constraints`` with the live store
    temperature from the current ``SolveBundle``.

    The state is boost heat in kWh_th, not temperature, so that a store
    reporting a state of charge instead of a temperature can be added later
    without changing the model. Temperature is derived from it for the bounds
    and the outputs.

    Attributes:
        name: Device name matching the key in ``config.heat_stores``.
        config: Static heat store configuration.
    """

    def __init__(self, name: str, config: HeatStoreConfig) -> None:
        """Initialise the HeatStoreDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.heat_stores``.
            config: Validated static configuration for this heat store.
        """
        self.name = name
        self.config = config

        # Heat capacity of the stored water, kWh per kelvin.
        self._cap_kwh_per_k = config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K

        # Solver variables populated by add_variables.
        self._boost_kw: dict[int, Any] = {}
        self._heat: dict[int, Any] = {}
        self._consumed: dict[int, Any] = {}
        self._gap_fill: dict[int, Any] = {}

        # Heat below the normal setpoint at solve start, kWh_th. Set in
        # add_constraints from the live temperature.
        self._gap_kwh: float = 0.0

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this heat store.

        All variables are continuous. The upper bound on boost heat depends on
        the live temperature, so it is added as a constraint in
        ``add_constraints`` rather than as a variable bound here.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        cfg = self.config
        for t in ctx.T:
            # boost_kw[t]: electrical boost power drawn from the AC bus during
            # step t, kW. Continuous between off and full power: a relay that
            # can only switch fully is approximated by the published on/off flag.
            self._boost_kw[t] = ctx.solver.add_var(lb=0.0, ub=cfg.boost.power_kw)

            # heat[t]: boost heat held above the normal setpoint at the end of
            # step t, kWh_th. Non-negative: below the setpoint the normal
            # program, not the boost, owns the store.
            self._heat[t] = ctx.solver.add_var(lb=0.0)

            # consumed[t]: boost heat used up during step t in place of the
            # normal program, kWh_th. Bounded in add_constraints by the standby
            # loss at the setpoint, which is what the normal program would
            # otherwise have to supply.
            self._consumed[t] = ctx.solver.add_var(lb=0.0)

            # gap_fill[t]: boost heat delivered during step t into the gap below
            # the normal setpoint present at solve start, kWh_th. Zero unless
            # the store starts below the setpoint.
            self._gap_fill[t] = ctx.solver.add_var(lb=0.0)

    def add_constraints(self, ctx: ModelContext, inputs: HeatStoreInputs) -> None:
        """Add all MILP constraints for this heat store.

        Adds, per step, the boost heat balance (heat in from the boost, loss
        above the setpoint, heat consumed in place of the normal program, heat
        delivered into the starting gap), the bound on consumed heat, and the
        temperature ceiling. See IMPLEMENTATION_DETAILS.md §8, subsection
        "Heat store boost and running minimum price", for the model derivation.

        Args:
            ctx: The current solve context.
            inputs: Live store temperature. ``current_temp_c`` sets the boost
                heat (above the setpoint) or the gap (below it) at solve start.
        """
        cfg = self.config
        cap = self._cap_kwh_per_k
        setpoint = cfg.normal_setpoint_c

        # Loss is linear in temperature: drift_k_per_hour at the setpoint,
        # scaled by (T - ambient) / (setpoint - ambient). Split into the loss
        # at the setpoint (a constant the normal program covers when there is
        # no boost heat) and an extra loss proportional to the heat above it.
        #   standby_kwh: heat lost per step at the setpoint, kWh_th.
        #   loss_frac:   fraction of boost heat lost per step on top of that.
        standby_kwh = cap * cfg.drift_k_per_hour * ctx.dt
        loss_frac = cfg.drift_k_per_hour / (setpoint - cfg.ambient_c) * ctx.dt

        start_heat = max(0.0, cap * (inputs.current_temp_c - setpoint))
        self._gap_kwh = max(0.0, cap * (setpoint - inputs.current_temp_c))

        # no_boost_heat: boost heat the store would hold at the end of each
        # step with the boost off, kWh_th. Used for the temperature ceiling.
        no_boost_heat = start_heat
        ceiling_heat = cap * (cfg.boost.max_temp_c - setpoint)

        gap_filled: Any = 0.0
        for t in ctx.T:
            prior: Any = start_heat if t == 0 else self._heat[t - 1]

            # Boost heat balance, kWh_th. Loss is taken on the heat held at the
            # start of the step (explicit step), so heat delivered and consumed
            # within one step is not charged a loss.
            ctx.solver.add_constraint(
                self._heat[t]
                == (1.0 - loss_frac) * prior
                + cfg.boost.cop * ctx.dt * self._boost_kw[t]
                - self._consumed[t]
                - self._gap_fill[t]
            )

            # Boost heat can replace at most the standby loss at the setpoint:
            # that is all the normal program would have supplied this step.
            ctx.solver.add_constraint(self._consumed[t] <= standby_kwh)

            # Temperature ceiling: max(boost.max_temp_c, temperature with no
            # boost). The no-boost term keeps a store that starts above the
            # limit feasible; it allows no boost until the store cools below it.
            no_boost_heat = max(0.0, (1.0 - loss_frac) * no_boost_heat - standby_kwh)
            ctx.solver.add_constraint(self._heat[t] <= max(ceiling_heat, no_boost_heat))

            gap_filled = gap_filled + self._gap_fill[t]

        # The boost can fill at most the gap that exists at solve start. The
        # normal program covers any loss below the setpoint.
        ctx.solver.add_constraint(gap_filled <= self._gap_kwh)

    def net_power(self, t: int) -> Any:
        """Net AC power at step t in kW: the boost draw, always <= 0.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for ``-boost_kw[t]``.
        """
        return -self._boost_kw[t]

    def objective_terms(self, t: int) -> list[Any]:
        """Return objective cost terms for time step t.

        The heat store has no wear cost. Its value enters the objective through
        ``saved_electricity_kwh`` instead.

        Args:
            t: Zero-based time step index.

        Returns:
            An empty list.
        """
        return []

    def saved_electricity_kwh(self, t: int) -> Any:
        """Normal-program electricity saved during step t, in kWh_e.

        Boost heat consumed in place of the normal program, plus boost heat
        delivered into the starting gap below the setpoint, divided by the
        normal program's COP. ``ObjectiveBuilder`` prices it.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression in kWh_e.
        """
        return (self._consumed[t] + self._gap_fill[t]) * (1.0 / self.config.normal_cop)

    def terminal_saved_electricity_kwh(self, ctx: ModelContext) -> Any | None:
        """Normal-program electricity saved after the horizon, in kWh_e.

        Boost heat left above the setpoint at the last step, divided by the
        normal program's COP. Deliberately not exposed as
        ``terminal_soc_var``: that hook prices at the mean import price, which
        would let the plan bank heat for a later price peak. See
        IMPLEMENTATION_DETAILS.md §8, subsection "Heat store boost and running
        minimum price".

        Args:
            ctx: The current solve context. Used to identify the last step.

        Returns:
            Solver expression in kWh_e, or None if add_variables has not been
            called yet.
        """
        last = ctx.T[-1]
        if last not in self._heat:
            return None
        return self._heat[last] * (1.0 / self.config.normal_cop)

    def planned_temp_c(self, ctx: ModelContext, t: int) -> float:
        """Planned store temperature at the end of step t after a solve, °C.

        Derived from the boost heat above the setpoint, less any part of the
        starting gap the boost has not filled by step t.

        Args:
            ctx: The solved model context.
            t: Zero-based time step index.

        Returns:
            Planned temperature in degrees Celsius.
        """
        heat = ctx.solver.var_value(self._heat[t])
        filled = sum(ctx.solver.var_value(self._gap_fill[s]) for s in range(t + 1))
        gap_left = max(0.0, self._gap_kwh - filled)
        return self.config.normal_setpoint_c + (heat - gap_left) / self._cap_kwh_per_k
