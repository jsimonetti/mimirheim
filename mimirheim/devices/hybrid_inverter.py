"""HybridInverterDevice — models a DC-coupled hybrid inverter in the MILP.

This module implements the Device Protocol for a hybrid inverter: a single
unit that integrates a PV MPPT input, a battery on the DC bus, and an AC grid
connection. The key structural difference from an AC-coupled battery plus a
separate PV device is the explicit DC bus power balance constraint: PV can
charge the battery directly without any AC round-trip, and both directions of
power flow share the inverter's conversion efficiency.

The module does not import from ``mimirheim.io``. Runtime inputs (initial SOC and
per-step PV forecast) are passed as a ``HybridInverterInputs`` argument to
``add_constraints``. All solver interactions go through ``ModelContext.solver``.
"""

from typing import Any

from mimirheim.config.schema import HybridInverterConfig
from mimirheim.core.bundle import HybridInverterInputs
from mimirheim.core.context import ModelContext


class HybridInverterDevice:
    """Models a DC-coupled hybrid inverter as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.hybrid_inverters``. The
    model builder creates one ``HybridInverterDevice`` per named inverter in the
    config, calls ``add_variables`` once, then calls ``add_constraints`` with
    the live SOC and PV forecast from the current ``SolveBundle``.

    The device exposes the following solver variables after ``add_variables``:

        pv_dc[t]          — PV DC power at the MPPT input, in kW.
        bat_charge_dc[t]  — DC power flowing from the DC bus into the battery
                            cells, in kW (DC bus side).
        bat_discharge_dc[t] — DC power flowing from the battery cells to the
                              DC bus, in kW (DC bus side).
        ac_to_dc[t]       — AC power drawn from the AC bus by the inverter,
                            in kW (AC bus side).
        dc_to_ac[t]       — AC power delivered to the AC bus by the inverter,
                            in kW (AC bus side).
        soc[t]            — Battery state of charge at the end of step t, in kWh.
        mode[t]           — Binary; 1 = battery is charging, 0 = discharging.
        inv_mode[t]       — Binary; 1 = inverter converts AC→DC, 0 = DC→AC.

    Attributes:
        name: Device name matching the key in ``config.hybrid_inverters``.
        config: Static hybrid inverter configuration.
    """

    def __init__(self, name: str, config: HybridInverterConfig) -> None:
        """Initialise the HybridInverterDevice.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.hybrid_inverters``.
            config: Validated static configuration for this hybrid inverter.
        """
        self.name = name
        self.config = config

        # Solver variables populated by add_variables.
        self.pv_dc: dict[int, Any] = {}
        self.bat_charge_dc: dict[int, Any] = {}
        self.bat_discharge_dc: dict[int, Any] = {}
        self.ac_to_dc: dict[int, Any] = {}
        self.dc_to_ac: dict[int, Any] = {}
        self.soc: dict[int, Any] = {}
        # mode[t] = 1 → battery charging mode; = 0 → battery discharging mode.
        self.mode: dict[int, Any] = {}
        # inv_mode[t] = 1 → inverter imports AC→DC; = 0 → inverter exports DC→AC.
        self.inv_mode: dict[int, Any] = {}
        # soc_low[t] = SOC deficit below optimal_lower_soc_kwh at step t, in kWh.
        # Populated only when optimal_lower_soc_kwh > min_soc_kwh.
        self._soc_low: dict[int, Any] = {}

        self._dt: float = 0.25  # set from ctx in add_variables

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MILP variables for this hybrid inverter.

        For every time step t in ctx.T:

        - ``pv_dc[t]``: PV DC power at the MPPT input in kW. Lower bound: 0
          (PV never consumes power). Upper bound: max_pv_kw, the physical peak
          capacity of the array. The forecast-based upper bound is applied in
          ``add_constraints`` as a per-step linear constraint.

        - ``bat_charge_dc[t]``: Power delivered from the DC bus to the battery
          cells, in kW. Lower bound: 0. Upper bound: max_charge_kw.

        - ``bat_discharge_dc[t]``: Power delivered from the battery cells to
          the DC bus, in kW. Lower bound: 0. Upper bound: max_discharge_kw.

        - ``ac_to_dc[t]``: Power drawn from the AC bus by the inverter in kW.
          After conversion at inverter_efficiency, this power arrives on the
          DC bus. Upper bound: max_charge_kw / inverter_efficiency (the AC
          import that saturates the DC bus charging capacity).

        - ``dc_to_ac[t]``: Power delivered to the AC bus by the inverter in
          kW. The DC bus must supply dc_to_ac / inverter_efficiency for each
          kW delivered to AC. Upper bound: (max_discharge_kw + max_pv_kw) ×
          inverter_efficiency (maximum possible AC output when both PV and
          battery discharge at maximum simultaneously).

        - ``soc[t]``: Battery state of charge at the end of step t in kWh.
          Bounds: [min_soc_kwh, capacity_kwh].

        - ``mode[t]``: Binary variable. 1 = battery is in charging mode; 0 =
          battery is in discharging mode. The Big-M guard in add_constraints
          uses this to prevent simultaneous charge and discharge.

        - ``inv_mode[t]``: Binary variable. 1 = inverter is in AC→DC (import)
          mode; 0 = inverter is in DC→AC (export) mode. The Big-M guard in
          add_constraints uses this to prevent simultaneous AC import and export.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt
        cfg = self.config

        # Upper bound for the AC import variable: the AC power that saturates
        # the battery's maximum DC charge power after inverter conversion.
        max_ac_import_kw = cfg.max_charge_kw / cfg.inverter_efficiency

        # Upper bound for the AC export variable: maximum DC output available
        # from both PV (at peak) and battery (at maximum discharge), converted
        # to AC. This is a conservative ceiling — the actual export depends on
        # the available PV and battery state at each step.
        max_ac_export_kw = (cfg.max_discharge_kw + cfg.max_pv_kw) * cfg.inverter_efficiency

        for t in ctx.T:
            # PV DC power (kW). The tight per-step upper bound (forecast clip)
            # is added as a constraint in add_constraints because it depends on
            # runtime inputs unavailable here.
            self.pv_dc[t] = ctx.solver.add_var(lb=0.0, ub=cfg.max_pv_kw)

            # Battery charge and discharge (DC bus side, in kW).
            self.bat_charge_dc[t] = ctx.solver.add_var(lb=0.0, ub=cfg.max_charge_kw)
            self.bat_discharge_dc[t] = ctx.solver.add_var(lb=0.0, ub=cfg.max_discharge_kw)

            # Inverter AC power (AC bus side, in kW).
            self.ac_to_dc[t] = ctx.solver.add_var(lb=0.0, ub=max_ac_import_kw)
            self.dc_to_ac[t] = ctx.solver.add_var(lb=0.0, ub=max_ac_export_kw)

            # Battery state of charge (kWh).
            self.soc[t] = ctx.solver.add_var(lb=cfg.min_soc_kwh, ub=cfg.capacity_kwh)

            # Binary: battery direction (1=charging, 0=discharging).
            self.mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

            # Binary: inverter direction (1=AC→DC import, 0=DC→AC export).
            self.inv_mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        # soc_low[t] is the SOC deficit below optimal_lower_soc_kwh at step t,
        # in kWh. It is zero when soc[t] >= optimal_lower_soc_kwh and equals
        # the deficit otherwise. Used by the soft lower-bound penalty in
        # objective_terms. Not created when optimal_lower_soc_kwh == 0 (the
        # default) to keep the variable count identical to the pre-plan-54
        # behaviour for most users.
        soc_low_ub = self.config.optimal_lower_soc_kwh - self.config.min_soc_kwh
        if soc_low_ub > 0.0:
            for t in ctx.T:
                self._soc_low[t] = ctx.solver.add_var(lb=0.0, ub=soc_low_ub)

    def terminal_soc_var(self, ctx: ModelContext) -> Any | None:
        """Return the solver variable for the battery SOC at the last step.

        Used by ``ObjectiveBuilder._terminal_soc_terms`` to attach a terminal
        value to stored energy. Without this, the solver treats kWh remaining
        at the end of the horizon as worthless and drains the battery every
        cycle.

        Args:
            ctx: The current solve context. Used to identify the last step.

        Returns:
            The solver variable ``soc[T-1]``, or ``None`` if ``add_variables``
            has not been called yet.
        """
        return self.soc.get(ctx.T[-1])

    def add_constraints(self, ctx: ModelContext, inputs: HybridInverterInputs) -> None:
        """Add all MILP constraints for this hybrid inverter.

        Constraints fall into five groups:

        **PV forecast clip**: At each step t, pv_dc[t] is bounded above by the
        per-step forecast value (clipped to max_pv_kw). This converts the
        static upper bound on the variable (max_pv_kw) to a tighter dynamic
        bound. The solver may curtail PV (set pv_dc < forecast) to avoid
        over-charging the battery or exporting past the grid limit.

        **DC bus power balance**: The core hybrid inverter constraint. At each
        step, all power flows on the DC bus must sum to zero:

            pv_dc[t]
            + bat_discharge_dc[t]
            + ac_to_dc[t] × η_inv           (AC→DC conversion)
            − bat_charge_dc[t]
            − dc_to_ac[t] / η_inv           (DC consumed to produce AC export)
            == 0

        This constraint couples PV, battery, and inverter in a way that is
        absent in AC-coupled systems. In particular, PV can directly charge
        the battery (pv_dc → bat_charge_dc) without any AC round-trip.

        **SOC dynamics**: Energy accounting for the battery across the horizon:

            soc[t] = soc[t−1]
                     + (bat_charge_dc[t] × η_bat_c
                        − bat_discharge_dc[t] / η_bat_d)
                     × dt

        bat_charge_dc is measured at the DC bus; only the fraction η_bat_c
        reaches the cells. bat_discharge_dc is the power on the DC bus; the
        cells must supply bat_discharge_dc / η_bat_d (η_bat_d < 1 means more
        cell energy is consumed than appears on the DC bus).

        For t=0, uses ``inputs.soc_kwh`` as the initial state.

        **Battery Big-M guard**: Prevents simultaneous charge and discharge.
        A binary variable ``mode[t]`` gates each direction:

            bat_charge_dc[t]    ≤ max_charge_kw    × mode[t]
            bat_discharge_dc[t] ≤ max_discharge_kw × (1 − mode[t])

        **Inverter Big-M guard**: Prevents simultaneous AC import and export.
        A binary variable ``inv_mode[t]`` gates each AC direction:

            ac_to_dc[t]  ≤ (max_charge_kw / η_inv)                    × inv_mode[t]
            dc_to_ac[t]  ≤ (max_discharge_kw + max_pv_kw) × η_inv     × (1 − inv_mode[t])

        Without this guard, an LP relaxation could simultaneously import and
        export at equal prices, which is physically impossible because a single
        inverter cannot convert in both directions at the same time.

        Args:
            ctx: The current solve context.
            inputs: Live battery SOC and per-step PV forecast for this
                inverter. ``inputs.pv_forecast_kw`` must have length equal to
                ``ctx.horizon``.
        """
        cfg = self.config
        η_inv = cfg.inverter_efficiency
        η_bat_c = cfg.battery_charge_efficiency
        η_bat_d = cfg.battery_discharge_efficiency

        # Precompute reciprocals to avoid solver-variable division (python-mip
        # expressions support multiplication by a scalar, not division).
        inv_η_inv = 1.0 / η_inv
        inv_η_bat_d = 1.0 / η_bat_d

        max_ac_import_kw = cfg.max_charge_kw * inv_η_inv
        max_ac_export_kw = (cfg.max_discharge_kw + cfg.max_pv_kw) * η_inv

        for t in ctx.T:
            # --- PV forecast clip ---
            # Restrict PV output to the per-step forecast, clipped to the hardware
            # peak. The solver may curtail below this value if DC bus surplus
            # cannot be stored or exported.
            pv_cap = min(inputs.pv_forecast_kw[t], cfg.max_pv_kw)
            ctx.solver.add_constraint(self.pv_dc[t] <= pv_cap)

            # --- DC bus power balance ---
            # All DC bus power sources must equal all DC bus power sinks.
            # Sources: PV, battery discharge (DC bus side), and AC→DC conversion.
            # Sinks: battery charge (DC bus side) and DC consumed to produce
            # the AC export.
            ctx.solver.add_constraint(
                self.pv_dc[t]
                + self.bat_discharge_dc[t]
                + self.ac_to_dc[t] * η_inv
                - self.bat_charge_dc[t]
                - inv_η_inv * self.dc_to_ac[t]
                == 0
            )

            # --- SOC dynamics ---
            # Energy stored in the cells increases by bat_charge_dc × η_bat_c
            # and decreases by bat_discharge_dc / η_bat_d per unit time.
            # The ratio (1 / η_bat_d) > 1 because the cells must release more
            # energy than appears on the DC bus due to discharge losses.
            if t == 0:
                ctx.solver.add_constraint(
                    self.soc[t]
                    == inputs.soc_kwh
                    + (
                        self.bat_charge_dc[t] * η_bat_c
                        - inv_η_bat_d * self.bat_discharge_dc[t]
                    )
                    * ctx.dt
                )
            else:
                ctx.solver.add_constraint(
                    self.soc[t]
                    == self.soc[t - 1]
                    + (
                        self.bat_charge_dc[t] * η_bat_c
                        - inv_η_bat_d * self.bat_discharge_dc[t]
                    )
                    * ctx.dt
                )

            # --- Battery Big-M guard ---
            # Prevents simultaneous charge and discharge. When mode[t]=1 the
            # battery charges (discharge is blocked by its bound dropping to 0).
            # When mode[t]=0 the battery discharges (charge bound drops to 0).
            # The Big-M values are the physical limits of each variable.
            ctx.solver.add_constraint(
                self.bat_charge_dc[t] <= cfg.max_charge_kw * self.mode[t]
            )
            ctx.solver.add_constraint(
                self.bat_discharge_dc[t] <= cfg.max_discharge_kw * (1 - self.mode[t])
            )

            # --- Inverter direction Big-M guard ---
            # A real inverter cannot convert in both directions simultaneously.
            # inv_mode[t]=1 opens the AC→DC path; inv_mode[t]=0 opens the DC→AC path.
            # Without this guard the LP relaxation could simultaneously import and
            # export, which is physically impossible in a single-stage inverter.
            ctx.solver.add_constraint(
                self.ac_to_dc[t] <= max_ac_import_kw * self.inv_mode[t]
            )
            ctx.solver.add_constraint(
                self.dc_to_ac[t] <= max_ac_export_kw * (1 - self.inv_mode[t])
            )

            # --- Soft SOC lower bound ---
            # soc_low[t] is the amount by which soc[t] falls below
            # optimal_lower_soc_kwh. Rearranging: soc_low[t] >= optimal - soc[t].
            # The solver will minimise soc_low through the penalty in
            # objective_terms, so it will only violate the soft bound when the
            # economic gain from discharging outweighs the penalty.
            if self._soc_low:
                ctx.solver.add_constraint(
                    self._soc_low[t] >= cfg.optimal_lower_soc_kwh - self.soc[t]
                )

            # --- Minimum charge power floor ---
            # When the battery is charging (mode[t]=1), the DC charge power must
            # be at least min_charge_kw. This models inverters that cannot operate
            # at arbitrarily low charge rates. The Big-M on the right pins the
            # constraint inactive when mode[t]=0 (discharging).
            if cfg.min_charge_kw is not None:
                ctx.solver.add_constraint(
                    self.bat_charge_dc[t] >= cfg.min_charge_kw * self.mode[t]
                )

            # --- Minimum discharge power floor ---
            # Symmetric to the charge floor: when the battery is discharging
            # (mode[t]=0, so 1 - mode[t]=1) the discharge power must be at least
            # min_discharge_kw. The Big-M pins the constraint inactive during charging.
            if cfg.min_discharge_kw is not None:
                ctx.solver.add_constraint(
                    self.bat_discharge_dc[t] >= cfg.min_discharge_kw * (1 - self.mode[t])
                )

        # --- Charge derating ---
        # At high SOC, many batteries cannot sustain peak charge power. This
        # block enforces a linear derating: charge power falls from max_charge_kw
        # at reduce_charge_above_soc_kwh to reduce_charge_min_kw at capacity_kwh.
        #
        # The constraint is: bat_charge_dc[t] <= slope_c * soc_prev + intercept_c
        # where soc_prev is the SOC at the start of step t. This is a linear
        # relationship between the SOC state variable and the charge power limit.
        #
        # Derivation of slope_c and intercept_c:
        #   At soc_prev = reduce_charge_above_soc_kwh: limit = max_charge_kw
        #   At soc_prev = capacity_kwh:                limit = reduce_charge_min_kw
        #   slope_c = (min_kw - max_kw) / (capacity - threshold)  [negative]
        #   intercept_c = max_kw - slope_c * threshold
        if cfg.reduce_charge_above_soc_kwh is not None and cfg.reduce_charge_min_kw is not None:
            slope_c = (cfg.reduce_charge_min_kw - cfg.max_charge_kw) / (
                cfg.capacity_kwh - cfg.reduce_charge_above_soc_kwh
            )
            rhs_c = cfg.max_charge_kw - slope_c * cfg.reduce_charge_above_soc_kwh
            for t in ctx.T:
                soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]
                ctx.solver.add_constraint(
                    self.bat_charge_dc[t] - slope_c * soc_prev <= rhs_c
                )

        # --- Discharge derating ---
        # At low SOC, the battery may not sustain peak discharge power. This
        # enforces a linear derating: discharge power falls from max_discharge_kw
        # at reduce_discharge_below_soc_kwh to reduce_discharge_min_kw at min_soc_kwh.
        #
        # slope_d is positive (power increases as SOC increases).
        #   At soc_prev = min_soc_kwh:                     limit = reduce_discharge_min_kw
        #   At soc_prev = reduce_discharge_below_soc_kwh:  limit = max_discharge_kw
        #   slope_d = (max_kw - min_kw) / (threshold - min_soc_kwh)
        #   intercept_d = max_kw - slope_d * threshold
        if (
            cfg.reduce_discharge_below_soc_kwh is not None
            and cfg.reduce_discharge_min_kw is not None
        ):
            slope_d = (cfg.max_discharge_kw - cfg.reduce_discharge_min_kw) / (
                cfg.reduce_discharge_below_soc_kwh - cfg.min_soc_kwh
            )
            rhs_d = cfg.max_discharge_kw - slope_d * cfg.reduce_discharge_below_soc_kwh
            for t in ctx.T:
                soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]
                ctx.solver.add_constraint(
                    self.bat_discharge_dc[t] - slope_d * soc_prev <= rhs_d
                )

    def net_power(self, t: int) -> Any:
        """Net AC power contribution to the home bus at step t, in kW.

        Positive means the device injects power to the AC bus (discharge or PV
        surplus export). Negative means the device draws power from the AC bus
        (battery charging from the grid).

        The grid's power balance uses this value: a positive contribution
        reduces (or eliminates) the need for grid import; a negative
        contribution requires grid import or other AC-side sources.

        Args:
            t: Zero-based time step index.

        Returns:
            Solver expression for ``dc_to_ac[t] − ac_to_dc[t]``.
        """
        return self.dc_to_ac[t] - self.ac_to_dc[t]

    def objective_terms(self, t: int) -> list[Any]:
        """Return objective cost terms for time step t.

        Two optional penalty terms may be included:

        **Wear cost**: penalises AC-side energy throughput (ac_to_dc + dc_to_ac)
        multiplied by the configured cost per kWh. Using AC power means the
        wear cost scales with the energy actually exchanged with the grid and
        home, capturing both battery degradation and inverter losses. Prior to
        plan 54 this used DC-side power; the change aligns the cost basis with
        the Battery device.

        **Soft SOC lower bound**: when ``soc_low_penalty_eur_per_kwh_h`` > 0,
        accrues a penalty proportional to the SOC deficit below
        ``optimal_lower_soc_kwh``. The penalty is denominated in
        EUR·kWh⁻¹·h⁻¹, so multiplying by ``_dt`` (hours per step) converts to
        EUR per step.

        Args:
            t: Zero-based time step index.

        Returns:
            List of zero to two solver expressions.
        """
        terms: list[Any] = []

        # Wear cost: penalises AC-side energy throughput.
        # ac_to_dc[t] is the AC power drawn from the grid/home for charging, in kW.
        # dc_to_ac[t] is the AC power delivered to the grid/home from discharge or PV, in kW.
        # Multiplying by _dt converts power (kW) to energy (kWh) for the step.
        if self.config.wear_cost_eur_per_kwh > 0.0:
            terms.append(
                self.config.wear_cost_eur_per_kwh
                * (self.ac_to_dc[t] + self.dc_to_ac[t])
                * self._dt
            )

        # Soft SOC penalty: penalises SOC deficit below optimal_lower_soc_kwh.
        # soc_low[t] holds the deficit in kWh. Multiplying by dt converts to kWh·h
        # (energy-time), consistent with the eur_per_kwh_h unit.
        if self._soc_low and self.config.soc_low_penalty_eur_per_kwh_h > 0.0:
            terms.append(
                self.config.soc_low_penalty_eur_per_kwh_h
                * self._soc_low[t]
                * self._dt
            )

        return terms
