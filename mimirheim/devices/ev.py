"""EV charger device — models a vehicle battery with optional V2H discharge.

This module is structurally similar to ``battery.py`` with three key differences:

1. **Availability gate**: when the vehicle is not plugged in (``available=False``),
   all charge and discharge variables are forced to zero and no SOC tracking
   is performed.

2. **Charging window constraint**: when ``available=True``,
   ``inputs.window_latest`` is set, and ``inputs.target_soc_kwh`` is set,
   the SOC must reach ``inputs.target_soc_kwh`` by the step that corresponds
   to ``window_latest``. This is a hard constraint — the solver is not allowed
   to deliver the vehicle below its target charge.

3. **Discharge segments optional**: if ``config.discharge_segments`` is empty,
   no discharge variables are declared and the mode guard is skipped.

``solve_time_utc`` is accepted as an argument to ``add_constraints`` so that
window datetimes can be translated to step indices. It must be supplied by the
model builder; it is not fetched from the IO layer.

This module does not import from ``mimirheim.io``. All solver interactions go through
``ModelContext.solver`` (a ``SolverBackend``); ``python-mip`` is never imported here.
"""

from datetime import datetime
from typing import Any

from mimirheim.config.schema import EvConfig
from mimirheim.core.bundle import EvInputs
from mimirheim.core.context import ModelContext


def _datetime_to_step(dt_value: datetime, solve_time: datetime, dt_hours: float) -> int:
    """Convert a wall-clock datetime to a horizon step index.

    Args:
        dt_value: The datetime to convert (e.g. ``window_latest``).
        solve_time: The UTC timestamp at which this solve cycle began.
        dt_hours: Duration of each step in hours (e.g. 0.25 for quarter-hourly).

    Returns:
        Zero-based step index. Clamped to a non-negative integer; no upper
        bound clamping is applied here (callers should clamp to ``len(ctx.T)-1``
        if needed).
    """
    delta_hours = (dt_value - solve_time).total_seconds() / 3600.0
    return max(0, int(delta_hours / dt_hours))


