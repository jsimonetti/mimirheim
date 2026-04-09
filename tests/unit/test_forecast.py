"""Unit tests for mimirheim/core/forecast.py.

Tests cover forecast horizon computation, gap detection, price step-function
resampling, and power linear-interpolation resampling.

All tests must fail before forecast.py exists.
"""

import pytest
from datetime import UTC, datetime, timedelta

from mimirheim.core.bundle import PowerForecastStep, PriceStep
from mimirheim.core.forecast import (
    compute_horizon_steps,
    find_gaps,
    floor_to_15min,
    resample_power,
    resample_prices,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(hour: int, minute: int = 0) -> datetime:
    """Return a UTC datetime on a fixed reference date at the given hour/minute."""
    return datetime(2026, 3, 30, hour, minute, tzinfo=UTC)


def _price(hour: int, imp: float = 0.20, exp: float = 0.05) -> PriceStep:
    return PriceStep(ts=_ts(hour), import_eur_per_kwh=imp, export_eur_per_kwh=exp)


def _power(hour: int, minute: int = 0, kw: float = 3.0) -> PowerForecastStep:
    return PowerForecastStep(ts=_ts(hour, minute), kw=kw)


# ---------------------------------------------------------------------------
# floor_to_15min
# ---------------------------------------------------------------------------


def test_floor_to_15min_already_aligned() -> None:
    """A timestamp already on a 15-min boundary is unchanged."""
    t = _ts(14, 0)
    assert floor_to_15min(t) == t


def test_floor_to_15min_rounds_down() -> None:
    """A timestamp at :07 floors to the prior :00 boundary."""
    t = _ts(14, 7)
    assert floor_to_15min(t) == _ts(14, 0)


def test_floor_to_15min_at_45() -> None:
    """A timestamp at :47 floors to :45."""
    t = datetime(2026, 3, 30, 14, 47, 33, tzinfo=UTC)
    expected = datetime(2026, 3, 30, 14, 45, tzinfo=UTC)
    assert floor_to_15min(t) == expected


# ---------------------------------------------------------------------------
# compute_horizon_steps
# ---------------------------------------------------------------------------


def test_compute_horizon_empty_series_returns_zero() -> None:
    """An empty forecast series yields a horizon of 0 steps."""
    solve_start = _ts(14, 0)
    assert compute_horizon_steps(solve_start, []) == 0


def test_compute_horizon_no_future_data_returns_zero() -> None:
    """A series with all timestamps before solve_start yields 0 steps."""
    solve_start = _ts(14, 0)
    steps = [_price(12), _price(13)]
    assert compute_horizon_steps(solve_start, steps) == 0


def test_compute_horizon_single_series_one_hour() -> None:
    """One price step 1 hour ahead → 4 fifteen-minute steps."""
    solve_start = _ts(14, 0)
    steps = [_price(14), _price(15)]
    assert compute_horizon_steps(solve_start, steps) == 4


def test_compute_horizon_single_series_fractional_hours() -> None:
    """Last step at 14:45 ahead of 14:00 solve_start → 3 steps."""
    solve_start = _ts(14, 0)
    steps = [_power(14, 0), _power(14, 45)]
    assert compute_horizon_steps(solve_start, steps) == 3


def test_compute_horizon_limited_by_shortest_series() -> None:
    """horizon_end = min(last_ts) across all series."""
    solve_start = _ts(14, 0)
    prices = [_price(h) for h in range(14, 24)]   # covers to 23:00
    pv = [_power(h) for h in range(14, 22)]        # covers to 21:00
    load = [_power(h) for h in range(14, 23)]      # covers to 22:00
    # horizon_end = 21:00 → (21:00 - 14:00) / 15min = 28 steps
    assert compute_horizon_steps(solve_start, prices, pv, load) == 28


def test_compute_horizon_missing_series_returns_zero() -> None:
    """If one mandatory series has no future data, horizon is 0."""
    solve_start = _ts(14, 0)
    prices = [_price(h) for h in range(14, 24)]
    pv_past = [_power(12), _power(13)]  # all in the past
    assert compute_horizon_steps(solve_start, prices, pv_past) == 0


def test_compute_horizon_solve_start_between_steps() -> None:
    """solve_start between two data points: last step >= solve_start is used."""
    # Hourly prices 12:00–16:00, solve_start at 14:30.
    solve_start = _ts(14, 30)
    steps = [_price(h) for h in range(12, 17)]  # 12,13,14,15,16
    # Last step at or after 14:30 is 16:00.
    # horizon_end = 16:00 → (16:00 - 14:30) / 15min = 90 / 15 = 6 steps.
    assert compute_horizon_steps(solve_start, steps) == 6


# ---------------------------------------------------------------------------
# find_gaps
# ---------------------------------------------------------------------------


def test_find_gaps_no_gaps() -> None:
    """Hourly steps with no gap exceeding max_gap_hours returns empty list."""
    steps = [_price(h) for h in range(14, 20)]
    gaps = find_gaps(steps, _ts(14), _ts(19), max_gap_hours=2.0)
    assert gaps == []


def test_find_gaps_detects_gap() -> None:
    """A 3-hour gap between 15:00 and 18:00 is reported."""
    steps = [_price(14), _price(15), _price(18), _price(19)]
    gaps = find_gaps(steps, _ts(14), _ts(19), max_gap_hours=2.0)
    assert len(gaps) == 1
    gap_start, gap_end = gaps[0]
    assert gap_start == _ts(15)
    assert gap_end == _ts(18)


def test_find_gaps_ignores_outside_range() -> None:
    """A gap before solve_start is not reported even if larger than max_gap_hours.

    The gap from 10:00 to 14:00 (4 hours) lies entirely before solve_start
    so it is not counted. Within [14:00, 18:00] all consecutive gaps are
    1 hour, which is below the 2-hour threshold.
    """
    steps = [_price(10), _price(14), _price(15), _price(16), _price(17), _price(18)]
    gaps = find_gaps(steps, _ts(14), _ts(18), max_gap_hours=2.0)
    assert gaps == []


def test_find_gaps_threshold_exactly_equal_not_flagged() -> None:
    """A gap of exactly max_gap_hours is NOT reported (only strictly greater)."""
    steps = [_price(14), _price(16), _price(18)]  # 2-hour gaps
    gaps = find_gaps(steps, _ts(14), _ts(18), max_gap_hours=2.0)
    assert gaps == []


# ---------------------------------------------------------------------------
# resample_prices (step function)
# ---------------------------------------------------------------------------


def test_resample_prices_constant_within_period() -> None:
    """Price at 14:00 applies to all 15-min slots before the next step (15:00)."""
    steps = [_price(14, imp=0.20), _price(15, imp=0.30)]
    imp, exp, conf = resample_prices(steps, _ts(14), 4)
    # All 4 steps (14:00, 14:15, 14:30, 14:45) use the 14:00 price.
    assert imp == pytest.approx([0.20, 0.20, 0.20, 0.20])


def test_resample_prices_step_changes_at_boundary() -> None:
    """Step at 15:00 takes effect at the 15:00 slot."""
    steps = [_price(14, imp=0.20), _price(15, imp=0.30)]
    imp, exp, conf = resample_prices(steps, _ts(14), 8)
    # Slots 0–3 = 14:00..14:45 → 0.20; slots 4–7 = 15:00..15:45 → 0.30.
    assert imp[:4] == pytest.approx([0.20] * 4)
    assert imp[4:] == pytest.approx([0.30] * 4)


def test_resample_prices_export_and_confidence() -> None:
    """Export price and confidence are resampled independently."""
    steps = [
        PriceStep(ts=_ts(14), import_eur_per_kwh=0.20, export_eur_per_kwh=0.05, confidence=0.9),
        PriceStep(ts=_ts(15), import_eur_per_kwh=0.25, export_eur_per_kwh=0.07, confidence=0.8),
    ]
    imp, exp, conf = resample_prices(steps, _ts(14), 8)
    assert exp[:4] == pytest.approx([0.05] * 4)
    assert exp[4:] == pytest.approx([0.07] * 4)
    assert conf[:4] == pytest.approx([0.9] * 4)
    assert conf[4:] == pytest.approx([0.8] * 4)


def test_resample_prices_unsorted_input() -> None:
    """resample_prices handles unsorted input steps correctly."""
    steps = [_price(15, imp=0.30), _price(14, imp=0.20)]  # reversed
    imp, exp, conf = resample_prices(steps, _ts(14), 4)
    assert imp == pytest.approx([0.20] * 4)


def test_resample_prices_no_step_before_solve_start() -> None:
    """If all steps are after solve_start, the first step's price is used."""
    steps = [_price(15, imp=0.30)]
    imp, exp, conf = resample_prices(steps, _ts(14), 4)
    # Only one step exists (at 15:00), after the solve_start (14:00). Use it.
    assert imp == pytest.approx([0.30] * 4)


# ---------------------------------------------------------------------------
# resample_power (step function / hold-previous)
# ---------------------------------------------------------------------------


def test_resample_power_at_exact_timestamps() -> None:
    """Resampling at an exact step timestamp returns the exact value."""
    steps = [_power(14, 0, kw=3.2), _power(15, 0, kw=2.8)]
    result = resample_power(steps, _ts(14), 1)
    assert result == pytest.approx([3.2])


def test_resample_power_step_within_hour() -> None:
    """All 15-minute slots within an hourly interval hold the interval value."""
    steps = [_power(14, 0, kw=4.0), _power(15, 0, kw=0.0)]
    result = resample_power(steps, _ts(14), 4)
    # All four slots (14:00, 14:15, 14:30, 14:45) fall within [14:00, 15:00)
    # and therefore receive the interval's declared average of 4.0 kW.
    assert result == pytest.approx([4.0, 4.0, 4.0, 4.0])


def test_resample_power_multiple_segments() -> None:
    """Non-uniform input timestamps are handled with correct bracketing."""
    steps = [_power(14, 0, kw=6.0), _power(14, 30, kw=3.0), _power(15, 0, kw=0.0)]
    result = resample_power(steps, _ts(14), 4)
    # Segment [14:00, 14:30): t=14:00 and t=14:15 both hold 6.0 kW.
    # Segment [14:30, 15:00): t=14:30 and t=14:45 both hold 3.0 kW.
    assert result == pytest.approx([6.0, 6.0, 3.0, 3.0])


def test_resample_power_unsorted_input() -> None:
    """resample_power handles unsorted input steps correctly."""
    steps = [_power(15, 0, kw=2.8), _power(14, 0, kw=3.2)]  # reversed
    result = resample_power(steps, _ts(14), 1)
    assert result == pytest.approx([3.2])


def test_resample_power_constant_single_step() -> None:
    """A single data point results in a constant forecast."""
    steps = [_power(14, 0, kw=5.0)]
    result = resample_power(steps, _ts(14), 1)
    assert result == pytest.approx([5.0])
