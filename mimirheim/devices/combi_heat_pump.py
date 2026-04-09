"""CombiHeatPumpDevice — models a combined DHW and space heating heat pump in the MILP.

This module implements the Device Protocol for a combined heat pump that can operate
in two mutually exclusive modes:

**DHW mode**: The compressor heats the domestic hot water storage tank. The tank
temperature model is identical to ``ThermalBoilerDevice`` (plan 25): temperature
dynamics, hard upper and lower bounds, and a terminal value crediting stored energy.

**Space heating (SH) mode**: The compressor delivers heat to the space heating circuit
(underfloor heating, radiators). By default the demand model is identical to
``SpaceHeatingDevice`` in on/off mode (plan 26): a total-heat lower-bound constraint
derived from the externally computed degree-days demand.

When ``config.building_thermal`` is set, the SH mode uses the **building thermal
model (BTM)** instead of the degree-days demand: the solver tracks indoor temperature
as a per-step state variable and maintains a comfort band [comfort_min_c, comfort_max_c].
DHW mode is unaffected by the BTM setting.

**Mutual exclusion**: The HP has one refrigerant circuit. It cannot operate in both
modes simultaneously. A Big-M style constraint limits ``dhw_mode[t] + sh_mode[t] <= 1``
at every step.

**Minimum run length**: Applied to the shared ``hp_on[t]`` binary, which equals the
sum of the two mode binaries. Once the compressor starts (in any mode), it must run
for at least ``min_run_steps`` consecutive steps. Mode switches within a running block
are allowed; only the overall on/off transition is constrained.

The module does not import from ``mimirheim.io``. All runtime inputs are passed as a
``CombiHeatPumpInputs`` argument to ``add_constraints``. Solver interactions go
through ``ModelContext.solver``.
"""

from typing import Any

from mimirheim.config.schema import CombiHeatPumpConfig
from mimirheim.core.bundle import CombiHeatPumpInputs
from mimirheim.core.context import ModelContext

# Specific heat capacity of water: 4186 J/(kg·K) = 4186/3600/1000 kWh/(L·K).
# Identical constant to ThermalBoilerDevice — shared physics.
_WATER_THERMAL_CAP_KWH_PER_LITRE_K: float = 4186 / 3600 / 1000  # kWh/(L·K)


