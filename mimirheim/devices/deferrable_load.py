"""Deferrable load device — schedules a fixed-duration load within a time window.

A deferrable load (washing machine, dishwasher, EV pre-conditioning, etc.) has
three properties:

- It draws power according to ``config.power_profile`` while running: one kW
  value per step, consumed in sequence from the first step of the run.
- It must run for exactly ``len(config.power_profile)`` consecutive time steps.
- The operator provides a window [``earliest``, ``latest``] within which the
  run must start and complete.

The solver chooses the optimal start time within the window to minimise cost.
This is a classic "fixed-duration scheduling" MIP formulation using a single
binary variable per eligible start step.

Once the load physically starts, the external automation publishes the actual
start datetime to ``topic_committed_start_time`` (see ``DeferrableLoadConfig``). From
that point mimirheim observes one of four states, determined by comparing
``solve_start`` against the committed ``start_time``:

1. **No start_time, window present** — binary optimisation. The solver picks
   the optimal start time. This is the pre-start state.

2. **start_time present, run entirely in the past** (``solve_start >= start_time
   + duration``) — the previous run has completed. Fall through to scheduling:
   if a window is present, re-schedule for a new run; otherwise no-op.

3. **start_time present, run currently active** (``start_time <= solve_start <
   start_time + duration``) — the load is running. Add fixed power draws for
   the remaining profile steps. No binary variable is used.

4. **start_time present, run starts in the future** (``solve_start < start_time``)
   — committed: the automation has accepted and programmed the start time. mimirheim
   does not re-optimise. ``net_power`` returns the profile values at the
   committed horizon step offsets, any window is ignored.

If no window and no start_time are provided, the device adds no variables and
contributes zero power to the balance.

This module does not import from ``mimirheim.io``. All solver interactions go
through ``ModelContext.solver``. Window datetimes are translated to step
indices here, not in the IO layer.
"""

from datetime import datetime
from typing import Any

from mimirheim.config.schema import DeferrableLoadConfig
from mimirheim.core.bundle import DeferrableWindow
from mimirheim.core.context import ModelContext


def _datetime_to_step(dt_value: datetime, solve_time: datetime, dt_hours: float) -> int:
    """Convert a wall-clock datetime to a horizon step index.

    Args:
        dt_value: The datetime to convert.
        solve_time: The UTC timestamp at the start of this solve cycle.
        dt_hours: Duration of each step in hours.

    Returns:
        Zero-based step index, clamped to be non-negative.
    """
    delta_hours = (dt_value - solve_time).total_seconds() / 3600.0
    return max(0, int(delta_hours / dt_hours))


