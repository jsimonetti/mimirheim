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
        # active[t]: idle-state binary, populated only when both power floors
        # are set. See IMPLEMENTATION_DETAILS.md §8, subsection "Idle-state
        # binary (active[t])".
        self._active: dict[int, Any] = {}

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

        # active[t] is added only when both power floors are configured; see
        # IMPLEMENTATION_DETAILS.md §8, subsection "Idle-state binary
        # (active[t])".
        if (
            self.config.min_charge_kw is not None
            and self.config.min_discharge_kw is not None
        ):
            for t in ctx.T:
                self._active[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

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

        Adds, per step: a PV forecast clip (``pv_dc[t]`` bounded by the
        per-step forecast, clipped to ``max_pv_kw`` — the solver may curtail
        below this to avoid over-charging the battery or exceeding the grid
        export limit), the DC bus power balance, the battery direction Big-M
        guard, and the inverter direction Big-M guard. See
        IMPLEMENTATION_DETAILS.md §8, subsection "Hybrid inverter DC bus power
        balance", for the full constraint equations and why two independent
        direction binaries (battery ``mode[t]`` and inverter ``inv_mode[t]``)
        are needed.

        SOC dynamics: ``soc[t] = soc[t-1] + (bat_charge_dc[t] * eff_bat_charge
        - bat_discharge_dc[t] / eff_bat_discharge) * dt``, using
        ``inputs.soc_kwh`` as the initial state at ``t=0``.

        Args:
            ctx: The current solve context.
            inputs: Live battery SOC and per-step PV forecast for this
                inverter. ``inputs.pv_forecast_kw`` must have length equal to
                ``ctx.horizon``.
        """
        cfg = self.config
        # Naming: eff_* are efficiencies in (0, 1]; one_over_eff_* are their
        # reciprocals, precomputed because they appear inside constraints built
        # once per step. Note that "inv" elsewhere in this module means the
        # inverter (inv_mode, ac_to_dc), never "inverse".
        eff_inv = cfg.inverter_efficiency
        eff_bat_charge = cfg.battery_charge_efficiency
        eff_bat_discharge = cfg.battery_discharge_efficiency

        # Precompute reciprocals to avoid solver-variable division (python-mip
        # expressions support multiplication by a scalar, not division).
        one_over_eff_inv = 1.0 / eff_inv
        one_over_eff_bat_discharge = 1.0 / eff_bat_discharge

        max_ac_import_kw = cfg.max_charge_kw * one_over_eff_inv
        max_ac_export_kw = (cfg.max_discharge_kw + cfg.max_pv_kw) * eff_inv

        for t in ctx.T:
            # PV forecast clip: pv_dc[t] bounded by the per-step forecast,
            # clipped to the hardware peak.
            pv_cap = min(inputs.pv_forecast_kw[t], cfg.max_pv_kw)
            ctx.solver.add_constraint(self.pv_dc[t] <= pv_cap)

            # DC bus power balance. See IMPLEMENTATION_DETAILS.md §8,
            # subsection "Hybrid inverter DC bus power balance".
            ctx.solver.add_constraint(
                self.pv_dc[t]
                + self.bat_discharge_dc[t]
                + self.ac_to_dc[t] * eff_inv
                - self.bat_charge_dc[t]
                - one_over_eff_inv * self.dc_to_ac[t]
                == 0
            )

            # SOC dynamics: same efficiency asymmetry as Battery (see
            # IMPLEMENTATION_DETAILS.md §8, subsection "Piecewise efficiency
            # (battery and EV)").
            if t == 0:
                ctx.solver.add_constraint(
                    self.soc[t]
                    == inputs.soc_kwh
                    + (
                        self.bat_charge_dc[t] * eff_bat_charge
                        - one_over_eff_bat_discharge * self.bat_discharge_dc[t]
                    )
                    * ctx.dt
                )
            else:
                ctx.solver.add_constraint(
                    self.soc[t]
                    == self.soc[t - 1]
                    + (
                        self.bat_charge_dc[t] * eff_bat_charge
                        - one_over_eff_bat_discharge * self.bat_discharge_dc[t]
                    )
                    * ctx.dt
                )

            # Battery direction Big-M guard (mode[t]): same pattern as Battery.
            ctx.solver.add_constraint(
                self.bat_charge_dc[t] <= cfg.max_charge_kw * self.mode[t]
            )
            ctx.solver.add_constraint(
                self.bat_discharge_dc[t] <= cfg.max_discharge_kw * (1 - self.mode[t])
            )

            # Inverter direction Big-M guard (inv_mode[t]): a second,
            # independent direction binary — see IMPLEMENTATION_DETAILS.md §8,
            # subsection "Hybrid inverter DC bus power balance".
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

            # Minimum operating power floors: see IMPLEMENTATION_DETAILS.md §8,
            # subsection "Idle-state binary (active[t])".
            if t in self._active:
                ctx.solver.add_constraint(
                    self.bat_charge_dc[t] <= cfg.max_charge_kw * self._active[t]
                )
                ctx.solver.add_constraint(
                    self.bat_discharge_dc[t] <= cfg.max_discharge_kw * self._active[t]
                )
                ctx.solver.add_constraint(
                    self.bat_charge_dc[t]
                    >= cfg.min_charge_kw * (self.mode[t] + self._active[t] - 1)
                )
                ctx.solver.add_constraint(
                    self.bat_discharge_dc[t]
                    >= cfg.min_discharge_kw * (self._active[t] - self.mode[t])
                )
            else:
                if cfg.min_charge_kw is not None:
                    ctx.solver.add_constraint(
                        self.bat_charge_dc[t] >= cfg.min_charge_kw * self.mode[t]
                    )
                if cfg.min_discharge_kw is not None:
                    ctx.solver.add_constraint(
                        self.bat_discharge_dc[t]
                        >= cfg.min_discharge_kw * (1 - self.mode[t])
                    )

        # Charge derating near full: same two-point linear model as Battery.
        # See IMPLEMENTATION_DETAILS.md §8, subsection "Power derating near
        # SOC extremes".
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

        # Discharge derating near empty: mirror image of the charge derating
        # above.
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
