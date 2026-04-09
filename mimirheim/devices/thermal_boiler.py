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

        Constraints fall into four groups:

        **Temperature dynamics**: At each step t, the tank temperature equals
        the previous temperature minus the per-step cooling loss plus the
        heating contribution when the element is active:

            T_tank[t] = T_tank[t−1] − cool_per_step + heat_rise_per_step × heater_on[t]

        where:
            cool_per_step = cooling_rate_k_per_hour × dt
            thermal_cap_kwh_per_k = volume_liters × _WATER_THERMAL_CAP_KWH_PER_LITRE_K
            heat_rise_per_step = elec_power_kw × cop × dt / thermal_cap_kwh_per_k

        For t=0, ``T_tank[−1]`` is replaced by ``inputs.current_temp_c``.

        **Hard temperature bounds**: Enforced as explicit constraints to prevent
        the solver from overheating or underheating the tank:

            T_tank[t] >= min_temp_c
            T_tank[t] <= setpoint_c

        **Minimum run length** (only when ``min_run_steps > 1``): Prevents
        partial runs shorter than the configured minimum. The start sentinel
        detects on-transitions; once the heater starts, it must remain on for
        at least ``min_run_steps`` consecutive steps:

            start[t] >= heater_on[t] − heater_on[t−1]   (activates when turning on)
            start[t] <= heater_on[t]                     (can only be 1 when heater is on)
            heater_on[t+τ] >= start[t]   for τ in 1 .. min_run_steps−1 and t+τ < T

        The start sentinel is only constrained from above (not equality) so the
        solver can choose to set it to 0 even when the heater is on continuously.
        Under a cost-minimisation objective, spurious start=1 values would only
        tighten the run constraint, which is never beneficial, so the solver
        naturally drives start to the correct value.

        Args:
            ctx: The current solve context.
            inputs: Live temperature reading for this boiler. ``current_temp_c``
                is the initial tank temperature for the first step.
        """
        cfg = self.config

        # Pre-compute thermal parameters (not solver variables — pure Python floats).
        # These are derived from static config and never change within a solve cycle.
        thermal_cap_kwh_per_k = cfg.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K

        # cool_per_step: the unconditional temperature drop per 15-minute step
        # when the heater is off. Combines insulation losses and hot water draws.
        cool_per_step = cfg.cooling_rate_k_per_hour * ctx.dt

        # heat_rise_per_step: the temperature increase per step when the heater
        # is on. Derived from rated electrical power × COP × time, divided by
        # the tank's thermal mass. COP = 1.0 for resistive elements (all
        # electrical energy becomes heat); COP > 1 for heat pump (each kWh
        # electric produces multiple kWh of thermal energy via the refrigeration
        # cycle).
        heat_rise_per_step = cfg.elec_power_kw * cfg.cop * ctx.dt / thermal_cap_kwh_per_k

        for t in ctx.T:
            # --- Temperature dynamics ---
            # Using 'prior_temp' avoids an if-branch inside the constraint call.
            # At t=0 the prior temperature is the current reading from MQTT.
            # At t>0 it is the solver variable for the previous step.
            prior_temp: Any = inputs.current_temp_c if t == 0 else self._T_tank[t - 1]
            ctx.solver.add_constraint(
                self._T_tank[t]
                == prior_temp - cool_per_step + heat_rise_per_step * self._heater_on[t]
            )

            # --- Hard temperature bounds ---
            # These must be explicit solver constraints (not variable bounds)
            # because the right-hand side involves the heater binary, and the
            # dynamics can cause the variable bounds to be infeasible if tightened.
            ctx.solver.add_constraint(self._T_tank[t] >= cfg.min_temp_c)
            ctx.solver.add_constraint(self._T_tank[t] <= cfg.setpoint_c)

        # --- Minimum run length ---
        # Only needed when min_run_steps > 1. A heat pump compressor that must
        # run in blocks (e.g. 4 × 15 min = 1 hour) uses this constraint.
        if cfg.min_run_steps > 1:
            for t in range(1, len(ctx.T)):
                # start[t] >= heater_on[t] - heater_on[t-1]:
                # When the heater turns on at step t (heater_on[t]=1 but
                # heater_on[t-1]=0), the right-hand side is 1, forcing start[t]=1.
                # When already on, or when off, the right side is <= 0 and start
                # can be 0.
                ctx.solver.add_constraint(
                    self._start[t] >= self._heater_on[t] - self._heater_on[t - 1]
                )
                # start[t] <= heater_on[t]: start cannot be 1 when the heater
                # is off. This prevents the solver from exploiting the inequality
                # above when heater_on[t-1] < 0 (impossible since binary).
                ctx.solver.add_constraint(self._start[t] <= self._heater_on[t])

                # If the heater starts at step t, it must remain on for the
                # following min_run_steps - 1 steps (the start step itself
                # is already constrained by start[t] <= heater_on[t]).
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
