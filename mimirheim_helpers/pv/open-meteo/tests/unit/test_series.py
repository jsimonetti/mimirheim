"""Unit tests for pv_openmeteo.series.

Tests verify:
- Confidence bands are selected from the distance between fetch time and step.
- trim_history drops steps older than the retention window and keeps the rest.
- apply_confidence converts a kW series into mimirheim forecast steps, sorted,
  with UTC ISO timestamps and rounded power values.
- Naive timestamps are treated as UTC rather than silently compared as local.
"""

from datetime import datetime, timedelta, timezone

from pv_openmeteo.series import ConfidenceDecay, apply_confidence, trim_history

_DECAY = ConfidenceDecay(
    hours_0_to_6=0.9,
    hours_6_to_24=0.75,
    hours_24_to_48=0.55,
    hours_48_plus=0.35,
)
_NOW = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)


def test_confidence_bands() -> None:
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=1), _NOW) == 0.9
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=12), _NOW) == 0.75
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=30), _NOW) == 0.55
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=60), _NOW) == 0.35


def test_confidence_band_boundaries_are_inclusive_at_the_lower_edge() -> None:
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=6), _NOW) == 0.75
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=24), _NOW) == 0.55
    assert _DECAY.confidence_for_step(_NOW + timedelta(hours=48), _NOW) == 0.35


def test_elapsed_steps_keep_the_nearest_band() -> None:
    """A step in the recent past is not a lower-confidence forecast."""
    assert _DECAY.confidence_for_step(_NOW - timedelta(minutes=45), _NOW) == 0.9


def test_trim_history_drops_steps_beyond_the_retention_window() -> None:
    series = {
        _NOW - timedelta(hours=3): 0.0,
        _NOW - timedelta(minutes=30): 1.0,
        _NOW + timedelta(hours=1): 2.0,
    }
    trimmed = trim_history(series, now=_NOW, past_hours=1.0)
    assert sorted(trimmed) == [_NOW - timedelta(minutes=30), _NOW + timedelta(hours=1)]


def test_trim_history_with_zero_window_keeps_only_the_future() -> None:
    series = {
        _NOW - timedelta(minutes=15): 1.0,
        _NOW: 2.0,
        _NOW + timedelta(minutes=15): 3.0,
    }
    trimmed = trim_history(series, now=_NOW, past_hours=0.0)
    assert sorted(trimmed) == [_NOW, _NOW + timedelta(minutes=15)]


def test_apply_confidence_builds_sorted_mimirheim_steps() -> None:
    series = {
        _NOW + timedelta(hours=12): 1.23456,
        _NOW + timedelta(hours=1): 0.0,
    }
    steps = apply_confidence(series, _NOW, _DECAY)
    assert steps == [
        {"ts": "2026-03-30T13:00:00+00:00", "kw": 0.0, "confidence": 0.9},
        {"ts": "2026-03-31T00:00:00+00:00", "kw": 1.235, "confidence": 0.75},
    ]


def test_apply_confidence_normalises_timestamps_to_utc() -> None:
    local = timezone(timedelta(hours=2))
    series = {datetime(2026, 3, 30, 15, 0, tzinfo=local): 2.0}
    steps = apply_confidence(series, _NOW, _DECAY)
    assert steps[0]["ts"] == "2026-03-30T13:00:00+00:00"


def test_apply_confidence_treats_naive_input_as_utc() -> None:
    series = {datetime(2026, 3, 30, 13, 0): 2.0}
    steps = apply_confidence(series, _NOW.replace(tzinfo=None), _DECAY)
    assert steps[0]["ts"] == "2026-03-30T13:00:00+00:00"
    assert steps[0]["confidence"] == 0.9


def test_apply_confidence_on_empty_series_returns_empty_list() -> None:
    assert apply_confidence({}, _NOW, _DECAY) == []
