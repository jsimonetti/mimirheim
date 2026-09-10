"""ThermalBoilerDevice — models a thermal boiler (resistive or heat pump DHW) in the MILP.

This module implements the Device Protocol for a thermal boiler. Both an electric
immersion heater and a heat pump domestic hot water boiler use the same model: a
binary on/off variable per step, a linear temperature dynamics constraint (rise when
on, cooling loss when off), and hard bounds on the tank temperature. The two device
classes differ only in the coefficient of performance (COP) applied to the electrical
input: COP=1.0 for resistive elements, COP >= 2.0 for heat pumps.

The module does not import from ``mimirheim.io``. The current tank temperature is passed
as a ``ThermalBoilerInputs`` argument to ``add_constraints``. All solver interactions
go through ``ModelContext.solver``.
"""

from typing import Any

from mimirheim.config.schema import ThermalBoilerConfig
from mimirheim.core.bundle import ThermalBoilerInputs
from mimirheim.core.context import ModelContext

# Specific heat capacity of water: 4186 J/(kg·K) = 4186/3600 Wh/(kg·K).
# One litre of water weighs approximately 1 kg.
# Converting to kWh/(litre·K): 4186 / 3600 / 1000 ≈ 0.001163 kWh/(L·K).
# Used to convert electrical energy input (kWh) to temperature rise (K):
#   ΔT = kWh_thermal / (volume_L × _WATER_THERMAL_CAP_KWH_PER_LITRE_K)
_WATER_THERMAL_CAP_KWH_PER_LITRE_K: float = 4186 / 3600 / 1000  # kWh/(L·K)


