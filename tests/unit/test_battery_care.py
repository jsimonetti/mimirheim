"""Unit tests for mimirheim/core/battery_care.py — the periodic full-charge policy.

The policy is pure arithmetic over an observed timestamp: given when the battery
was last measured full, how high is the floor now and is a full charge due
inside this horizon. Keeping it here, away from the solver, is what makes the
ratchet testable without building a model.

All tests must fail before the implementation exists (TDD).
"""

from datetime import datetime, timedelta, timezone

import pytest

from mimirheim.config.schema import SocRatchetConfig
from mimirheim.core.battery_care import CarePlan, care_plan, observe_full_charge

_NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
_CAPACITY = 10.0


def _cfg(**overrides: object) -> SocRatchetConfig:
    base: dict = {
        "enabled": True,
        "target_interval_days": 7.0,
        "full_threshold_pct": 97.0,
        "step_pct": 5.0,
        "cap_pct": 80.0,
    }
    base.update(overrides)
    # Pre-hold defaults for the pre-hold tests: plan for the threshold, hold
    # for a single step. The hold tests below override both explicitly.
    base.setdefault("target_pct", base["full_threshold_pct"])
    base.setdefault("hold_hours", 0.0)
    return SocRatchetConfig(**base)


def _plan(
    *,
    last_full_utc: datetime | None,
    care_since_utc: datetime | None = None,
    horizon: int = 96,
    cfg: SocRatchetConfig | None = None,
) -> CarePlan:
    return care_plan(
        config=cfg if cfg is not None else _cfg(),
        capacity_kwh=_CAPACITY,
        last_full_utc=last_full_utc,
        care_since_utc=care_since_utc,
        solve_time_utc=_NOW,
        horizon=horizon,
        dt=0.25,
    )


# ---------------------------------------------------------------------------
# Disabled policy
# ---------------------------------------------------------------------------


def test_disabled_policy_asks_for_nothing() -> None:
    """With the feature off the plan must be inert in every field."""
    plan = _plan(last_full_utc=_NOW - timedelta(days=30), cfg=_cfg(enabled=False))
    assert plan.floor_kwh == 0.0
    assert plan.deadline_step is None
    assert plan.full_target_kwh is None


# ---------------------------------------------------------------------------
# The climb
# ---------------------------------------------------------------------------


def test_floor_stays_at_zero_inside_the_interval() -> None:
    """Six days into a seven-day interval nothing has been missed yet."""
    plan = _plan(last_full_utc=_NOW - timedelta(days=6))
    assert plan.floor_kwh == 0.0


def test_floor_steps_up_once_per_missed_interval() -> None:
    """One step of 5% of 10 kWh per elapsed interval, and no more."""
    assert _plan(last_full_utc=_NOW - timedelta(days=7)).floor_kwh == pytest.approx(0.5)
    assert _plan(last_full_utc=_NOW - timedelta(days=13)).floor_kwh == pytest.approx(0.5)
    assert _plan(last_full_utc=_NOW - timedelta(days=14)).floor_kwh == pytest.approx(1.0)
    assert _plan(last_full_utc=_NOW - timedelta(days=21)).floor_kwh == pytest.approx(1.5)


def test_floor_is_capped() -> None:
    """The climb stops at cap_pct, however long the battery is neglected."""
    plan = _plan(last_full_utc=_NOW - timedelta(days=3650))
    assert plan.floor_kwh == pytest.approx(8.0)  # 80% of 10 kWh


def test_never_seen_full_and_never_observed_is_dormant() -> None:
    """With no history at all there is nothing to measure an interval from."""
    plan = _plan(last_full_utc=None, care_since_utc=None)
    assert plan.floor_kwh == 0.0
    assert plan.deadline_step is None


def test_never_seen_full_does_not_ratchet_immediately() -> None:
    """A fresh deploy has no history; it must not step up on day one.

    Treating an absent timestamp as "infinitely overdue" would have every new
    installation start against a floor at its cap, which looks like a bug and
    behaves like one.
    """
    plan = _plan(last_full_utc=None, care_since_utc=_NOW - timedelta(hours=1))
    assert plan.floor_kwh == 0.0
    assert plan.deadline_step is None


