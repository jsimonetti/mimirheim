"""Unit tests for pv_fetcher.confidence.

Tests verify:
- confidence_for_step returns 0.90 for steps 0–6 h ahead.
- confidence_for_step returns 0.75 for steps 6–24 h ahead.
- confidence_for_step returns 0.55 for steps 24–48 h ahead.
- confidence_for_step returns 0.35 for steps 48+ h ahead.
- apply_confidence returns the correct confidence for each step.
- Custom decay values from config are used.
- fill_night_gaps inserts zero-watt entries for missing hourly slots.
- apply_confidence calls fill_night_gaps so night hours are present at kw=0.
"""

from datetime import datetime, timedelta, timezone

from pv_fetcher.confidence import ConfidenceDecay, apply_confidence, fill_night_gaps


def _decay() -> ConfidenceDecay:
    return ConfidenceDecay(
        hours_0_to_6=0.90,
        hours_6_to_24=0.75,
        hours_24_to_48=0.55,
        hours_48_plus=0.35,
    )


def _now() -> datetime:
    return datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)


def test_confidence_0_to_6h() -> None:
    now = _now()
    decay = _decay()
    for h in [0, 1, 3, 5]:
        ts = now + timedelta(hours=h)
        assert decay.confidence_for_step(ts, now) == 0.90, f"h={h}"


def test_confidence_6_to_24h() -> None:
    now = _now()
    decay = _decay()
    for h in [6, 7, 12, 23]:
        ts = now + timedelta(hours=h)
        assert decay.confidence_for_step(ts, now) == 0.75, f"h={h}"


def test_confidence_24_to_48h() -> None:
    now = _now()
    decay = _decay()
    for h in [24, 30, 47]:
        ts = now + timedelta(hours=h)
        assert decay.confidence_for_step(ts, now) == 0.55, f"h={h}"


def test_confidence_48h_plus() -> None:
    now = _now()
    decay = _decay()
    for h in [48, 60, 72]:
        ts = now + timedelta(hours=h)
        assert decay.confidence_for_step(ts, now) == 0.35, f"h={h}"


def test_apply_confidence_attaches_correct_values() -> None:
    now = _now()
    decay = _decay()

    # watts: {ts: watts_int} — three steps in different confidence bands.
    # Night gaps between the steps will be zero-filled automatically.
    watts = {
        now + timedelta(hours=3): 3000,    # 0–6 h → 0.90
        now + timedelta(hours=10): 4000,   # 6–24 h → 0.75
        now + timedelta(hours=30): 2000,   # 24–48 h → 0.55
    }

    steps = apply_confidence(watts, now, decay)

    # Gap-filling inserts zeros for missing hourly slots; check by key lookup
    # rather than total count so the test is not sensitive to the filled count.
    by_ts = {s["ts"]: s for s in steps}

    early_ts = (now + timedelta(hours=3)).isoformat()
    assert by_ts[early_ts]["kw"] == pytest.approx(3.0)
    assert by_ts[early_ts]["confidence"] == 0.90

    mid_ts = (now + timedelta(hours=10)).isoformat()
    assert by_ts[mid_ts]["kw"] == pytest.approx(4.0)
    assert by_ts[mid_ts]["confidence"] == 0.75

    late_ts = (now + timedelta(hours=30)).isoformat()
    assert by_ts[late_ts]["kw"] == pytest.approx(2.0)
    assert by_ts[late_ts]["confidence"] == 0.55

    # All hours between the first and last step must be present.
    assert len(steps) == 28  # hours 3..30 inclusive = 28 steps


def test_apply_confidence_converts_watts_to_kw() -> None:
    now = _now()
    decay = _decay()
    watts = {now: 1500}
    steps = apply_confidence(watts, now, decay)
    assert steps[0]["kw"] == pytest.approx(1.5)


def test_apply_confidence_ts_is_utc_iso8601() -> None:
    """Each step's ts field is a UTC ISO 8601 string with +00:00 offset."""
    now = _now()
    decay = _decay()
    watts = {now: 1000}
    steps = apply_confidence(watts, now, decay)
    assert steps[0]["ts"].endswith("+00:00")


# ---------------------------------------------------------------------------
# fill_night_gaps
# ---------------------------------------------------------------------------


def _h(base: datetime, hours: float) -> datetime:
    return base + timedelta(hours=hours)


def test_fill_night_gaps_inserts_zeros_for_missing_hours() -> None:
    """Hourly slots between the first and last timestamp that are absent from
    the API response are inserted with watts=0."""
    base = datetime(2026, 6, 1, 7, 0, 0, tzinfo=timezone.utc)
    # API returned hours 7, 8, 10, 12 — hour 9 and 11 are missing.
    watts = {
        _h(base, 0): 1000,
        _h(base, 1): 2000,
        _h(base, 3): 1500,
        _h(base, 5): 500,
    }

    filled = fill_night_gaps(watts)

    for offset_h in range(6):
        assert _h(base, offset_h) in filled, f"Missing slot at h+{offset_h}"
    assert filled[_h(base, 2)] == 0
    assert filled[_h(base, 4)] == 0
    # Original values are preserved.
    assert filled[_h(base, 0)] == 1000
    assert filled[_h(base, 3)] == 1500