class ThermalBoilerDevice:
    """Models a thermal boiler as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.thermal_boilers``. The
    model builder creates one ``ThermalBoilerDevice`` per named boiler in the
    config, calls ``add_variables`` once, then calls ``add_constraints`` with
    the live tank temperature from the current ``SolveBundle``.

    The device exposes the following solver variables after ``add_variables``:

        _T_tank[t]    — Water temperature at the end of step t, in °C.
        _heater_on[t] — Binary; 1 = heater active during step t, 0 = off.
        _start[t]     — Binary sentinel (only when min_run_steps > 1); 1 if
                        the heater turns on at step t.

    Attributes:
        name: Device name matching the key in ``config.thermal_boilers``.
        config: Static thermal boiler configuration.
    """

    def __init__(self, name: str, config: ThermalBoilerConfig) -> None:
        """Initialise the ThermalBoilerDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.thermal_boilers``.
            config: Validated static configuration for this thermal boiler.
        """
        self.name = name
        self.config = config

        # Solver variables populated by add_variables.
        self._T_tank: dict[int, Any] = {}
        self._heater_on: dict[int, Any] = {}
        # _start[t] = 1 when the heater transitions from off (t-1) to on (t).
        # Only populated when min_run_steps > 1.
        self._start: dict[int, Any] = {}

        self._dt: float = 0.25  # set from ctx in add_variables

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this thermal boiler.

        For each time step t in ctx.T:

        - ``_T_tank[t]``: Water temperature at the end of step t, in degrees
          Celsius. Lower bound: ``min_temp_c − 5`` (small numerical slack below
          the hard bound, which is enforced as a constraint). Upper bound:
          ``setpoint_c + 5`` (corresponding slack above the setpoint). These
          loose variable bounds prevent the LP from becoming infeasible due to
          numerical noise; the hard temperature bounds are enforced explicitly
          in ``add_constraints``.

        - ``_heater_on[t]``: Binary variable. 1 = the heating element or heat
          pump compressor is active during step t. 0 = off. The on/off state
          determines the thermal power added to the tank at this step.

        If ``config.min_run_steps > 1``, for each t from 1 to T−1:

        - ``_start[t]``: Binary sentinel. Set to 1 by the solver when the
          heater transitions from off to on (heater_on[t]=1, heater_on[t-1]=0).
          Used by the minimum run constraint to enforce that once started, the
          heater stays on for at least ``min_run_steps`` consecutive steps.
          Not created for t=0 (no previous step to compare) or when the minimum
          run length is 0 or 1 (no minimum run to enforce).

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt
        cfg = self.config

        for t in ctx.T:
            # T_tank[t]: tank temperature at the end of step t.
            # A small slack on the bounds (±5°C) keeps the LP feasible under
            # minor numerical noise while the real bounds are handled as
            # explicit solver constraints in add_constraints.
            self._T_tank[t] = ctx.solver.add_var(
                lb=cfg.min_temp_c - 5.0,
                ub=cfg.setpoint_c + 5.0,
            )

            # heater_on[t]: binary on/off for the heating element.
            self._heater_on[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        if cfg.min_run_steps > 1:
            # start[t] sentinels: needed to detect on-transitions and enforce
            # the minimum consecutive run length.
            for t in range(1, len(ctx.T)):
                self._start[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

    def add_constraints(self, ctx: ModelContext, inputs: ThermalBoilerInputs) -> None:
        """Add all MILP constraints for this thermal boiler.

        Adds, per step: the temperature dynamics equation (``T_tank[t] =
        T_tank[t-1] - cool_per_step + heat_rise_per_step * heater_on[t]``,
        with ``inputs.current_temp_c`` as the initial condition at ``t=0``),
        hard bounds (``min_temp_c <= T_tank[t] <= setpoint_c``, enforced as
        explicit constraints because the dynamics can otherwise push past
        tightened variable bounds), and, when ``min_run_steps > 1``, the
        minimum run-time constraint. See IMPLEMENTATION_DETAILS.md §8,
        subsection "Thermal boiler and DHW tank dynamics", for the tank model
        derivation and the start-sentinel run-length mechanism.

        Args:
            ctx: The current solve context.
            inputs: Live temperature reading for this boiler. ``current_temp_c``
                is the initial tank temperature for the first step.
        """
        cfg = self.config

        # Thermal parameters derived from static config (plain floats, not
        # solver variables): cool_per_step is the unconditional temperature
        # drop per step when off (insulation losses plus hot water draws);
        # heat_rise_per_step is the rise per step when on (elec_power_kw *
        # cop * dt / thermal_cap_kwh_per_k — cop=1.0 for resistive elements,
        # >1 for heat pumps).
        thermal_cap_kwh_per_k = cfg.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
        cool_per_step = cfg.cooling_rate_k_per_hour * ctx.dt
        heat_rise_per_step = cfg.elec_power_kw * cfg.cop * ctx.dt / thermal_cap_kwh_per_k

        for t in ctx.T:
            # prior_temp: inputs.current_temp_c at t=0, else the previous
            # step's solver variable.
            prior_temp: Any = inputs.current_temp_c if t == 0 else self._T_tank[t - 1]
            ctx.solver.add_constraint(
                self._T_tank[t]
                == prior_temp - cool_per_step + heat_rise_per_step * self._heater_on[t]
            )

            # Hard temperature bounds as explicit constraints, not variable
            # bounds — the dynamics can otherwise push past a tightened bound.
            ctx.solver.add_constraint(self._T_tank[t] >= cfg.min_temp_c)
            ctx.solver.add_constraint(self._T_tank[t] <= cfg.setpoint_c)

        # Minimum run length (e.g. a compressor that must run in blocks). See
        # IMPLEMENTATION_DETAILS.md §8, subsection "Thermal boiler and DHW
        # tank dynamics", for the start-sentinel mechanism.
        if cfg.min_run_steps > 1:
            for t in range(1, len(ctx.T)):
                ctx.solver.add_constraint(
                    self._start[t] >= self._heater_on[t] - self._heater_on[t - 1]
                )
                ctx.solver.add_constraint(self._start[t] <= self._heater_on[t])

                for tau in range(1, cfg.min_run_steps):
                    if t + tau < len(ctx.T):
                        ctx.solver.add_constraint(
                            self._heater_on[t + tau] >= self._start[t]
                        )

    def net_power(self, t: int) -> Any:
        """Net AC power drawn from the home bus at step t, in kW.

        The boiler draws ``elec_power_kw`` from the AC bus when active and
        zero when off. The sign follows the Device Protocol convention:
        positive = producing power (injection), negative = consuming power
        (draw). A heater is always a consumer, so net_power is always <= 0.

        The return value is a solver expression because ``_heater_on[t]`` is a
        solver variable. The expression evaluates to exactly ``-elec_power_kw``
        when ``heater_on[t]=1`` and to 0 when ``heater_on[t]=0``.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for ``−elec_power_kw × heater_on[t]``.
        """
        return -self.config.elec_power_kw * self._heater_on[t]

    def objective_terms(self, t: int) -> list[Any]:
        """Return objective cost terms for time step t.

        The only optional cost term is the wear (cycling) cost, which penalises
        electrical consumption to discourage unnecessary short cycles. For
        resistive elements, ``wear_cost_eur_per_kwh`` should be 0.0. For heat
        pump compressors, a small positive value (e.g. 0.01 EUR/kWh) adds an
        energy-cost-independent penalty on top of the minimum run constraint.

        When ``wear_cost_eur_per_kwh`` is zero (the default), this method
        returns an empty list and contributes nothing to the objective.

        Args:
            t: Zero-based time step index.

        Returns:
            List containing zero or one solver expressions.
        """
        if self.config.wear_cost_eur_per_kwh <= 0.0:
            return []
        return [
            self.config.wear_cost_eur_per_kwh
            * self.config.elec_power_kw
            * self._heater_on[t]
            * self._dt
        ]

    def terminal_soc_var(self, ctx: ModelContext) -> Any | None:
        """Return a solver expression proportional to the thermal energy stored at T-1.

        The terminal value mechanism in ``ObjectiveBuilder`` multiplies the
        returned expression by ``−avg_import_price / dt``, creating a reward
        for leaving the tank warm at the end of the horizon.

        The expression represents the equivalent electrical kWh stored above the
        minimum temperature at the last step:

            (T_tank[T−1] − min_temp_c) × thermal_cap_kwh_per_k / cop

        Dividing by COP converts thermal kWh to electrical kWh equivalent,
        so the terminal value uses the same units as battery SOC (kWh_electric).
        This ensures an equal average import price coefficient applies to all
        storage devices regardless of their COP.

        Without this terminal value, the solver would drain the tank to
        ``min_temp_c`` at the end of the horizon (free heat storage is treated
        as worthless in a finite-horizon model). With the terminal value, the
        solver preserves tank temperature when the expected refill cost exceeds
        the export price.

        Args:
            ctx: The current solve context. Used to identify the last step.

        Returns:
            Solver expression in kWh_electric, or None if add_variables has
            not been called yet.
        """
        if ctx.T[-1] not in self._T_tank:
            return None
        thermal_cap = self.config.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
        # (T_tank[T-1] - min_temp_c) gives the temperature surplus above the floor,
        # in K. Multiplying by thermal_cap converts to kWh_thermal. Dividing by cop
        # converts to the equivalent kWh_electric needed to produce that heat.
        factor = thermal_cap / self.config.cop
        return (self._T_tank[ctx.T[-1]] - self.config.min_temp_c) * factor