class EvDevice:
    """Models an EV charger with optional V2H discharge as a MILP sub-problem.

    Each instance corresponds to one entry in ``config.ev``. The model builder
    creates one ``EvDevice`` per named EV in the config, calls
    ``add_variables`` once, then calls ``add_constraints`` with the live state
    from MQTT and the solve cycle's start time.

    Attributes:
        name: Device name matching the key in ``config.ev``.
        config: Static EV charger configuration.
        charge_seg: Maps ``(t, i)`` to the solver variable for the power
            delivered to the vehicle via charge segment ``i`` at step ``t``,
            in kW. Populated by ``add_variables``.
        discharge_seg: Maps ``(t, i)`` to the solver variable for power drawn
            from the vehicle (V2H) via segment ``i`` at step ``t``, in kW.
            Empty if ``config.discharge_segments`` is empty.
        soc: Maps ``t`` to the solver variable for vehicle SOC at the end of
            step ``t``, in kWh. Empty when ``available=False``.
        mode: Maps ``t`` to the binary mode variable. Only populated when
            discharge segments exist.
    """

    def __init__(self, name: str, config: EvConfig) -> None:
        """Initialise the EV device.

        Args:
            name: Device name, matching the key in ``MimirheimConfig.ev``.
            config: Validated static configuration for this EV charger.
        """
        self.name = name
        self.config = config
        self.charge_seg: dict[tuple[int, int], Any] = {}
        self.discharge_seg: dict[tuple[int, int], Any] = {}
        self.soc: dict[int, Any] = {}
        self.mode: dict[int, Any] = {}
        self._dt: float = 0.25
        # Set to True by add_constraints when the vehicle is plugged in.
        # Used by terminal_soc_var to determine whether the SOC variable
        # at the last step is physically meaningful.
        self._available: bool = False

    def add_variables(self, ctx: ModelContext) -> None:
        """Declare all MIP variables for this EV charger.

        For each time step ``t`` in ``ctx.T`` and each charge segment ``i``:

        - ``charge_seg[t, i]``: Power delivered to the vehicle via segment
          ``i`` in kW. Bounded ``[0, segment.power_max_kw]``.

        For each time step ``t`` and each discharge segment ``i`` (only when
        ``config.discharge_segments`` is non-empty):

        - ``discharge_seg[t, i]``: Power drawn from the vehicle via segment
          ``i`` in kW (V2H discharge). Bounded ``[0, segment.power_max_kw]``.

        For each time step ``t``:

        - ``soc[t]``: Vehicle SOC in kWh. Bounded
          ``[config.min_soc_kwh, config.capacity_kwh]``.

        - ``mode[t]``: Binary direction variable. Only declared when
          discharge segments exist, to prevent simultaneous charge and
          discharge.

        Args:
            ctx: The current solve context.
        """
        self._dt = ctx.dt
        has_v2h = len(self.config.discharge_segments) > 0

        for t in ctx.T:
            for i, seg in enumerate(self.config.charge_segments):
                # charge_seg[t, i]: power delivered to the vehicle in kW via
                # segment i at step t. Upper bound = seg.power_max_kw (the
                # maximum that segment can carry). Ranges like 0–3.7 kW
                # (single-phase 16 A) or 0–7.4 kW (single-phase 32 A) are
                # typical.
                self.charge_seg[t, i] = ctx.solver.add_var(lb=0.0, ub=seg.power_max_kw)

            if has_v2h:
                for i, seg in enumerate(self.config.discharge_segments):
                    # discharge_seg[t, i]: power drawn from the vehicle to the
                    # home (V2H) via segment i at step t, in kW. Only created
                    # when the hardware supports bidirectional flow.
                    self.discharge_seg[t, i] = ctx.solver.add_var(
                        lb=0.0, ub=seg.power_max_kw
                    )

                # mode[t]: binary direction variable. 1 = charging, 0 = V2H
                # discharging. Required to prevent the LP from simultaneously
                # charging and discharging (see battery.py for a full
                # explanation of the Big-M pattern).
                self.mode[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)

            # soc[t]: vehicle state of charge at end of step t, in kWh.
            # Bounded by [min_soc_kwh, capacity_kwh] to protect the battery
            # and respect physical limits.
            self.soc[t] = ctx.solver.add_var(
                lb=self.config.min_soc_kwh,
                ub=self.config.capacity_kwh,
            )

    def add_constraints(
        self,
        ctx: ModelContext,
        inputs: EvInputs,
        solve_time_utc: datetime,
    ) -> None:
        """Add availability, SOC tracking, and window constraints.

        **Availability gate** — when ``inputs.available`` is ``False``, the
        vehicle is not plugged in. All charge and discharge variables are forced
        to zero. No SOC tracking constraint is added because the vehicle is off
        the charger and its SOC is not meaningful in the model.

        This constraint is critical for correctness: without it the solver
        could schedule charge power for an unplugged vehicle, which cannot
        be delivered and would corrupt the power balance.

        **SOC update** — when ``available=True``, a SOC equality constraint is
        added per step (identical in structure to the Battery device):

        .. code-block::

            soc[t] = soc[t-1]
                     + Σ_i (charge_efficiency_i × charge_seg[t,i] × dt)
                     - Σ_i ((1/discharge_efficiency_i) × discharge_seg[t,i] × dt)

        **Target SOC window constraint** — when ``available=True``,
        ``inputs.window_latest`` is set, and ``inputs.target_soc_kwh`` is set,
        the vehicle must reach ``inputs.target_soc_kwh`` by the step
        corresponding to ``window_latest``. This is a hard constraint, not a
        soft penalty: the solver must satisfy the user's departure requirement.

        If either field is absent, or if the window_latest step is beyond the
        horizon, no constraint is added.

        **Simultaneous charge/discharge guard** (Big-M) — only added when
        discharge segments exist; identical to the Battery implementation.

        Args:
            ctx: The current solve context.
            inputs: Validated live EV state including SOC, availability, and
                optional charging window.
            solve_time_utc: UTC timestamp at the start of this solve cycle.
                Used to translate ``window_latest`` to a step index.
        """
        has_v2h = len(self.config.discharge_segments) > 0
        max_charge_kw = sum(s.power_max_kw for s in self.config.charge_segments)
        max_discharge_kw = (
            sum(s.power_max_kw for s in self.config.discharge_segments)
            if has_v2h else 0.0
        )

        # Record availability for terminal_soc_var. The SOC tracking variables
        # exist for all steps regardless of availability, but when the vehicle
        # is not plugged in they are unconstrained free variables. Attaching a
        # terminal value to a free variable would distort the objective without
        # reflecting any physical reality.
        self._available = inputs.available

        if not inputs.available:
            # ----------------------------------------------------------------
            # Availability gate: vehicle is not plugged in.
            #
            # Force all charge (and discharge if configured) variables to zero.
            # This is the most operationally important constraint in this
            # device: without it, the solver would schedule charge power
            # thinking the car is present. The resulting setpoint would be
            # sent to the charger hardware and either be ignored (if the
            # charger detects no vehicle) or cause a fault.
            #
            # Note: we do NOT add SOC update constraints when unavailable.
            # The SOC is not meaningful while the car is away, and adding
            # SOC equality constraints without a valid energy balance would
            # make the model infeasible.
            # ----------------------------------------------------------------
            for t in ctx.T:
                for i in range(len(self.config.charge_segments)):
                    ctx.solver.add_constraint(self.charge_seg[t, i] == 0.0)
                if has_v2h:
                    for i in range(len(self.config.discharge_segments)):
                        ctx.solver.add_constraint(self.discharge_seg[t, i] == 0.0)
            return

        # Compute the window_latest step index if a departure window is set.
        window_step: int | None = None
        if inputs.window_latest is not None:
            step = _datetime_to_step(inputs.window_latest, solve_time_utc, ctx.dt)
            if step < len(ctx.T):
                window_step = step

        for t in ctx.T:
            # --- Energy balance for SOC update ---
            energy_stored = sum(
                seg.efficiency * self.charge_seg[t, i] * ctx.dt
                for i, seg in enumerate(self.config.charge_segments)
            )
            energy_drawn = sum(
                (1.0 / seg.efficiency) * self.discharge_seg[t, i] * ctx.dt
                for i, seg in enumerate(self.config.discharge_segments)
            ) if has_v2h else 0.0

            soc_prev = inputs.soc_kwh if t == 0 else self.soc[t - 1]
            ctx.solver.add_constraint(
                self.soc[t] - energy_stored + energy_drawn == soc_prev
            )

            # --- Big-M simultaneous charge/discharge guard ---
            if has_v2h:
                total_charge = sum(
                    self.charge_seg[t, i]
                    for i in range(len(self.config.charge_segments))
                )
                total_discharge = sum(
                    self.discharge_seg[t, i]
                    for i in range(len(self.config.discharge_segments))
                )
                ctx.solver.add_constraint(
                    total_charge <= max_charge_kw * self.mode[t]
                )
                ctx.solver.add_constraint(
                    total_discharge <= max_discharge_kw * (1 - self.mode[t])
                )

                # Minimum operating power floors (Plan 38C).
                #
                # When the EV charger (in V2H mode) is actively charging or
                # discharging, some hardware cannot safely operate below a
                # threshold — for example, a CHAdeMO gateway with a minimum
                # current setpoint.
                #
                # The floor is applied only when mode[t] selects the direction:
                #   - charge floor: total_charge >= min_charge_kw * mode[t]
                #   - discharge floor: total_discharge >= min_discharge_kw * (1 - mode[t])
                #
                # For charge-only EVs (has_v2h=False) this entire block is
                # unreachable, so the discharge floor is never added and
                # self.mode is never used in add_constraints.
                if self.config.min_charge_kw is not None:
                    ctx.solver.add_constraint(
                        total_charge >= self.config.min_charge_kw * self.mode[t]
                    )
                if self.config.min_discharge_kw is not None:
                    ctx.solver.add_constraint(
                        total_discharge
                        >= self.config.min_discharge_kw * (1 - self.mode[t])
                    )

        # ----------------------------------------------------------------
        # Target SOC window constraint.
        #
        # The user expects the vehicle to be charged to at least
        # inputs.target_soc_kwh by window_latest. This is enforced as a
        # hard lower bound on the SOC variable at the relevant step.
        #
        # Both fields must be present for the constraint to apply. If either
        # is absent the departure target is treated as unset: the solver
        # charges opportunistically based on prices and the terminal SoC value.
        #
        # Why a hard constraint and not a penalty? Because a partially-charged
        # vehicle that cannot complete a journey is a safety and usability
        # failure, not merely a cost inefficiency. The solver must guarantee
        # the target is met; if it cannot (e.g. grid import capacity is
        # insufficient), the solve will be infeasible and the IO layer will
        # retain the previous schedule and raise an alert.
        # ----------------------------------------------------------------
        if window_step is not None and inputs.target_soc_kwh is not None:
            ctx.solver.add_constraint(
                self.soc[window_step] >= inputs.target_soc_kwh
            )

    def terminal_soc_var(self, ctx: ModelContext) -> Any | None:
        """Return the solver variable for the vehicle's SOC at the last step.

        Returns ``None`` when the vehicle is not plugged in. In that case the
        SOC tracking constraints are absent, making the SOC variables free
        (unconstrained). Attaching a terminal value to a free variable would
        cause the solver to set it to an arbitrary extreme without any physical
        meaning.

        When the vehicle is available (plugged in), the SOC at the last step
        carries the same end-of-horizon value semantics as the battery: each
        kWh retained avoids having to re-charge at the average import price
        after the horizon ends. This is relevant for V2H-capable vehicles that
        could otherwise be drained near the end of the horizon.

        Args:
            ctx: The current solve context. Used to identify the last step.

        Returns:
            The solver variable ``soc[T-1]`` if the vehicle is available,
            otherwise ``None``.
        """
        if not self._available:
            return None
        return self.soc.get(ctx.T[-1])

    def set_external_mode(self, mode_vars: dict[int, Any]) -> None:
        """Replace per-step mode variables with externally-supplied shared ones.

        Called by ``build_and_solve`` when two or more EV chargers are present,
        to enforce a shared system charge/discharge direction across all EVs.
        This prevents energy from circulating between EVs (one V2H-discharging
        while another charges) with no net gain to the system.

        The shared binary ``ev_system_mode[t]`` has the same semantics as the
        per-device mode:

        - 1 = all EVs are in the charging direction this step.
        - 0 = all EVs are in the discharging direction this step.

        For charge-only EVs (no ``discharge_segments``), ``mode[t]`` is never
        created by ``add_variables`` and is never read by ``add_constraints``
        (the Big-M discharge guard is skipped when ``has_v2h=False``). Calling
        this method on a charge-only EV injects the shared variable into
        ``self.mode`` but has no effect on the solver model. This is intentional:
        the activation logic is uniform across all EV chargers regardless of
        V2H capability.

        This method must be called after ``add_variables`` and before
        ``add_constraints``.

        Args:
            mode_vars: Dict mapping step index ``t`` to the shared binary
                solver variable for that step.
        """
        self.mode = dict(mode_vars)

    def net_power(self, t: int) -> Any:
        """Return the net power expression at step ``t``.

        Positive = V2H discharge (producing power for the home). Negative =
        charging (consuming power from the home bus).

        Args:
            t: Time step index.

        Returns:
            A linear expression or float representing net power in kW.
        """
        total_discharge = sum(
            self.discharge_seg[t, i]
            for i in range(len(self.config.discharge_segments))
        ) if self.discharge_seg else 0.0

        total_charge = sum(
            self.charge_seg[t, i]
            for i in range(len(self.config.charge_segments))
        )
        return total_discharge - total_charge

    def objective_terms(self, t: int) -> Any:
        """Return the wear cost penalty at step ``t``.

        Identical in structure to ``Battery.objective_terms``: a positive term
        in the minimisation objective that discourages unnecessary throughput.

        Args:
            t: Time step index.

        Returns:
            A linear expression in EUR, or ``0`` if wear cost is zero.
        """
        if self.config.wear_cost_eur_per_kwh == 0.0:
            return 0

        total_charge = sum(
            self.charge_seg[t, i]
            for i in range(len(self.config.charge_segments))
        )
        total_discharge = sum(
            self.discharge_seg[t, i]
            for i in range(len(self.config.discharge_segments))
        ) if self.discharge_seg else 0.0

        return (
            self.config.wear_cost_eur_per_kwh
            * (total_charge + total_discharge)
            * self._dt
        )
