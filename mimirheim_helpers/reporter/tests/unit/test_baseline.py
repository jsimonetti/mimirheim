"""Unit tests for reporter/baseline.py: the naive flexible load across plans."""
from __future__ import annotations

import pytest

from reporter.baseline import naive_flexible_kw, naive_overrides


def _plan(t: str, soc: float, end_soc: float, power_kw: float) -> tuple[dict, dict]:
    """A dump pair for one plan: a charge-only car at ``soc`` that the plan
    takes to ``end_soc``, on a charger of ``power_kw`` at 100%."""
    inp = {
        "solve_time_utc": t,
        "config": {
            "ev_chargers": {
                "car": {"charge_segments": [{"power_max_kw": power_kw, "efficiency": 1.0}]},
            },
        },
        "ev_inputs": {"car": {"soc_kwh": soc, "available": True}},
    }
    out = {
        "schedule": [
            {"t": t, "devices": {"car": {"kw": 0.0, "type": "ev_charger", "soc_kwh": soc}}},
            {"t": t, "devices": {"car": {"kw": 0.0, "type": "ev_charger", "soc_kwh": end_soc}}},
        ],
    }
    return inp, out


def test_each_quarter_charges_at_its_own_plans_rate() -> None:
    """The car needs 2 kWh and waits through the first quarter, when the
    charger was configured at 4 kW. That quarter's naive car takes 1 kWh, as
    much as 4 kW gives in 15 minutes, even though the charger is at 8 kW by
    the current plan. The current plan's naive car then still owes 1 kWh."""
    earlier_inp, earlier_out = _plan("2026-11-02T07:00:00Z", 20.0, 22.0, 4.0)
    inp, out = _plan("2026-11-02T07:15:00Z", 20.0, 22.0, 8.0)

    per_quarter, current = naive_overrides(
        [(earlier_out["schedule"][0], earlier_inp, earlier_out)], inp, out, 0.25
    )

    assert per_quarter[0].ev_needed["car"] == pytest.approx(2.0)
    assert current.ev_needed["car"] == pytest.approx(1.0)


def test_unplugging_ends_the_session() -> None:
    """After an unplugged quarter the naive car starts over: the energy the
    previous session's naive car took no longer counts against the new one."""
    first_inp, first_out = _plan("2026-11-02T07:00:00Z", 20.0, 22.0, 8.0)
    gone_inp, gone_out = _plan("2026-11-02T07:15:00Z", 20.0, 22.0, 8.0)
    gone_inp["ev_inputs"]["car"]["available"] = False
    inp, out = _plan("2026-11-02T07:30:00Z", 20.0, 22.0, 8.0)
    history = [
        (first_out["schedule"][0], first_inp, first_out),
        (gone_out["schedule"][0], gone_inp, gone_out),
    ]

    _, current = naive_overrides(history, inp, out, 0.25)

    assert current.ev_needed["car"] == pytest.approx(2.0)


def _dw_plan(
    t: str,
    draws: list[float],
    *,
    window: tuple[str, str] | None = None,
    recommended: str | None = None,
    committed: str | None = None,
    profile: list[float] | None = None,
) -> tuple[dict, dict]:
    """A dump pair for a plan with a dishwasher (by default one step of 2 kW)
    drawing ``draws`` kW per step, 15 minutes apart from ``t``."""
    from datetime import datetime, timedelta

    t0 = datetime.fromisoformat(t.replace("Z", "+00:00"))
    inp: dict = {
        "solve_time_utc": t,
        "config": {"deferrable_loads": {"dishwasher": {"power_profile": profile or [2.0]}}},
    }
    if window:
        inp["deferrable_windows"] = {"dishwasher": {"earliest": window[0], "latest": window[1]}}
    if committed:
        inp["deferrable_start_times"] = {"dishwasher": committed}
    out = {
        "schedule": [
            {
                "t": (t0 + timedelta(minutes=15 * i)).isoformat(),
                "devices": {"dishwasher": {"kw": -kw, "type": "deferrable_load"}},
            }
            for i, kw in enumerate(draws)
        ],
        "deferrable_recommended_starts": {"dishwasher": recommended} if recommended else {},
    }
    return inp, out


def _entry(pair: tuple[dict, dict]) -> tuple[dict, dict, dict]:
    inp, out = pair
    return out["schedule"][0], inp, out


def _current_naive(history: list, inp: dict, out: dict) -> list[float]:
    """The naive flexible load the current plan ends up with: its overrides
    where there are any, its own rule elsewhere."""
    _, current = naive_overrides(history, inp, out, 0.25)
    return naive_flexible_kw(
        inp, out["schedule"], 0.25, out.get("deferrable_recommended_starts") or {}, current
    )


