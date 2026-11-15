"""Unit tests for household._earlier: picking earlier quarters and EV session breaks."""
from __future__ import annotations

from datetime import UTC, datetime

from reporter.household import _earlier


def _pair(t: str, made: str, available: bool) -> tuple[dict, dict]:
    """A dump pair whose plan starts at ``t``, made at ``made``, with the car
    plugged in or not."""
    inp = {
        "triggered_at_utc": made,
        "ev_inputs": {"car": {"soc_kwh": 20.0, "available": available}},
    }
    out = {"schedule": [{"t": t, "devices": {}}]}
    return inp, out


def test_an_unplug_seen_only_in_a_dropped_dump_still_breaks_the_session() -> None:
    """Unplugged at 07:16 and plugged back in at 07:20: both dumps start at
    07:15, and only the later is kept. Its quarter still carries the break."""
    pairs = [
        _pair("2026-11-02T07:00:00Z", "2026-11-02T07:00:30Z", True),
        _pair("2026-11-02T07:15:00Z", "2026-11-02T07:16:00Z", False),
        _pair("2026-11-02T07:15:00Z", "2026-11-02T07:20:00Z", True),
    ]
    first = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)

    entries, breaks = _earlier(pairs, first, UTC, "2026-11-02T07:30:30Z")

    assert [inp["triggered_at_utc"] for _, _, inp, _ in entries] == [
        "2026-11-02T07:00:30Z",
        "2026-11-02T07:20:00Z",
    ]
    assert breaks == [set(), {"car"}, set()]


def test_an_unplug_in_the_current_quarter_breaks_the_current_plan() -> None:
    """Unplugged at 07:31 and plugged back in by the current plan at 07:35:
    the older dump of the current quarter is not history, but its unplug ends
    the session before the current plan."""
    pairs = [
        _pair("2026-11-02T07:15:00Z", "2026-11-02T07:15:30Z", True),
        _pair("2026-11-02T07:30:00Z", "2026-11-02T07:31:00Z", False),
    ]
    first = datetime(2026, 11, 2, 7, 30, tzinfo=UTC)

    entries, breaks = _earlier(pairs, first, UTC, "2026-11-02T07:35:00Z")

    assert len(entries) == 1
    assert breaks == [set(), {"car"}]
