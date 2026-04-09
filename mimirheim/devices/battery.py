"""Battery device — models a DC-coupled residential battery in the MILP.

This module implements the Device Protocol for a battery. It declares MIP
variables for each charge and discharge segment and each time step, adds SOC
tracking constraints, enforces the simultaneous charge/discharge guard, and
contributes a wear cost term to the objective.

This module does not import from ``mimirheim.io``. It receives its runtime state
(current SOC) as a ``BatteryInputs`` argument to ``add_constraints`` and reads
its static parameters from ``BatteryConfig``. All solver interactions go through
``ModelContext.solver`` (a ``SolverBackend``); ``python-mip`` is never imported here.
"""

from typing import Any

from mimirheim.config.schema import BatteryConfig
from mimirheim.core.bundle import BatteryInputs
from mimirheim.core.context import ModelContext


class Battery:
    """Models a DC-coupled residential battery as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.batteries``. The model
    builder creates one ``Battery`` per named battery in the config, calls
    ``add_variables`` once, then calls ``add_constraints`` with the live SOC
    from MQTT.

    Attributes:
        name: Device name matching the key in ``config.batteries``. Used by
            the power balance assembler and the MQTT publisher.
        config: Static battery configuration.
        charge_seg: Maps ``(t, i)`` to the solver variable for the power
            delivered to the battery via charge segment ``i`` at step ``t``,
            in kW. Populated by ``add_variables``.
        discharge_seg: Maps ``(t, i)`` to the solver variable for the power
            drawn from the battery via discharge segment ``i`` at step ``t``,
            in kW. Populated by ``add_variables``.
        soc: Maps ``t`` to the solver variable for the battery state of charge
            at the end of step ``t``, in kWh. Populated by ``add_variables``.
        mode: Maps ``t`` to the binary mode variable at step ``t``.
            1 = charging, 0 = discharging. Populated by ``add_variables``.
    """

    def __init__(self, name: str, config: BatteryConfig) -> None:
        """Initialise the Battery device.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.batteries``.
            config: Validated static configuration for this battery.
        """
        self.name = name
        self.config = config
        self.charge_seg: dict[tuple[int, int], Any] = {}
        self.discharge_seg: dict[tuple[int, int], Any] = {}
        self.soc: dict[int, Any] = {}
        self.mode: dict[int, Any] = {}
        # Populated only when optimal_lower_soc_kwh > min_soc_kwh.
        # soc_low[t] represents the SOC deficit below the optimal lower level at
        # step t, in kWh. Zero when SOC >= optimal_lower_soc_kwh.
        self._soc_low: dict[int, Any] = {}
        self._dt: float = 0.25  # set from ctx in add_variables

        # SOS2 piecewise-linear efficiency model fields.
        # Populated when config.charge_efficiency_curve is not None.
        self._use_sos2: bool = config.charge_efficiency_curve is not None
        # Weight solver variables keyed by (t, s) where s is breakpoint index.
        self._w_charge: dict[tuple[int, int], Any] = {}
        self._w_discharge: dict[tuple[int, int], Any] = {}
        # Precomputed linear expressions (solver objects) for AC and DC power.
        # charge_ac_kw[t] = Σ_s (w_charge[t, s] × P_c[s])
        # charge_dc_kw[t] = Σ_s (w_charge[t, s] × P_c[s] × η_c[s])
        self._charge_ac_expr: dict[int, Any] = {}
        self._charge_dc_expr: dict[int, Any] = {}
        self._discharge_ac_expr: dict[int, Any] = {}
        self._discharge_dc_expr: dict[int, Any] = {}

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MIP variables for this battery.

        For each time step ``t`` in ``ctx.T`` and each charge segment ``i``:

        - ``charge_seg[t, i]``: Power delivered to the battery via segment ``i``
          in kW. Lower bound: 0 (non-negative power flow). Upper bound:
          ``segment.power_max_kw`` — the maximum power the segment can carry.
          The sum of all segment upper bounds equals the maximum charge power.

        - ``discharge_seg[t, i]``: Power drawn from the battery via segment ``i``
          in kW. Same bound structure as charge. The SOC update applies a
          per-segment efficiency loss in the discharge direction.

        For each time step ``t``:

        - ``soc[t]``: State of charge at the end of step ``t``, in kWh.
          Lower bound: ``config.min_soc_kwh`` — the minimum depth of discharge
          allowed to protect battery longevity. Upper bound:
          ``config.capacity_kwh`` — the usable cell capacity. Without these
          bounds, the solver would discharge below safe limits or charge beyond
          the physical maximum.

        - ``mode[t]``: Binary variable. 1 = charging is permitted; 0 =
          discharging is permitted. Used in the Big-M simultaneous
          charge/discharge guard added in ``add_constraints``. The guard is
          always applied — it is a mathematical necessity, not a hardware
          setting.

        Args:
            ctx: The current solve context. Variables are registered on
                ``ctx.solver``.
        """
        self._dt = ctx.dt

        if self._use_sos2:
            # SOS2 piecewise-linear efficiency model.
            # For each direction, introduce one weight variable per breakpoint.
            # The SOS2 constraint (added here via add_sos2) enforces that the
            # operating point lies on exactly one linear segment of the curve.
            charge_curve = self.config.charge_efficiency_curve
            discharge_curve = self.config.discharge_efficiency_curve

            for t in ctx.T:
                # --- Charge weights ---
                # w_charge[t, s] is the weight placed on breakpoint s by the solver.
                # Two adjacent weights can be nonzero (via the SOS2 constraint);
                # all others are driven to zero. The operating AC power and DC
                # power are the convex combinations Σ_s (w_s × P_s) and
                # Σ_s (w_s × P_s × η_s) respectively.
                w_c = [ctx.solver.add_var(lb=0.0, ub=1.0) for _ in charge_curve]
                ctx.solver.add_sos2(w_c, [bp.power_kw for bp in charge_curve])
                for s, var in enumerate(w_c):
                    self._w_charge[t, s] = var

                # Precompute the charge AC power expression (kW from the AC bus).
                ac_c: Any = 0
                for s, bp in enumerate(charge_curve):
                    if bp.power_kw > 0.0:
                        ac_c = ac_c + w_c[s] * bp.power_kw
                self._charge_ac_expr[t] = ac_c

                # Precompute the charge DC power expression (kW stored in the cells).
                # DC power = AC power × efficiency at this breakpoint.
                dc_c: Any = 0
                for s, bp in enumerate(charge_curve):
                    if bp.power_kw > 0.0:
                        dc_c = dc_c + w_c[s] * (bp.power_kw * bp.efficiency)
                self._charge_dc_expr[t] = dc_c

                # --- Discharge weights ---
                w_d = [ctx.solver.add_var(lb=0.0, ub=1.0) for _ in discharge_curve]
                ctx.solver.add_sos2(w_d, [bp.power_kw for bp in discharge_curve])
                for s, var in enumerate(w_d):
                    self._w_discharge[t, s] = var

                # Discharge AC power expression (kW at the DC bus output).
                ac_d: Any = 0
                for s, bp in enumerate(discharge_curve):
                    if bp.power_kw > 0.0:
                        ac_d = ac_d + w_d[s] * bp.power_kw
                self._discharge_ac_expr[t] = ac_d

                # Discharge DC power expression (kW drawn from the cells).
                # DC power drawn = AC power / efficiency (more energy leaves cells
                # than appears at the bus when efficiency < 1).
                dc_d: Any = 0
                for s, bp in enumerate(discharge_curve):
                    if bp.power_kw > 0.0:
                        dc_d = dc_d + w_d[s] * (bp.power_kw / bp.efficiency)
                self._discharge_dc_expr[t] = dc_d

                self.soc[t] = ctx.solver.add_var(
                    lb=self.config.min_soc_kwh,
                    ub=self.config.capacity_kwh,
                )
                self.mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
        else:
            # Stacked-segment model (existing behaviour).
            for t in ctx.T:
                for i, seg in enumerate(self.config.charge_segments):
                    # charge_seg[t, i] is the power flowing into the battery via
                    # efficiency segment i at time step t, in kilowatts. Each
                    # segment covers a power range [0, seg.power_max_kw] with a
                    # fixed efficiency. Using multiple segments approximates the
                    # real curve where efficiency varies with power level.
                    self.charge_seg[t, i] = ctx.solver.add_var(lb=0.0, ub=seg.power_max_kw)

                for i, seg in enumerate(self.config.discharge_segments):
                    # discharge_seg[t, i] is the power drawn from the battery via
                    # segment i at time step t, in kilowatts. The SOC update
                    # divides by segment efficiency (efficiency < 1 means more
                    # energy is extracted from the cells than appears at the DC
                    # bus — conversion loss).
                    self.discharge_seg[t, i] = ctx.solver.add_var(lb=0.0, ub=seg.power_max_kw)

                # soc[t] is the state of charge at the end of time step t, in
                # kilowatt-hours. The bounds enforce the operating window:
                # [min_soc_kwh, capacity_kwh]. Without the lower bound the solver
                # would discharge below the manufacturer's recommended depth of
                # discharge, accelerating cell degradation. Without the upper bound
                # the solver would overcharge beyond the physical cell capacity.
                self.soc[t] = ctx.solver.add_var(
                    lb=self.config.min_soc_kwh,
                    ub=self.config.capacity_kwh,
                )

                # mode[t] is a binary variable that encodes the charging direction
                # at time step t. 1 means charging is permitted; 0 means discharging
                # is permitted. It is used in the Big-M constraints below to prevent
                # the LP from simultaneously charging and discharging to exploit any
                # efficiency asymmetry as free energy. The guard is always applied —
                # omitting it would produce physically meaningless solutions.
                self.mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

        # Add soc_low[t] variables only when an optimal lower SOC is configured.
        # Skipping this in the default case (optimal = 0) keeps variable count
        # identical to the pre-plan-21 behaviour.
        soc_low_ub = self.config.optimal_lower_soc_kwh - self.config.min_soc_kwh
        if soc_low_ub > 0.0:
            for t in ctx.T:
                # soc_low[t] is the SOC deficit below optimal_lower_soc_kwh at
                # step t, in kWh. When soc[t] >= optimal_lower_soc_kwh the
                # deficit constraint (added in add_constraints) is inactive and
                # soc_low[t] = 0 at its lower bound. When soc[t] <
                # optimal_lower_soc_kwh the constraint forces soc_low[t] to
                # equal the deficit.
                #
                # Upper bound: the largest possible deficit is when soc[t] ==
                # min_soc_kwh (the absolute floor), giving a deficit of
                # optimal_lower_soc_kwh - min_soc_kwh.
                self._soc_low[t] = ctx.solver.add_var(lb=0.0, ub=soc_low_ub)

    def add_constraints(self, ctx: ModelContext, inputs: BatteryInputs) -> None:
        """Add SOC tracking and mode-guard constraints.

        This method must be called after ``add_variables`` and requires a fresh
        ``BatteryInputs`` reading with a current SOC value.

        **SOC update constraint** (for each ``t`` in ``ctx.T``):

        The SOC at the end of step ``t`` equals the SOC at the start (which is
        the SOC at the end of step ``t-1``, or ``inputs.soc_kwh`` for ``t=0``)
        plus energy stored minus energy drawn:

        .. code-block::

            soc[t] = soc[t-1]
                     + Σ_i (seg_i.efficiency × charge_seg[t, i] × dt)
                     - Σ_i ((1 / seg_i.efficiency) × discharge_seg[t, i] × dt)

        The asymmetry is intentional: in the charge direction, efficiency < 1
        means less energy reaches the cells than is drawn from the DC bus. In
        the discharge direction, efficiency < 1 means more energy leaves the
        cells than appears at the DC bus.

        **Simultaneous charge/discharge guard** (Big-M, for each ``t``):

        The binary ``mode[t]`` forces the solver to choose exactly one direction
        per step. Without this guard, the LP can charge and discharge
        simultaneously to exploit any efficiency difference as free energy —
        a physically impossible result that inflates the objective.

        .. code-block::

            total_charge[t]    ≤ max_charge_kw    × mode[t]
            total_discharge[t] ≤ max_discharge_kw × (1 − mode[t])

        Args:
            ctx: The current solve context.
            inputs: Validated live battery state from MQTT, providing the
                initial SOC used at ``t=0``.
        """
        max_charge_kw = self._max_charge_kw()
        max_discharge_kw = self._max_discharge_kw()

        for t in ctx.T:
            # --- Build the energy stored and energy drawn expressions ---

            if self._use_sos2:
                # SOS2 model: convex combination constraint enforces sum of weights = 1.
                # This ensures the solver interpolates on exactly one segment rather
                # than placing fractional weight on multiple non-adjacent breakpoints.
                charge_curve = self.config.charge_efficiency_curve
                discharge_curve = self.config.discharge_efficiency_curve
                ctx.solver.add_constraint(
                    sum(self._w_charge[t, s] for s in range(len(charge_curve))) == 1.0
                )
                ctx.solver.add_constraint(
                    sum(self._w_discharge[t, s] for s in range(len(discharge_curve))) == 1.0
                )
                # energy_stored = Σ_s (w_s × P_s × η_s) × dt  (DC power into cells)
                energy_stored = self._charge_dc_expr[t] * ctx.dt
                # energy_drawn = Σ_s (w_s × P_s / η_s) × dt  (DC power out of cells)
                energy_drawn = self._discharge_dc_expr[t] * ctx.dt
            else:
                # Stacked-segment model (original).
                # energy_stored[t] = Σ_i (seg.efficiency × charge_seg[t, i] × dt)
                # This is the net energy added to the cells after accounting for
                # charger losses. For a single segment with efficiency=0.95 and
                # 1 kW of charge power over a 0.25 h step: 0.95 × 1 × 0.25 = 0.2375 kWh.
                energy_stored = sum(
                    seg.efficiency * self.charge_seg[t, i] * ctx.dt
                    for i, seg in enumerate(self.config.charge_segments)
                )

                # energy_drawn[t] = Σ_i ((1 / seg.efficiency) × discharge_seg[t, i] × dt)
                # This is the energy that leaves the cells. For efficiency=0.95,
                # discharging at 1 kW at the DC bus requires (1 / 0.95) ≈ 1.053 kW
                # to leave the cells — the rest is lost as heat in the inverter.
                energy_drawn = sum(
                    (1.0 / seg.efficiency) * self.discharge_seg[t, i] * ctx.dt
                    for i, seg in enumerate(self.config.discharge_segments)
                )

            # --- SOC update ---
            # The SOC at the start of step t=0 is inputs.soc_kwh (a constant
            # from the most recent MQTT reading). For t > 0, it is the solver
            # variable soc[t-1].
            soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]

            # soc[t] = soc_prev + energy_stored - energy_drawn
            # Expressed as an equality constraint: soc[t] - energy_stored + energy_drawn == soc_prev
            ctx.solver.add_constraint(
                self.soc[t] - energy_stored + energy_drawn == soc_prev
            )

            # --- Simultaneous charge/discharge guard (Big-M) ---
            # charge_ac[t] <= max_charge_kw * mode[t]
            # If mode[t] = 0 (discharging), this forces charge to 0.
            # If mode[t] = 1 (charging), the bound is max_charge_kw (non-binding
            # since charge variables are individually bounded by segment/curve limits).
            # max_charge_kw is the Big-M coefficient — it must be at least as large
            # as the largest possible charge power for the constraint to be tight.
            ctx.solver.add_constraint(
                self.charge_ac_kw(t) <= max_charge_kw * self.mode[t]
            )
            ctx.solver.add_constraint(
                self.discharge_ac_kw(t) <= max_discharge_kw * (1 - self.mode[t])
            )

            # Minimum operating power floors (Plan 38C).
            #
            # When the battery is actively charging (mode[t]=1) or discharging
            # (mode[t]=0), some hardware cannot safely operate below a threshold
            # — for example, a DC-coupled inverter with a minimum PWM duty cycle.
            # Below this threshold the inverter may cut out or behave erratically.
            #
            # The floor is only applied when mode[t] selects the relevant direction:
            #   - charge floor: charge_ac_kw(t) >= min_charge_kw * mode[t]
            #     When mode[t]=0 (discharging) the right-hand side collapses to 0,
            #     so the constraint is trivially satisfied and imposes no lower bound
            #     on the charge power (which is already forced to zero by the Big-M above).
            #   - discharge floor: discharge_ac_kw(t) >= min_discharge_kw * (1 - mode[t])
            #     Same pattern: floor is active only when mode[t]=0 (discharging).
            #
            # The discharge floor is applied to whichever discharge power expression
            # the current model uses (stacked-segment or SOS2 piecewise-linear). All
            # BatteryConfig instances are required by schema validation to configure a
            # discharge model, so this constraint is always reachable.
            if self.config.min_charge_kw is not None:
                ctx.solver.add_constraint(
                    self.charge_ac_kw(t) >= self.config.min_charge_kw * self.mode[t]
                )
            if self.config.min_discharge_kw is not None:
                ctx.solver.add_constraint(
                    self.discharge_ac_kw(t)
                    >= self.config.min_discharge_kw * (1 - self.mode[t])
                )

        # Soft lower SOC bound — only when optimal_lower_soc_kwh is configured.
        #
        # The variable soc_low[t] is defined in add_variables as:
        #   lb=0, ub=(optimal_lower_soc_kwh - min_soc_kwh)
        #
        # The constraint below forces:
        #   soc_low[t] >= optimal_lower_soc_kwh - soc[t]
        #
        # Combined with the non-negativity bound on soc_low[t], this models:
        #   soc_low[t] = max(0, optimal_lower_soc_kwh - soc[t])
        #
        # This is a soft bound, not a hard floor: the solver may still dispatch
        # the battery below optimal_lower_soc_kwh when the price spread justifies
        # the penalty cost. Removing this constraint would allow soc_low[t] to
        # remain at 0 regardless of actual SOC, eliminating the penalty entirely.
        if self._soc_low:
            for t in ctx.T:
                ctx.solver.add_constraint(
                    self._soc_low[t] >= self.config.optimal_lower_soc_kwh - self.soc[t]
                )

        # Power derating near SOC extremes.
        #
        # Real inverters reduce charge power as the battery approaches full capacity
        # and reduce discharge power as it approaches minimum SOC. Both reductions
        # are approximately linear in the SOC.
        #
        # The constraints below implement this linearity directly in the LP. Each
        # constraint is safe to add unconditionally: when the SOC is outside the
        # derated region, the right-hand side exceeds the maximum power from the
        # segment bounds, so the constraint is slack and has no effect.

        if self.config.reduce_charge_above_soc_kwh is not None:
            # Two-point linear function:
            #   point A: (soc = reduce_charge_above_soc_kwh, power = max_charge_kw)
            #   point B: (soc = capacity_kwh,                power = reduce_charge_min_kw)
            #
            # slope_c = (reduce_charge_min_kw - max_charge_kw)
            #           / (capacity_kwh - reduce_charge_above_soc_kwh)
            #
            # slope_c is always negative (min < max, and denominator > 0).
            #
            # The derating bound is evaluated against the START-of-step SOC
            # (soc_prev), because that is what the inverter observes when deciding
            # how much charge power to allow. Using the end-of-step SOC (soc[t])
            # would create a circular dependency between the power decision and the
            # resulting SOC that, while still linear and valid in LP, would not
            # match the physically intended behaviour.
            #
            # Rearranging power_limit(soc_prev) >= charge_total into LP form:
            #   charge_total[t] - slope_c * soc_prev <= max_charge_kw - slope_c * reduce_charge_above_soc_kwh
            #
            # When soc_prev <= reduce_charge_above_soc_kwh, the RHS >= max_charge_kw,
            # so the segment-bound upper limits already dominate and this adds nothing.
            slope_c = (
                (self.config.reduce_charge_min_kw - max_charge_kw)
                / (self.config.capacity_kwh - self.config.reduce_charge_above_soc_kwh)
            )
            rhs_c = max_charge_kw - slope_c * self.config.reduce_charge_above_soc_kwh
            for t in ctx.T:
                soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]
                ctx.solver.add_constraint(
                    self.charge_ac_kw(t) - slope_c * soc_prev <= rhs_c
                )

        if self.config.reduce_discharge_below_soc_kwh is not None:
            # Two-point linear function:
            #   point A: (soc = reduce_discharge_below_soc_kwh, power = max_discharge_kw)
            #   point B: (soc = min_soc_kwh,                    power = reduce_discharge_min_kw)
            #
            # slope_d = (reduce_discharge_min_kw - max_discharge_kw)
            #           / (reduce_discharge_below_soc_kwh - min_soc_kwh)
            #
            # slope_d is always negative (min < max, and denominator > 0).
            #
            # As with charge derating, the bound is applied to the start-of-step
            # SOC (soc_prev) for physical correctness. The inverter caps discharge
            # power based on the SOC it observes before the step begins.
            #
            # LP form:
            #   discharge_total[t] + slope_d * soc_prev <= max_discharge_kw + slope_d * reduce_discharge_below_soc_kwh
            #
            # When soc_prev >= reduce_discharge_below_soc_kwh, the RHS >= max_discharge_kw,
            # so the segment bounds dominate and this constraint is slack.
            slope_d = (
                (self.config.reduce_discharge_min_kw - max_discharge_kw)
                / (self.config.reduce_discharge_below_soc_kwh - self.config.min_soc_kwh)
            )
            rhs_d = max_discharge_kw + slope_d * self.config.reduce_discharge_below_soc_kwh
            for t in ctx.T:
                soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]
                ctx.solver.add_constraint(
                    self.discharge_ac_kw(t) + slope_d * soc_prev <= rhs_d
                )

    def set_external_mode(self, mode_vars: dict[int, Any]) -> None:
        """Replace per-step mode variables with externally-supplied shared ones.

        Called by ``build_and_solve`` when two or more batteries are present,
        to enforce a shared system charge/discharge direction: all batteries
        must charge or discharge in the same step. This prevents energy from
        circulating between batteries (A discharges while B charges) with no
        net gain to the system.

        The shared binary ``bat_system_mode[t]`` has the same semantics as the
        per-device mode:

        - 1 = all batteries are in the charging direction this step.
        - 0 = all batteries are in the discharging direction this step.

        The per-device mode variables created in ``add_variables`` become
        unused (they are unconstrained free binaries in the solver model).
        This is a negligible overhead: the solver sets them arbitrarily
        without affecting the objective or feasibility.

        This method must be called after ``add_variables`` and before
        ``add_constraints``. Calling it after ``add_constraints`` has no
        effect on already-added constraints.

        Args:
            mode_vars: Dict mapping step index ``t`` to the shared binary
                solver variable for that step.
        """
        self.mode = dict(mode_vars)

    def net_power(self, t: int) -> Any:
        """Return the net power expression at time step ``t``.

        Net power is defined as total discharge minus total charge. A positive
        value means the battery is producing power (discharging to the home
        DC bus); a negative value means the battery is consuming power
        (charging from the DC bus). This sign convention matches the system
        power balance constraint in ``build_and_solve()``.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            A linear expression representing net power in kW.
        """
        return self.discharge_ac_kw(t) - self.charge_ac_kw(t)

    def charge_ac_kw(self, t: int) -> Any:
        """Return the AC charge power expression at step ``t``, in kW.

        In the SOS2 model, this is the precomputed convex-combination expression
        ``Σ_s (w_charge[t, s] × P_c[s])``. In the stacked-segment model, it is
        the sum of all charge-segment variables.

        Args:
            t: Time step index.

        Returns:
            A solver expression (or 0 if no power is possible).
        """
        if self._use_sos2:
            return self._charge_ac_expr[t]
        return sum(
            self.charge_seg[t, i]
            for i in range(len(self.config.charge_segments))
        )

    def discharge_ac_kw(self, t: int) -> Any:
        """Return the AC discharge power expression at step ``t``, in kW.

        Args:
            t: Time step index.

        Returns:
            A solver expression (or 0 if no power is possible).
        """
        if self._use_sos2:
            return self._discharge_ac_expr[t]
        return sum(
            self.discharge_seg[t, i]
            for i in range(len(self.config.discharge_segments))
        )

    def _max_charge_kw(self) -> float:
        """Return the maximum charge power in kW from the configured model.

        In the SOS2 model, this is the last breakpoint's power_kw (the maximum
        AC power on the curve). In the stacked-segment model, it is the sum of
        all segment upper bounds.
        """
        if self._use_sos2:
            return self.config.charge_efficiency_curve[-1].power_kw
        return sum(s.power_max_kw for s in self.config.charge_segments)

    def _max_discharge_kw(self) -> float:
        """Return the maximum discharge power in kW from the configured model."""
        if self._use_sos2:
            return self.config.discharge_efficiency_curve[-1].power_kw
        return sum(s.power_max_kw for s in self.config.discharge_segments)

    def terminal_soc_var(self, ctx: ModelContext) -> Any | None:
        """Return the solver variable for the battery's state of charge at the last step.

        This variable is used by the objective builder to attach a terminal
        value to stored energy. Without a terminal value, the solver treats
        kWh remaining at the end of the horizon as worthless and will drain
        the battery whenever there is any positive export price.

        The terminal value attached to this variable in the objective is the
        average import price over the horizon — the expected cost of
        re-acquiring 1 kWh after the horizon ends.

        Args:
            ctx: The current solve context. Used to identify the last step.

        Returns:
            The solver variable ``soc[T-1]``, or ``None`` if ``add_variables``
            has not been called yet.
        """
        return self.soc.get(ctx.T[-1])

    def objective_terms(self, t: int) -> Any:
        """Return cost contributions to the objective at step ``t``.

        Two optional cost terms are accumulated:

        1. **Wear cost** — a per-kWh throughput charge that discourages
           unnecessary cycling. Enabled when ``wear_cost_eur_per_kwh > 0``.

           .. code-block::

               wear_term(t) = wear_cost × (total_charge[t] + total_discharge[t]) × dt

        2. **SOC low penalty** — a cost applied when the battery SOC drops below
           ``optimal_lower_soc_kwh``. The magnitude is proportional to the
           deficit. Enabled when ``soc_low_penalty_eur_per_kwh_h > 0`` and
           ``optimal_lower_soc_kwh > min_soc_kwh``.

           .. code-block::

               soc_penalty(t) = soc_low_penalty_eur_per_kwh_h × soc_low[t] × dt

        Both terms are zero by default.

        Args:
            t: Time step index within ``ctx.T``.

        Returns:
            A linear expression in EUR representing the total cost penalty at
            step ``t``, or ``0`` if all penalty rates are zero.
        """
        terms: Any = 0

        if self.config.wear_cost_eur_per_kwh > 0.0:
            # Use the AC power expressions which are model-agnostic (work for
            # both stacked-segment and SOS2 modes).
            terms = (
                self.config.wear_cost_eur_per_kwh
                * (self.charge_ac_kw(t) + self.discharge_ac_kw(t))
                * self._dt
            )

        if self.config.soc_low_penalty_eur_per_kwh_h > 0.0 and t in self._soc_low:
            # soc_low[t] is the kWh deficit below optimal_lower_soc_kwh at step t.
            # Multiplying by dt (hours) converts the rate from EUR/kWh-hour to EUR/step.
            # Adding to terms means the solver treats staying below the preferred
            # lower bound as costly — it will charge sooner or more aggressively
            # to avoid a large deficit, but will still discharge when the arbitrage
            # revenue exceeds this penalty.
            penalty = self.config.soc_low_penalty_eur_per_kwh_h * self._soc_low[t] * self._dt
            terms = terms + penalty

        return terms