def test_a_cancelled_window_leaves_no_naive_run() -> None:
    """Scheduled for noon at 08:00, then cancelled at 09:00 before it ran:
    the plan never runs it, so neither does the baseline."""
    scheduled = _dw_plan(
        "2026-11-02T07:00:00Z", [0.0] * 24,
        window=("2026-11-02T11:00:00Z", "2026-11-02T14:00:00Z"),
        recommended="2026-11-02T11:00:00Z",
    )
    inp, out = _dw_plan("2026-11-02T08:00:00Z", [0.0] * 24)

    assert _current_naive([_entry(scheduled)], inp, out) == [0.0] * 24


def test_a_moved_window_counts_one_naive_run() -> None:
    """Scheduled for a window from noon, then moved to one from 14:00 before
    it ran: one run in the plan, one in the baseline, at the new window."""
    scheduled = _dw_plan(
        "2026-11-02T07:00:00Z", [0.0] * 32,
        window=("2026-11-02T11:00:00Z", "2026-11-02T15:00:00Z"),
        recommended="2026-11-02T11:00:00Z",
    )
    draws = [0.0] * 24
    draws[20] = 2.0  # 13:00 UTC
    inp, out = _dw_plan(
        "2026-11-02T08:00:00Z", draws,
        window=("2026-11-02T13:00:00Z", "2026-11-02T15:00:00Z"),
        recommended="2026-11-02T13:00:00Z",
    )

    expected = [0.0] * 24
    expected[20] = 2.0
    assert _current_naive([_entry(scheduled)], inp, out) == expected


def test_the_naive_car_keeps_charging_between_missing_dumps() -> None:
    """At 23:00 the car needs 4 kWh on an 8 kW charger: the naive car is done
    by 23:30. With no dumps between 23:00 and 00:00, the naive car must not
    still owe energy at midnight."""
    late = _plan("2026-11-02T22:00:00Z", 20.0, 24.0, 8.0)
    midnight = _plan("2026-11-02T23:00:00Z", 20.0, 24.0, 8.0)
    inp, out = _plan("2026-11-02T23:15:00Z", 20.0, 24.0, 8.0)

    per_quarter, current = naive_overrides(
        [_entry(late), _entry(midnight)], inp, out, 0.25
    )

    assert per_quarter[0].ev_needed["car"] == pytest.approx(4.0)
    assert per_quarter[1].ev_needed["car"] == pytest.approx(0.0)
    assert current.ev_needed["car"] == pytest.approx(0.0)


def test_a_window_moved_beyond_the_horizon_leaves_no_naive_run() -> None:
    """Scheduled for 10:00, then moved to tomorrow, beyond this plan: the
    plan no longer delivers it today, so neither does the baseline."""
    scheduled = _dw_plan(
        "2026-11-02T07:00:00Z", [0.0] * 16,
        window=("2026-11-02T09:00:00Z", "2026-11-02T12:00:00Z"),
        recommended="2026-11-02T09:00:00Z",
    )
    inp, out = _dw_plan(
        "2026-11-02T07:15:00Z", [0.0] * 16,
        window=("2026-11-04T09:00:00Z", "2026-11-04T12:00:00Z"),
    )

    assert _current_naive([_entry(scheduled)], inp, out) == [0.0] * 16


def test_a_stale_commit_does_not_keep_a_cancelled_window() -> None:
    """Yesterday's start time is still retained. A window scheduled for
    10:00 and cleared at 08:00 leaves no naive run."""
    stale = "2026-11-01T18:00:00Z"
    scheduled = _dw_plan(
        "2026-11-02T07:00:00Z", [0.0] * 16,
        window=("2026-11-02T09:00:00Z", "2026-11-02T12:00:00Z"),
        recommended="2026-11-02T09:00:00Z", committed=stale,
    )
    inp, out = _dw_plan("2026-11-02T07:15:00Z", [0.0] * 16, committed=stale)

    assert _current_naive([_entry(scheduled)], inp, out) == [0.0] * 16


def test_earlier_quarters_draw_deferrable_loads_as_planned() -> None:
    """Deferrable loads are not followed across plans: an earlier quarter's
    run is drawn when it ran, with no override from the session tracker."""
    morning = _dw_plan(
        "2026-11-02T07:00:00Z", [0.0, 2.0],
        window=("2026-11-02T07:00:00Z", "2026-11-02T09:00:00Z"),
        recommended="2026-11-02T07:15:00Z",
    )
    ran = _dw_plan("2026-11-02T07:15:00Z", [2.0], committed="2026-11-02T07:15:00Z")
    inp, out = _dw_plan("2026-11-02T07:30:00Z", [0.0], committed="2026-11-02T07:15:00Z")
    history = [_entry(morning), _entry(ran)]

    per_quarter, _ = naive_overrides(history, inp, out, 0.25)

    drawn = [
        naive_flexible_kw(pinp, [step], 0.25, {}, over)
        for (step, pinp, _), over in zip(history, per_quarter, strict=True)
    ]
    assert drawn == [[0.0], [2.0]]
