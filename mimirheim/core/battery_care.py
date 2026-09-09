"""Periodic full-charge policy for lithium batteries.

A lithium pack that is never brought to the top of its range stops balancing:
the BMS gets no opportunity to equalise cells, the weakest one drifts, and
usable capacity is lost quietly. A floor enforced by the inverter on its own is
invisible to a planner, and a plan that targets below it is not refused, only
under-delivered, so every downstream
assumption in that plan is wrong. This module is what lets mimirheim own the
policy instead.

Two quantities come out of it, both derived from a single piece of state: the
timestamp at which the battery was last *measured* full.

- **The floor.** Each elapsed target interval without a full charge adds one
  step to a dynamic minimum SOC, capped. It costs nothing in the objective and
  simply narrows the usable window, biasing the plan upward.
- **The deadline.** The floor alone cannot force a full charge, because it is
  capped well below the top. So once the due time falls inside the solve
  horizon, the caller constrains the SOC to reach the threshold. That is a
  constraint on state at a time, not an instruction to charge at a time: the
  solver still picks the cheap quarter-hours.

What this module does not do:
- It does not touch the solver. It returns numbers; ``BatteryDevice`` turns
  them into constraints.
- It does not perform I/O, and it holds no state of its own. The observed
  timestamp is passed in and handed back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from mimirheim.config.schema import SocRatchetConfig


@dataclass(frozen=True)
class CarePlan:
    """What the full-charge policy asks of one solve cycle.

    Attributes:
        floor_kwh: Dynamic minimum SOC in kWh contributed by the ratchet. Zero
            when the policy is off, satisfied, or has no history to act on.
            The caller combines it with the configured ``min_soc_kwh``; this
            value on its own is not the enforced floor.
        deadline_step: The step the due time falls on — the last step ending at
            or before it, or 0 when the target is already overdue. None when no
            full charge is due inside this horizon. This is the clock's answer
            alone, and it is a lower bound on when the charge must land, not a
            prediction that it can: whether the model can deliver by then is
            settled later by ``model_builder._probe_care_targets``, which
            constrains the first step at or after this one it can prove
            reachable and reports it as ``enforced_step``.
        full_target_kwh: SOC in kWh the battery should reach. None whenever
            ``deadline_step`` is None.
        hours_since_full: Hours since the last measured full charge, for the
            status topic. None when the battery has never been seen full.
    """

    floor_kwh: float
    deadline_step: int | None
    full_target_kwh: float | None
    hours_since_full: float | None


def is_newer(candidate: datetime | None, current: datetime | None) -> bool:
    """Return True when ``candidate`` should replace ``current`` as the last full charge.

    The last measured full charge only ever moves forward. It records that the
    cells were balanced at a moment in time, and no later reading can make that
    untrue — so a smaller timestamp is always stale, whatever path it arrived
    by. Letting one win re-arms a policy the battery has already satisfied,
    and because the retained topic is the only durable store, the mistake
    survives a restart.

    Args:
        candidate: The timestamp being offered. None never wins.
        current: The timestamp held now. None always loses to a real one.

    Returns:
        True if ``candidate`` is present and strictly later than ``current``,
        or ``current`` is absent.
    """
    if candidate is None:
        return False
    return current is None or candidate > current


def is_older(candidate: datetime | None, current: datetime | None) -> bool:
    """Return True when ``candidate`` should replace ``current`` as the baseline.

    The mirror image of :func:`is_newer`, and the direction is the whole point:
    ``care_since_utc`` marks the start of the interval the battery has been
    waiting through, so only an earlier value is news. For a battery never seen
    full it is the only clock the policy has, and advancing it discards elapsed
    waiting — repeatedly, that postpones the first balance charge indefinitely
    rather than delaying it a cycle.

    Both rules live here, rather than at each of the places that needs them,
    because they were open-coded seven times across the readiness tracker and
    the publisher and drifted: one site was written with the comparison
    inverted relative to its neighbours, and one was simply missed.

    Args:
        candidate: The timestamp being offered. None never wins.
        current: The timestamp held now. None always loses to a real one.

    Returns:
        True if ``candidate`` is present and strictly earlier than ``current``,
        or ``current`` is absent.
    """
    if candidate is None:
        return False
    return current is None or candidate < current


def observe_full_charge(
    *,
    config: SocRatchetConfig,
    capacity_kwh: float,
    soc_kwh: float,
    now: datetime,
) -> datetime | None:
    """Return ``now`` when a measured SOC counts as a full charge, else None.

    The reset must key off an observed state of charge. A plan that intended a
    full charge and fell short cannot be allowed to count, or the policy
    congratulates itself on the schedule it wrote while the cells never
    balance. This function is therefore called with a reading from the battery,
    never with a solver value.

    Args:
        config: The battery's full-charge policy.
        capacity_kwh: Usable capacity, used to turn the percentage threshold
            into kWh.
        soc_kwh: Measured state of charge in kWh.
        now: Timestamp to record, normally the moment the reading arrived.

    Returns:
        ``now`` if the policy is enabled and the reading is at or above the
        threshold, otherwise None. A None result means "no new observation",
        not "not full" — callers keep whatever timestamp they already held.
    """
    if not config.enabled:
        return None
    if soc_kwh >= _full_threshold_kwh(config, capacity_kwh):
        return now
    return None


def care_plan(
    *,
    config: SocRatchetConfig,
    capacity_kwh: float,
    last_full_utc: datetime | None,
    care_since_utc: datetime | None,
    solve_time_utc: datetime,
    horizon: int,
    dt: float,
) -> CarePlan:
    """Work out what the policy asks of this solve cycle.

    Args:
        config: The battery's full-charge policy.
        capacity_kwh: Usable capacity in kWh.
        last_full_utc: When the battery was last measured at or above the
            threshold. None when it has never been observed full.
        care_since_utc: When the policy first saw this battery, used as the
            baseline while ``last_full_utc`` is None. Without it a battery that
            has never reached the threshold would stay dormant forever, and the
            first balance charge — the one most likely to be needed — would
            never be scheduled.
        solve_time_utc: Start of this solve cycle.
        horizon: Number of steps in the horizon.
        dt: Step duration in hours.

    Returns:
        A ``CarePlan``. Every field is inert when the policy is disabled.
    """
    if not config.enabled:
        return CarePlan(
            floor_kwh=0.0,
            deadline_step=None,
            full_target_kwh=None,
            hours_since_full=None,
        )

    # A battery never seen full still needs its first balance charge, so the
    # clock runs from when the policy first observed it. Treating "no history"
    # as infinitely overdue would put every fresh installation at its cap on
    # the first solve; treating it as dormant would mean a battery that never
    # happens to reach the threshold on its own is never made to.
    reference = last_full_utc if last_full_utc is not None else care_since_utc
    if reference is None:
        return CarePlan(
            floor_kwh=0.0,
            deadline_step=None,
            full_target_kwh=None,
            hours_since_full=None,
        )

    reference = _as_utc(reference)
    # Clamped at zero: solve_time_utc is floored to the quarter hour while an
    # observation carries its arrival time, so a full charge seen at 12:07
    # would otherwise report -7 minutes against the 12:00 solve. A retained
    # timestamp from the future — a clock skew on the publisher — would
    # otherwise drive the floor negative and fail validation, taking the solve
    # down over a reporting field.
    elapsed = max(timedelta(0), solve_time_utc - reference)
    hours_since_full = (
        max(0.0, (solve_time_utc - _as_utc(last_full_utc)).total_seconds() / 3600.0)
        if last_full_utc is not None
        else None
    )
    interval = timedelta(days=config.target_interval_days)

    # One step per WHOLE interval missed (int() truncates, so a partially
    # elapsed interval does not step early), capped. Note this is a function of
    # elapsed time only: there is no accumulator to drift out of step with
    # reality, and a restart reconstructs the same floor from the same
    # timestamp.
    intervals_missed = int(elapsed / interval)
    floor_pct = min(intervals_missed * config.step_pct, config.cap_pct)
    floor_kwh = capacity_kwh * floor_pct / 100.0

    deadline_step, full_target_kwh = _deadline(
        config=config,
        capacity_kwh=capacity_kwh,
        due_at=reference + interval,
        solve_time_utc=solve_time_utc,
        horizon=horizon,
        dt=dt,
    )

    return CarePlan(
        floor_kwh=floor_kwh,
        deadline_step=deadline_step,
        full_target_kwh=full_target_kwh,
        hours_since_full=hours_since_full,
    )


def _deadline(
    *,
    config: SocRatchetConfig,
    capacity_kwh: float,
    due_at: datetime,
    solve_time_utc: datetime,
    horizon: int,
    dt: float,
) -> tuple[int | None, float | None]:
    """Return the first step the full-charge target applies from, if any.

    Three outcomes, in order:

    - The due time is beyond the horizon: nothing to do this cycle. The floor
      keeps climbing in the meantime and the deadline will arrive later.
    - The due time has passed: step 0. An overdue battery has waited long
      enough, and step 0 costs nothing when the target is out of reach in the
      first quarter-hour: the caller anchors at the first step from here that
      it can prove reachable, so this is a lower bound, not a demand.
      Anchoring at the end of the horizon instead reads as the generous choice
      and is in fact the broken one: mimirheim re-solves every quarter of an
      hour and executes only the first step, so an overdue charge pinned to
      "the last step" is pushed one step further out on every solve and never
      happens.
    - The due time falls inside the horizon: the last step that ends at or
      before it, so charging after the deadline is not counted as having met
      it, and the solver has the whole window to find cheap quarter-hours in.

    There is deliberately no reachability test here. Deciding whether the model
    can climb from here to the target in a given number of steps means
    reproducing derating, the minimum charge power and a non-linear efficiency
    curve outside the solver, and being wrong either strands the policy or
    makes the solve infeasible. The question is answered by the solver instead,
    in ``model_builder._probe_care_targets``.
    """
    target_kwh = _full_threshold_kwh(config, capacity_kwh)

    if due_at <= solve_time_utc:
        return (0, target_kwh) if horizon > 0 else (None, None)

    if due_at > solve_time_utc + timedelta(hours=horizon * dt):
        return None, None

    # soc[t] is the SOC at the END of step t, so anchor on the last step whose
    # end does not pass the deadline. The epsilon absorbs float error so an
    # exact step boundary maps to the step ending on it.
    delta_hours = (due_at - solve_time_utc).total_seconds() / 3600.0
    step = int(delta_hours / dt + 1e-9) - 1
    if step < 0 or step >= horizon:
        return None, None

    return step, target_kwh


def _full_threshold_kwh(config: SocRatchetConfig, capacity_kwh: float) -> float:
    """Return the SOC in kWh that counts as a full charge."""
    return capacity_kwh * config.full_threshold_pct / 100.0


def _as_utc(value: datetime) -> datetime:
    """Return ``value`` as a UTC-aware datetime, treating a naive value as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