class DeferrableLoad:
    """Models a fixed-profile deferrable load as a binary scheduling problem.

    The only decision variable is ``start[t]`` — a binary variable that is 1
    if the load starts at step ``t``. Power at step ``t`` is derived from the
    profile and the active start variable, keeping the model compact.

    Once ``topic_committed_start_time`` has been received (the load has physically
    started), the device switches to fixed-draw mode: ``start`` is empty and
    ``net_power(t)`` returns the profile value for the profile offset
    ``_elapsed_steps + t`` for each still-running step and 0 afterwards. See
    the module-level docstring for the full state machine.

    Attributes:
        name: Device name matching the key in ``config.deferrable_loads``.
        config: Static deferrable load configuration.
        start: Maps eligible step indices to their binary start variable handle.
            Empty before ``add_constraints`` is called, if ``window`` is None,
            or when in fixed-draw or committed state.
        _fixed_steps: Number of horizon steps in which the load is fixed-on
            due to an active run (``start_time`` is in the past). Zero unless
            in running state. ``net_power(t)`` returns the profile value at
            ``_elapsed_steps + t`` for ``t < _fixed_steps``.
        _elapsed_steps: Profile steps already consumed before this horizon.
            Zero unless in running state.
        _committed_start_step: When the automation has published a future
            ``start_time`` (committed state), this is the horizon step at which
            the run is scheduled to begin. ``net_power(t)`` returns
            ``-profile[t - _committed_start_step]`` for the run's active steps.
            ``None`` unless in committed state.
    """

    def __init__(self, name: str, config: DeferrableLoadConfig) -> None:
        """Initialise the deferrable load device.

        Args:
            name: Device name.
            config: Validated static configuration for this load.
        """
        self.name = name
        self.config = config
        self.start: dict[int, Any] = {}
        self._horizon: int = 0
        self._active: bool = False
        self._fixed_steps: int = 0  # non-zero only in "running" (active) state
        self._elapsed_steps: int = 0  # profile steps already consumed before this horizon
        self._committed_start_step: int | None = None  # set in "committed" (future) state

    def add_variables(self, ctx: ModelContext) -> None:
        """No-op at this stage — variables are created in add_constraints.

        Deferrable load variables depend on the runtime window datetimes, which
        are not known until ``add_constraints`` is called. This method exists
        to satisfy the Device Protocol.

        Args:
            ctx: The current solve context (unused here).
        """
        self._horizon = len(ctx.T)

    def add_constraints(
        self,
        ctx: ModelContext,
        window: DeferrableWindow | None,
        solve_time_utc: datetime,
        start_time: datetime | None = None,
    ) -> None:
        """Add scheduling constraints for this run cycle.

        Determines the device state from ``start_time`` and ``solve_time_utc``,
        then applies the appropriate modelling approach:

        - **Completed state** (``start_time`` present, run entirely in the
          past): falls through to scheduling. If ``window`` is provided, the
          load is re-scheduled for a new run; otherwise no-op.
        - **Running state** (``start_time`` present, run currently active): sets
          ``_fixed_steps`` and ``_elapsed_steps`` so ``net_power(t)`` returns
          the correct profile values for remaining steps. No solver variables.
        - **Committed state** (``start_time`` present, run starts in the
          future): sets ``_committed_start_step`` so ``net_power(t)`` returns
          profile values at the committed horizon step. No solver variables;
          any ``window`` argument is ignored.
        - **Unscheduled state** (``window`` is None and no active
          ``start_time``): no-op. ``net_power`` returns 0.
        - **Scheduling state** (``window`` present, no ``start_time``, or
          ``start_time`` has completed): binary optimisation within the window.
        - **Completed state** (``start_time`` present but the run has ended):
          no-op. ``net_power`` returns 0.
        - **Unscheduled state** (``window`` is None and no ``start_time``):
          no-op. ``net_power`` returns 0.
        - **Scheduling state** (``window`` present, no active ``start_time``,
          or ``start_time`` is in the future): binary optimisation within
          the window.

        When both ``start_time`` and ``window`` are provided and ``start_time``
        indicates the load is currently running, the running state takes
        priority and the window is ignored.

        Args:
            ctx: The current solve context.
            window: The scheduling window for this cycle, or None if no run is
                scheduled.
            solve_time_utc: UTC timestamp at the start of this solve cycle,
                used to convert window datetimes to step indices.
            start_time: Actual start datetime published by the external
                automation when the load physically began. None if the load
                has not started yet.
        """
        duration = len(self.config.power_profile)
        horizon = len(ctx.T)

        # --- States 2 / 3 / 4: start_time is known ---
        if start_time is not None:
            elapsed_seconds = (solve_time_utc - start_time).total_seconds()
            elapsed_steps = int(elapsed_seconds / (ctx.dt * 3600.0))

            # State 2: run is entirely in the past — reschedule.
            # The retained start_time is stale; a new run should be scheduled
            # if a window is present. Fall through to the scheduling block.
            if elapsed_steps >= duration:
                pass  # fall through to State 1 scheduling logic

            # State 4: start_time is in the future — committed.
            # The automation has accepted and programmed this start time.
            # mimirheim must not alter the schedule. Convert the future datetime to
            # a horizon step index and record it; net_power will return the
            # profile values at those steps without any solver variable.
            elif elapsed_steps < 0:
                self._committed_start_step = _datetime_to_step(
                    start_time, solve_time_utc, ctx.dt
                )
                return

            else:
                # State 3: load is currently running.
                # remaining_steps = duration - elapsed_steps, capped to horizon.
                self._fixed_steps = min(duration - elapsed_steps, horizon)
                self._elapsed_steps = elapsed_steps
                return

        # --- State 1: binary scheduling ---
        if window is None:
            return

        self._active = True

        # Convert window datetimes to step indices.
        earliest_step = _datetime_to_step(window.earliest, solve_time_utc, ctx.dt)
        latest_step = _datetime_to_step(window.latest, solve_time_utc, ctx.dt)

        # The last valid start step: the load must finish by latest_step, so
        # the start must be no later than (latest_step - duration).
        # Example: duration=2, latest_step=6 → last start = step 4 (runs 4,5).
        last_valid_start = min(latest_step - duration, horizon - duration)

        # Declare start[t] for each eligible step and set upper bound 0 for
        # out-of-window steps so the solver cannot choose them.
        for t in ctx.T:
            if earliest_step <= t <= last_valid_start:
                # start[t] is a binary "start decision" variable.
                #
                # Decision variable: 1 if the load begins at step t, 0 otherwise.
                # The solver allocates exactly one start across all eligible steps
                # (enforced by the sum-to-one constraint below). Binary because
                # the load either starts or it does not — there is no partial start.
                self.start[t] = ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)
            else:
                # Steps outside the window are given a fixed-zero variable so
                # that net_power(t) can still reference start[k] for window
                # boundary steps without raising a KeyError. Upper bound 0
                # makes them effectively constants equal to 0.
                self.start[t] = ctx.solver.add_var(lb=0.0, ub=0.0, integer=True)

        # Exactly one start within the window.
        #
        # This constraint is the core of the scheduling model. It ensures:
        # - The load runs exactly once per cycle (not zero, not multiple).
        # - The start falls within [earliest_step, last_valid_start].
        #
        # Without this constraint, the solver would either ignore the load
        # entirely (if running is costly) or start it at every cheap step
        # (exploiting cheap electricity multiple times). The sum-to-one
        # constraint makes the scheduling problem well-defined.
        eligible_starts = sum(
            self.start[t]
            for t in range(earliest_step, last_valid_start + 1)
            if t in self.start
        )
        ctx.solver.add_constraint(eligible_starts == 1)

    def net_power(self, t: int) -> Any:
        """Return the net power at step ``t``.

        Checks device state in priority order:

        **Running state** (``_fixed_steps > 0``): returns the negated profile
        value at index ``_elapsed_steps + t`` for ``t < _fixed_steps``,
        0 elsewhere.

        **Committed state** (``_committed_start_step is not None``): returns
        the negated profile value at index ``t - _committed_start_step`` for
        steps where the run is active, 0 elsewhere. No solver variable is
        involved.

        **Scheduling state** (``start`` dict is populated): the power at step
        ``t`` is the sum of profile-weighted start variables for all start
        times that place a running step at ``t``:

        .. code-block::

            net_power(t) = -Σ_{k = max(0, t - d + 1)}^{t} profile[t - k] × start[k]

        where ``d = len(profile)`` and ``profile[t - k]`` is the power drawn
        at offset ``t - k`` within the run. This is a linear expression in the
        binary variables.

        **Unscheduled / completed-no-window state**: returns 0.

        Power is negative (consuming) when the load is running.

        Args:
            t: Time step index.

        Returns:
            A linear expression representing net power in kW (negative when
            running), or a plain float in fixed-draw or committed state.
        """
        # Fixed-draw state: start_time is known and the load is running.
        if self._fixed_steps > 0:
            if t < self._fixed_steps:
                profile_idx = self._elapsed_steps + t
                return -self.config.power_profile[profile_idx]
            return 0.0

        # Committed state: start_time is set to a future time. The run is
        # modelled as a fixed-power draw at the scheduled horizon steps without
        # any solver variable.
        if self._committed_start_step is not None:
            step_in_run = t - self._committed_start_step
            if 0 <= step_in_run < len(self.config.power_profile):
                return -self.config.power_profile[step_in_run]
            return 0.0

        if not self.start:
            return 0

        profile = self.config.power_profile
        duration = len(profile)
        # For each eligible start step k that would cause the load to be
        # running at step t, multiply the corresponding profile offset
        # (t - k) by the binary start variable. The sum is weighted by the
        # actual power level for that offset in the run cycle.
        running = sum(
            profile[t - k] * self.start[k]
            for k in range(max(0, t - duration + 1), t + 1)
            if k in self.start
        )
        return -running

    def objective_terms(self, t: int) -> int:
        """Return zero — DeferrableLoad has no wear cost or objective penalty.

        Deferrable loads are purely demand devices. Their electricity
        consumption appears in the objective indirectly via the grid import
        cost: when the load runs at step ``t`` it displaces grid import (or
        export revenue) and the objective reflects this through the power
        balance. No additional objective term is required here.

        Args:
            t: Time step index (unused).

        Returns:
            Zero, always.
        """
        return 0