def test_a_battery_never_seen_full_still_gets_its_first_balance_charge() -> None:
    """The first observation starts the clock, so the policy eventually acts.

    A battery installed at 50% that never reaches 97% on its own is exactly the
    one that needs the mechanism. Staying dormant until a full charge is
    observed would mean waiting for the event the policy exists to cause.
    """
    plan = _plan(last_full_utc=None, care_since_utc=_NOW - timedelta(days=8))
    assert plan.floor_kwh == pytest.approx(0.5)
    assert plan.deadline_step is not None
    assert plan.hours_since_full is None


def test_a_measured_full_charge_supersedes_the_first_observation() -> None:
    plan = _plan(
        last_full_utc=_NOW - timedelta(hours=2),
        care_since_utc=_NOW - timedelta(days=90),
    )
    assert plan.floor_kwh == 0.0
    assert plan.deadline_step is None


# ---------------------------------------------------------------------------
# The deadline
# ---------------------------------------------------------------------------


def test_no_deadline_while_it_lies_beyond_the_horizon() -> None:
    """Two days in, with a 24 h horizon, the deadline is five days out."""
    plan = _plan(last_full_utc=_NOW - timedelta(days=2), horizon=96)
    assert plan.deadline_step is None
    assert plan.full_target_kwh is None


def test_deadline_lands_on_the_step_that_ends_at_or_before_it() -> None:
    """Deadline in 6 hours, quarter-hourly steps: step 23 ends at +6:00."""
    plan = _plan(last_full_utc=_NOW - timedelta(days=7) + timedelta(hours=6), horizon=96)
    assert plan.deadline_step == 23
    assert plan.full_target_kwh == pytest.approx(9.7)  # 97% of 10 kWh


def test_an_overdue_deadline_anchors_at_the_first_step() -> None:
    """An overdue charge must not be pushed along a rolling horizon.

    mimirheim re-solves every quarter of an hour and executes only the first
    step. A deadline pinned to "the last step of the horizon" moves one step
    further out on every solve, so the balance charge is perpetually planned
    and never performed. Anchoring at zero is free even when the target is
    hours of charging away, because this is the earliest step the policy will
    accept rather than the step it demands: the builder probes forward from
    here for the first step it can prove the target is reachable at.
    """
    plan = _plan(last_full_utc=_NOW - timedelta(days=9), horizon=96)
    assert plan.deadline_step == 0
    assert plan.full_target_kwh == pytest.approx(9.7)


def test_a_target_out_of_reach_is_still_asked_for() -> None:
    """No reachability test: an unreachable target keeps its deadline.

    Deciding up front whether the model can climb to the target in the steps
    available means reproducing derating, the minimum charge power and a
    non-linear efficiency curve outside the solver. Dropping the deadline on a
    wrong answer leaves the battery that needs the policy most with nothing
    asked of it, so the plan asks anyway and the builder settles reachability
    against the model.
    """
    plan = _plan(last_full_utc=_NOW - timedelta(days=9), horizon=1)
    assert plan.deadline_step == 0
    assert plan.full_target_kwh == pytest.approx(9.7)


def test_an_empty_horizon_has_nowhere_to_put_a_deadline() -> None:
    plan = _plan(last_full_utc=_NOW - timedelta(days=9), horizon=0)
    assert plan.deadline_step is None
    assert plan.full_target_kwh is None


def test_hours_since_full_is_reported_for_observability() -> None:
    plan = _plan(last_full_utc=_NOW - timedelta(hours=30))
    assert plan.hours_since_full == pytest.approx(30.0)
    assert _plan(last_full_utc=None).hours_since_full is None


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


def test_measured_full_charge_resets_the_policy() -> None:
    """An observed SOC at or above the threshold records the timestamp."""
    assert observe_full_charge(
        config=_cfg(), capacity_kwh=_CAPACITY, soc_kwh=9.7, now=_NOW
    ) == _NOW