def test_fill_night_gaps_two_day_forecast() -> None:
    """Night hours between day 1 dusk and day 2 dawn are filled with zeros."""
    # Day 1: 08:00–19:00 (12 steps); Day 2: 07:00–18:00 (12 steps).
    # Night gap: 19:00 day1 to 07:00 day2 = 12 missing hours.
    day1_dusk = datetime(2026, 6, 1, 19, 0, 0, tzinfo=timezone.utc)
    day2_dawn = datetime(2026, 6, 2, 7, 0, 0, tzinfo=timezone.utc)

    watts: dict[datetime, int] = {}
    for h in range(12):
        watts[day1_dusk - timedelta(hours=11 - h)] = max(0, 3000 - h * 200)
    for h in range(12):
        watts[day2_dawn + timedelta(hours=h)] = max(0, h * 200)

    filled = fill_night_gaps(watts)

    # All night hours must be present and zero.
    night_ts = day1_dusk + timedelta(hours=1)
    while night_ts < day2_dawn:
        assert filled[night_ts] == 0, f"Night slot {night_ts} not zero"
        night_ts += timedelta(hours=1)


def test_fill_night_gaps_no_change_when_contiguous() -> None:
    """A fully contiguous dict is returned unchanged."""
    base = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
    watts = {base + timedelta(hours=h): h * 100 for h in range(5)}

    filled = fill_night_gaps(watts)

    assert filled == watts


def test_fill_night_gaps_single_entry_unchanged() -> None:
    """A single-entry dict is returned unchanged (nothing to infer or fill)."""
    ts = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    watts = {ts: 5000}
    assert fill_night_gaps(watts) == watts


def test_fill_night_gaps_subhourly_sunrise_marker_does_not_inject_zeros_mid_hour() -> None:
    """A sub-hourly sunrise marker does not cause zeros to be injected inside hourly windows.

    forecast.solar includes a sunrise boundary marker at an exact sub-hourly
    time (e.g. 05:07:58Z).  Without floor-snapping, fill_night_gaps would walk
    the fill grid from 05:07:58 in 1-hour steps, generating 06:07:58, 07:07:58,
    etc.  These land inside hourly data windows; the step-function resampler
    would then pick 07:07:58=0 as the ``before`` value for t=07:15, t=07:30,
    t=07:45 and return zero for three out of four quarter-hour slots per hour.
    """
    sunrise = datetime(2026, 4, 2, 5, 7, 58, tzinfo=timezone.utc)
    watts = {
        sunrise: 0,
        datetime(2026, 4, 2, 6, 0, 0, tzinfo=timezone.utc): 200,
        datetime(2026, 4, 2, 7, 0, 0, tzinfo=timezone.utc): 1000,
        datetime(2026, 4, 2, 8, 0, 0, tzinfo=timezone.utc): 2000,
    }

    filled = fill_night_gaps(watts)

    # No zero must be injected strictly between 07:00 and 08:00.
    h7 = datetime(2026, 4, 2, 7, 0, 0, tzinfo=timezone.utc)
    h8 = datetime(2026, 4, 2, 8, 0, 0, tzinfo=timezone.utc)
    injected = [ts for ts, w in filled.items() if h7 < ts < h8 and w == 0]
    assert injected == [], f"Unexpected zero entries inside 07:00–08:00 window: {injected}"

    # The original hourly data must be preserved exactly.
    assert filled[h7] == 1000
    assert filled[h8] == 2000


def test_apply_confidence_night_steps_have_zero_kw() -> None:
    """Night hours are present in the apply_confidence output with kw=0.0.

    forecast.solar omits them; apply_confidence must fill them so that
    resample_power does not interpolate positive values during the night.
    """
    base = datetime(2026, 6, 1, 18, 0, 0, tzinfo=timezone.utc)  # 18:00 = dusk
    fetch_time = base - timedelta(hours=1)
    decay = _decay()

    # API returned only two daytime steps from consecutive days.
    # The 14-hour night gap (18:00–08:00) has no entries.
    watts = {
        base: 200,                        # last evening step
        base + timedelta(hours=14): 200,  # first morning step next day
    }

    steps = apply_confidence(watts, fetch_time, decay)

    # Every hour between 19:00 and 07:00 must be present with kw=0.
    by_ts = {s["ts"]: s["kw"] for s in steps}
    for h in range(1, 14):
        ts_str = (base + timedelta(hours=h)).isoformat()
        assert ts_str in by_ts, f"Night step {ts_str} missing"
        assert by_ts[ts_str] == pytest.approx(0.0), (
            f"Night step {ts_str} should be 0.0 kW, got {by_ts[ts_str]}"
        )


import pytest