class CombiHeatPumpDevice:
    """Models a combined DHW and space heating heat pump as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.combi_heat_pumps``. The model
    builder creates one ``CombiHeatPumpDevice`` per named device in the config, calls
    ``add_variables`` once, then calls ``add_constraints`` with the live state from
    the current ``SolveBundle``.

    Variables after ``add_variables``:

        _T_tank[t]    — DHW water temperature at the end of step t, in °C.
        _dhw_mode[t]  — Binary; 1 = HP heating DHW tank at step t.
        _sh_mode[t]   — Binary; 1 = HP delivering space heating at step t.
        _hp_on[t]     — Binary; 1 = HP running in any mode at step t.
                        Equals dhw_mode[t] + sh_mode[t] by constraint.
        _start[t]     — Binary sentinel (only when min_run_steps > 1); 1 if
                        the HP turns on at step t.

    Attributes:
        name: Device name matching the key in ``config.combi_heat_pumps``.
        config: Static combined heat pump configuration.
    """

    def __init__(self, name: str, config: CombiHeatPumpConfig) -> None:
        """Initialise the CombiHeatPumpDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.combi_heat_pumps``.
            config: Validated static configuration for this combined heat pump.
        """
        self.name = name
        self.config = config

        # Solver variables — populated by add_variables.
        self._T_tank: dict[int, Any] = {}
        self._dhw_mode: dict[int, Any] = {}
        self._sh_mode: dict[int, Any] = {}
        self._hp_on: dict[int, Any] = {}
        # _start[t] = 1 when the HP transitions from off to on at step t.
        # Only populated when min_run_steps > 1.
        self._start: dict[int, Any] = {}
        # _T_indoor[t] = indoor temperature variable at the end of step t, in °C.
        # Only populated when config.building_thermal is set.
        self._T_indoor: dict[int, Any] = {}

        self._dt: float = 0.25  # overwritten in add_variables from ctx.dt

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this combined heat pump.

        For each time step t in ctx.T:

        - ``_T_tank[t]``: Continuous variable representing the DHW water
          temperature at the end of step t in °C. Bounds are
          ``[min_temp_c − 5, setpoint_c + 5]`` — a small slack outside the hard
          temperature bounds prevents LP infeasibility from numerical noise. The
          hard bounds are added as explicit constraints in ``add_constraints``.

        - ``_dhw_mode[t]``: Binary. 1 = HP is heating the DHW tank at step t.

        - ``_sh_mode[t]``: Binary. 1 = HP is delivering space heating at step t.

        - ``_hp_on[t]``: Binary. 1 = HP is running in any mode at step t.
          Constrained to equal ``dhw_mode[t] + sh_mode[t]`` in ``add_constraints``.
          Kept as a separate variable (rather than a derived expression) so the
          minimum run constraint can reference it directly.

        If ``config.min_run_steps > 1``, for each t from 1 to T−1:

        - ``_start[t]``: Binary sentinel. 1 when the HP turns on at step t
          (was off at t−1, on at t). Used by the minimum run constraint.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt
        cfg = self.config

        for t in ctx.T:
            # DHW tank temperature — slack bounds, hard bounds added as constraints.
            self._T_tank[t] = ctx.solver.add_var(
                lb=cfg.min_temp_c - 5.0,
                ub=cfg.setpoint_c + 5.0,
            )
            self._dhw_mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            self._sh_mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            self._hp_on[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        if cfg.min_run_steps > 1:
            for t in range(1, len(ctx.T)):
                self._start[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        if cfg.building_thermal is not None:
            # Building thermal model: declare a continuous indoor temperature
            # variable at each step. Bounds enforce the comfort envelope directly.
            # The SH mode dynamics constraint links T_indoor to sh_mode[t].
            btm = cfg.building_thermal
            for t in ctx.T:
                self._T_indoor[t] = ctx.solver.add_var(
                    lb=btm.comfort_min_c,
                    ub=btm.comfort_max_c,
                )

    def add_constraints(
        self, ctx: ModelContext, inputs: CombiHeatPumpInputs
    ) -> None:
        """Add all MILP constraints for this combined heat pump.

        **Mutual exclusion**: At each step, the HP can operate in at most one
        mode. This reflects the physical limitation of a single refrigerant
        circuit:

            dhw_mode[t] + sh_mode[t] <= 1

        **On-indicator linkage**: ``hp_on[t]`` equals the sum of both mode
        binaries. Because the mutual exclusion constraint limits the sum to at
        most 1, ``hp_on[t]`` remains binary:

            hp_on[t] == dhw_mode[t] + sh_mode[t]

        **DHW tank dynamics**: At each step, the tank temperature evolves by
        passive cooling plus active heating in DHW mode. The same linear model
        as ``ThermalBoilerDevice``:

            T_tank[t] = T_tank[t−1] − cool_per_step + dhw_heat_rise × dhw_mode[t]

        At t=0, ``T_tank[−1]`` is replaced by ``inputs.current_temp_c``.

        **DHW hard bounds**:

            T_tank[t] >= min_temp_c
            T_tank[t] <= setpoint_c

        **SH total heat constraint** (only when ``heat_needed_kwh > 0``):

            Σ_t (elec_power_kw × cop_sh × dt × sh_mode[t]) >= heat_needed_kwh

        **Minimum run length** (only when ``min_run_steps > 1``): Applied to
        ``hp_on[t]``. Includes prevention of starts too close to the horizon end
        (same approach as ``SpaceHeatingDevice``):

            For t where t + min_run_steps > T:
                hp_on[t] <= hp_on[t−1]   (cannot start a new run near the end)
                hp_on[0] == 0             (special case for t=0)
            start[t] >= hp_on[t] - hp_on[t−1]
            start[t] <= hp_on[t]
            hp_on[t+τ] >= start[t]   for τ in 1 .. min_run_steps−1

        Args:
            ctx: The current solve context.
            inputs: Live state for this device. ``current_temp_c`` initialises
                the DHW tank; ``heat_needed_kwh`` sets the SH demand lower bound.
        """
        cfg = self.config
        n = len(ctx.T)

        # Pre-compute thermal parameters (pure Python floats — not solver variables).
        thermal_cap_kwh_per_k = cfg.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
        cool_per_step = cfg.cooling_rate_k_per_hour * ctx.dt
        # Temperature rise per step in DHW mode.
        dhw_heat_rise = cfg.elec_power_kw * cfg.cop_dhw * ctx.dt / thermal_cap_kwh_per_k

        for t in ctx.T:
            # --- Mutual exclusion ---
            # The HP has one compressor. DHW and SH modes cannot run simultaneously.
            ctx.solver.add_constraint(
                self._dhw_mode[t] + self._sh_mode[t] <= 1
            )

            # --- On-indicator linkage ---
            # hp_on[t] is the logical OR of both modes, encoded as their sum.
            # The mutual exclusion constraint above keeps the sum <= 1, so
            # hp_on[t] is always binary without needing a separate declaration.
            ctx.solver.add_constraint(
                self._hp_on[t] == self._dhw_mode[t] + self._sh_mode[t]
            )

            # --- DHW tank dynamics ---
            # Only dhw_mode contributes heat to the tank. When the HP is in SH
            # mode, the tank still cools at the standard rate — heat goes to the
            # floor circuit, not the tank.
            prior_temp: Any = inputs.current_temp_c if t == 0 else self._T_tank[t - 1]
            ctx.solver.add_constraint(
                self._T_tank[t]
                == prior_temp - cool_per_step + dhw_heat_rise * self._dhw_mode[t]
            )

            # --- DHW hard temperature bounds ---
            ctx.solver.add_constraint(self._T_tank[t] >= cfg.min_temp_c)
            ctx.solver.add_constraint(self._T_tank[t] <= cfg.setpoint_c)

        # --- Space heating constraint (degree-days or BTM) ---
        if cfg.building_thermal is not None:
            # BTM path: replace degree-days lower bound with indoor temperature
            # dynamics and comfort constraints. heat_needed_kwh is intentionally
            # ignored here \u2014 the comfort envelope drives SH scheduling.
            self._add_btm_sh_constraints(ctx, inputs)
        elif inputs.heat_needed_kwh > 0.0:
            sh_thermal_per_step = cfg.elec_power_kw * cfg.cop_sh * ctx.dt
            ctx.solver.add_constraint(
                sum(sh_thermal_per_step * self._sh_mode[t] for t in ctx.T)
                >= inputs.heat_needed_kwh
            )

        # --- Minimum run length ---
        self._add_min_run_constraints(ctx)

    def _add_btm_sh_constraints(
        self, ctx: ModelContext, inputs: CombiHeatPumpInputs
    ) -> None:
        """Add building thermal model dynamics for the SH mode.

        Tracks indoor temperature as a per-step state variable driven by the
        SH mode binary sh_mode[t]. DHW mode steps (sh_mode[t]=0) deliver no
        heat to the building; the building cools naturally during those steps.

        The dynamics equation for step t:

            T_indoor[t] = alpha * T_prev
                        + (dt / C) * P_heat_sh[t]
                        + beta_outdoor * T_outdoor[t]

        where:
            P_heat_sh[t] = elec_power_kw * cop_sh * sh_mode[t]
            alpha        = 1 - dt * L / C
            beta_outdoor = dt * L / C

        When sh_mode[t]=0 (HP off or in DHW mode), P_heat_sh=0 and the building
        cools toward outdoor temperature at the natural rate set by alpha.

        Comfort bounds [comfort_min_c, comfort_max_c] are enforced by the
        variable bounds declared in add_variables.

        Args:
            ctx: The current solve context.
            inputs: Live state. Must contain current_indoor_temp_c and
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
                "current_indoor_temp_c is None in CombiHeatPumpInputs."
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

        alpha = 1.0 - dt * L / C
        beta_outdoor = dt * L / C
        dt_over_C = dt / C

        # Coefficient: thermal power to temperature rise per step.
        # P_heat_sh[t] = elec_power_kw * cop_sh kW; multiply by dt_over_C for °C rise.
        p_heat_coeff = cfg.elec_power_kw * cfg.cop_sh  # kW per unit sh_mode step

        for t in ctx.T:
            if t == 0:
                t_prev = inputs.current_indoor_temp_c
            else:
                t_prev = self._T_indoor[t - 1]

            T_outdoor_t = inputs.outdoor_temp_forecast_c[t]

            # P_heat_sh is linear: elec_power_kw * cop_sh * sh_mode[t]
            p_heat_expr = p_heat_coeff * self._sh_mode[t]

            rhs = beta_outdoor * T_outdoor_t
            if t == 0:
                rhs += alpha * t_prev  # type: ignore[operator]
                ctx.solver.add_constraint(
                    self._T_indoor[t] - dt_over_C * p_heat_expr == rhs
                )
            else:
                ctx.solver.add_constraint(
                    self._T_indoor[t] - dt_over_C * p_heat_expr - alpha * t_prev == rhs
                )

    def _add_min_run_constraints(self, ctx: ModelContext) -> None:
        """Add minimum consecutive run length constraints based on hp_on[t].

        Prevents the compressor from being cycled on for fewer than
        ``min_run_steps`` consecutive steps. A mode switch (DHW→SH or SH→DHW)
        within a running block counts as continuous operation and does not
        trigger a new minimum-run window.

        The implementation mirrors ``SpaceHeatingDevice._add_min_run_constraints``
        exactly, operating on ``_hp_on[t]`` instead of the mode-specific binaries.

        Args:
            ctx: The current solve context.
        """
        cfg = self.config
        if cfg.min_run_steps <= 1:
            return

        n = len(ctx.T)

        # Prevent fresh starts near the end of the horizon where the minimum
        # run window would extend beyond T. Without this, the solver can start
        # a single-step run at the last step and satisfy the sentinel constraints
        # trivially (there are no future steps to constrain).
        for t in range(n):
            if t + cfg.min_run_steps > n:
                if t == 0:
                    ctx.solver.add_constraint(self._hp_on[0] == 0)
                else:
                    ctx.solver.add_constraint(
                        self._hp_on[t] <= self._hp_on[t - 1]
                    )

        # Step 0: if the HP is on at step 0, it must stay on for the following
        # min_run_steps − 1 steps (step 0 itself is already constrained above
        # when near the horizon end).
        for tau in range(1, cfg.min_run_steps):
            if tau < n:
                ctx.solver.add_constraint(self._hp_on[tau] >= self._hp_on[0])

        # Steps 1 … T−1: sentinel-based minimum run.
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

        The HP draws ``elec_power_kw`` whenever it is running, regardless of
        whether it is in DHW or SH mode. The sign convention follows the Device
        Protocol: positive = producing power, negative = consuming. The HP is
        always a consumer, so net_power is always <= 0.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for ``−elec_power_kw × hp_on[t]``.
        """
        return -self.config.elec_power_kw * self._hp_on[t]

    def objective_terms(self, t: int) -> list[Any]:
        """Return objective cost terms for time step t.

        The only optional term is the wear cost, which penalises electrical
        energy consumption to discourage unnecessary compressor cycling.

            wear_cost_eur_per_kwh × elec_power_kw × dt × hp_on[t]

        Args:
            t: Zero-based time step index.

        Returns:
            List of solver-expression terms to add to the minimisation objective.
            Empty when ``wear_cost_eur_per_kwh == 0.0``.
        """
        cfg = self.config
        if cfg.wear_cost_eur_per_kwh == 0.0:
            return []
        return [cfg.wear_cost_eur_per_kwh * cfg.elec_power_kw * self._dt * self._hp_on[t]]

    def terminal_soc_var(self, ctx: ModelContext) -> Any:
        """Return a solver expression representing stored DHW energy at horizon end.

        The terminal value credits the solver for leaving the DHW tank with
        thermal energy above the minimum temperature. Without this term a
        cost-minimising solver would drain the tank to ``min_temp_c`` at the
        last step because future re-heating costs are outside the horizon.

        The stored energy is expressed in kWh_electric by dividing by ``cop_dhw``
        — the COP the solver would use to re-heat the tank after the horizon.

            terminal_value = (T_tank[T−1] − min_temp_c) × thermal_cap / cop_dhw

        Units: kWh_electric (consistent with the electricity price terms in the
        objective, which are in EUR/kWh_electric).

        Args:
            ctx: The current solve context. Used to find the last step index.

        Returns:
            Solver expression for the DHW terminal value in kWh_electric.
        """
        cfg = self.config
        thermal_cap = cfg.volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K
        last_t = ctx.T[-1]
        # python-mip does not support division of solver expressions by a float.
        # Pre-compute the reciprocal and multiply instead.
        inv_cop_dhw = 1.0 / cfg.cop_dhw
        return (self._T_tank[last_t] - cfg.min_temp_c) * thermal_cap * inv_cop_dhw