def test_soc_below_the_threshold_does_not_reset() -> None:
    assert (
        observe_full_charge(config=_cfg(), capacity_kwh=_CAPACITY, soc_kwh=9.69, now=_NOW)
        is None
    )


def test_disabled_policy_records_nothing() -> None:
    assert (
        observe_full_charge(
            config=_cfg(enabled=False), capacity_kwh=_CAPACITY, soc_kwh=10.0, now=_NOW
        )
        is None
    )


def test_reset_clears_the_floor_and_the_deadline() -> None:
    """After a measured full charge the next plan starts clean.

    This is the whole point of keying the reset to a measurement: the floor
    that had climbed for weeks drops in one go, rather than decaying.
    """
    overdue = _plan(last_full_utc=_NOW - timedelta(days=21))
    assert overdue.floor_kwh > 0.0

    reset_at = observe_full_charge(
        config=_cfg(), capacity_kwh=_CAPACITY, soc_kwh=9.8, now=_NOW
    )
    after = _plan(last_full_utc=reset_at)
    assert after.floor_kwh == 0.0
    assert after.deadline_step is None


# ---------------------------------------------------------------------------
# Clock hygiene
# ---------------------------------------------------------------------------


def test_an_observation_inside_the_current_slot_does_not_go_negative() -> None:
    """solve_time_utc is floored to the quarter hour; observations are not.

    A full charge seen at 12:07 against a 12:00 solve would otherwise report a
    negative age, which is nonsense on the status topic.
    """
    plan = _plan(last_full_utc=_NOW + timedelta(minutes=7))
    assert plan.floor_kwh == 0.0
    assert plan.hours_since_full == 0.0


def test_a_future_timestamp_cannot_drive_the_floor_negative() -> None:
    """A clock-skewed retained value must not fail BatteryCareStatus validation.

    floor_kwh is declared non-negative, so a negative floor would raise inside
    the publisher and take down a solve over a reporting field.
    """
    plan = _plan(last_full_utc=_NOW + timedelta(days=30))
    assert plan.floor_kwh == 0.0
    assert plan.deadline_step is None


# ---------------------------------------------------------------------------
# Monotonic timestamp rules
# ---------------------------------------------------------------------------


def test_the_last_full_charge_only_moves_forward() -> None:
    """A measured full charge cannot be undone by a later, smaller reading."""
    from mimirheim.core.battery_care import is_newer

    older = _NOW - timedelta(days=1)
    assert is_newer(_NOW, older) is True
    assert is_newer(older, _NOW) is False
    assert is_newer(_NOW, _NOW) is False, "equal is not news"


def test_the_baseline_only_moves_backward() -> None:
    """The interval start retreats; advancing it discards elapsed waiting."""
    from mimirheim.core.battery_care import is_older

    older = _NOW - timedelta(days=1)
    assert is_older(older, _NOW) is True
    assert is_older(_NOW, older) is False
    assert is_older(_NOW, _NOW) is False


def test_absent_timestamps_resolve_the_same_way_for_both_rules() -> None:
    """None never wins and never blocks, whichever direction is being tested.

    Every call site had its own None handling before these were centralised,
    which is where the inconsistencies crept in: an absent value has to lose
    as a candidate and lose as an incumbent, or a first observation is either
    discarded or allowed to overwrite a real one.
    """
    from mimirheim.core.battery_care import is_newer, is_older

    for rule in (is_newer, is_older):
        assert rule(None, _NOW) is False, f"{rule.__name__}: None should not win"
        assert rule(_NOW, None) is True, f"{rule.__name__}: should beat an absent value"
        assert rule(None, None) is False, f"{rule.__name__}: nothing to say"


# ---------------------------------------------------------------------------
# Hold at the top, and plan for 100%
# ---------------------------------------------------------------------------


def test_the_plan_asks_for_the_target_not_the_reset_threshold() -> None:
    """Plan for CVL; reset on the observed threshold. Two numbers, not one."""
    plan = _plan(
        last_full_utc=_NOW - timedelta(days=7) + timedelta(hours=3),
        cfg=_cfg(full_threshold_pct=97.0, target_pct=100.0),
    )
    assert plan.full_target_kwh == pytest.approx(10.0)
    # And the reset still keys off 97, not 100.
    assert observe_full_charge(
        config=_cfg(full_threshold_pct=97.0, target_pct=100.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.7,
        now=_NOW,
    ) == _NOW


def test_hold_hours_become_whole_steps() -> None:
    plan = _plan(last_full_utc=_NOW, cfg=_cfg(hold_hours=2.0))
    assert plan.hold_steps == 8
    # A partial step rounds up: the hold is a minimum, not an estimate.
    assert _plan(last_full_utc=_NOW, cfg=_cfg(hold_hours=0.3)).hold_steps == 2
    assert _plan(last_full_utc=_NOW, cfg=_cfg(hold_hours=0.0)).hold_steps == 0


def test_a_disabled_policy_has_no_hold() -> None:
    assert _plan(last_full_utc=_NOW, cfg=_cfg(enabled=False)).hold_steps == 0


def test_a_touch_does_not_complete_a_hold() -> None:
    """One reading at the top is a touch, not a balance charge."""
    from mimirheim.core.battery_care import track_full_charge

    above_since, full = track_full_charge(
        config=_cfg(hold_hours=2.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.8,
        now=_NOW,
        above_since_utc=None,
    )
    assert above_since == _NOW
    assert full is None


def test_a_sustained_hold_completes_and_records_the_completion_time() -> None:
    """The timestamp is when the hold finished, not when the SOC crossed the line.

    That is the moment the cells have had their time at the top, and it is the
    reference the next interval should run from.
    """
    from mimirheim.core.battery_care import track_full_charge

    crossed = _NOW
    above_since, full = track_full_charge(
        config=_cfg(hold_hours=2.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.8,
        now=crossed + timedelta(hours=2),
        above_since_utc=crossed,
    )
    assert above_since == crossed
    assert full == crossed + timedelta(hours=2)


def test_a_reading_just_short_of_the_hold_does_not_complete_it() -> None:
    from mimirheim.core.battery_care import track_full_charge

    _, full = track_full_charge(
        config=_cfg(hold_hours=2.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.8,
        now=_NOW + timedelta(hours=1, minutes=59),
        above_since_utc=_NOW,
    )
    assert full is None


def test_a_dip_below_the_threshold_restarts_the_hold() -> None:
    """Time at the top only counts while it is contiguous."""
    from mimirheim.core.battery_care import track_full_charge

    above_since, full = track_full_charge(
        config=_cfg(hold_hours=2.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.69,
        now=_NOW + timedelta(hours=1),
        above_since_utc=_NOW,
    )
    assert above_since is None
    assert full is None


def test_a_dip_after_the_hold_has_elapsed_completes_it() -> None:
    """Silence means unchanged, so the pack was at the top until this reading.

    9.8 at noon, nothing for three hours, then 9.6: under the same rule that
    lets a constant 100% complete a hold without a single reading, that is
    three hours at the top followed by leaving it now. The run ends, and the
    completion is recorded at the moment the pack left.
    """
    from mimirheim.core.battery_care import track_full_charge

    above_since, full = track_full_charge(
        config=_cfg(hold_hours=2.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.6,
        now=_NOW + timedelta(hours=3),
        above_since_utc=_NOW,
    )
    assert above_since is None
    assert full == _NOW + timedelta(hours=3)


def test_a_zero_hold_is_a_touch() -> None:
    """hold_hours=0 reproduces the pre-hold behaviour exactly."""
    from mimirheim.core.battery_care import track_full_charge

    above_since, full = track_full_charge(
        config=_cfg(hold_hours=0.0),
        capacity_kwh=_CAPACITY,
        soc_kwh=9.7,
        now=_NOW,
        above_since_utc=None,
    )
    assert full == _NOW


def test_a_disabled_policy_tracks_nothing() -> None:
    from mimirheim.core.battery_care import track_full_charge

    assert track_full_charge(
        config=_cfg(enabled=False),
        capacity_kwh=_CAPACITY,
        soc_kwh=10.0,
        now=_NOW,
        above_since_utc=_NOW - timedelta(hours=5),
    ) == (None, None)
