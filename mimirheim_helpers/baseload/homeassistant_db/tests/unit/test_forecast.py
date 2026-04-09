"""Unit tests for baseload_ha.forecast.

Covers the same-hour averaging and horizon-filling logic.

After Plan 51, ``HourlyProfile.from_readings`` no longer accepts a ``unit``
parameter. All unit conversion happens inside ``fetch_statistics``; readings
arrive as kWh/h values. ``build_forecast`` similarly removes ``sum_units`` and
``subtract_units`` arguments.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from baseload_ha_db.forecast import build_forecast, HourlyProfile


# Monday 2026-03-30 14:00 UTC — used as a stable "now" for all tests.
_NOW = datetime(2026, 3, 30, 14, 0, 0, tzinfo=timezone.utc)


def _make_readings(
    entity_id: str,
    lookback_days: int,
    base_now: datetime,
    value_by_hour: dict[int, float],
) -> dict[str, list[dict]]:
    """Build a synthetic HA statistics response for a single entity.

    Produces hourly mean readings for lookback_days days before base_now.
    value_by_hour maps hour-of-day (0-23) to a kWh/h mean value.
    Missing hours are omitted from the result.
    """
    readings: list[dict] = []
    start = (base_now - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    for day_offset in range(lookback_days):
        day_start = start + timedelta(days=day_offset)
        for hour, value in value_by_hour.items():
            readings.append(
                {
                    "start": (day_start + timedelta(hours=hour)).isoformat(),
                    "mean": value,
                }
            )
    return {entity_id: readings}


class TestHourlyProfile:
    def test_averages_same_hour_across_days(self) -> None:
        # Two days of kWh/h readings: 0.2 and 0.4 at hour 14. Mean = 0.3 kWh/h.
        readings: dict[str, list[dict]] = {}
        base = _NOW - timedelta(days=2)
        for day_offset, val in enumerate([0.2, 0.4]):
            ts = (base + timedelta(days=day_offset)).replace(hour=14).isoformat()
            readings.setdefault("sensor.power_w", []).append({"start": ts, "mean": val})

        profile = HourlyProfile.from_readings(readings["sensor.power_w"])
        assert profile.kw_for_hour(14) == pytest.approx(0.3)

    def test_kwh_values_pass_through_unchanged(self) -> None:
        """A reading whose mean is already 3.5 kWh/h must produce kw_for_hour == 3.5."""
        ts = _NOW.replace(hour=10).isoformat()
        readings = [{"start": ts, "mean": 3.5}]
        profile = HourlyProfile.from_readings(readings)
        assert profile.kw_for_hour(10) == pytest.approx(3.5)

    def test_missing_hour_falls_back_to_global_mean(self) -> None:
        # Provide only hour 10 with value 2.0 kWh/h; asking for hour 5 (no data)
        # should fall back to the global mean.
        ts = _NOW.replace(hour=10).isoformat()
        readings = [{"start": ts, "mean": 2.0}]
        profile = HourlyProfile.from_readings(readings)
        assert profile.kw_for_hour(5) == pytest.approx(2.0)

    def test_from_readings_does_not_accept_unit_parameter(self) -> None:
        """Passing unit= to from_readings must raise TypeError."""
        ts = _NOW.replace(hour=10).isoformat()
        readings = [{"start": ts, "mean": 1.0}]
        with pytest.raises(TypeError):
            HourlyProfile.from_readings(readings, unit="W")  # type: ignore[call-arg]


class TestBuildForecast:
    def test_24h_horizon_produces_24_steps(self) -> None:
        readings_sum = _make_readings("sensor.sum", 7, _NOW, {h: 0.5 for h in range(24)})
        steps = build_forecast(
            sum_readings={"sensor.sum": readings_sum["sensor.sum"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=24,
            lookback_days=7,
        )
        assert len(steps) == 24

    def test_first_step_starts_at_now(self) -> None:
        readings_sum = _make_readings("sensor.sum", 7, _NOW, {h: 0.5 for h in range(24)})
        steps = build_forecast(
            sum_readings={"sensor.sum": readings_sum["sensor.sum"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=4,
            lookback_days=7,
        )
        assert steps[0]["ts"] == _NOW.isoformat()

    def test_steps_are_contiguous_hourly(self) -> None:
        readings_sum = _make_readings("sensor.sum", 7, _NOW, {h: 0.5 for h in range(24)})
        steps = build_forecast(
            sum_readings={"sensor.sum": readings_sum["sensor.sum"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=6,
            lookback_days=7,
        )
        for i in range(1, len(steps)):
            prev = datetime.fromisoformat(steps[i - 1]["ts"])
            curr = datetime.fromisoformat(steps[i]["ts"])
            assert curr - prev == timedelta(hours=1)

    def test_subtracts_subtract_entities(self) -> None:
        # sum entity: 1.0 kWh/h, subtract entity: 0.3 kWh/h → net 0.7 kWh/h
        readings_sum = _make_readings("s.load", 7, _NOW, {14: 1.0})
        readings_sub = _make_readings("s.battery", 7, _NOW, {14: 0.3})
        steps = build_forecast(
            sum_readings={"s.load": readings_sum["s.load"]},
            subtract_readings={"s.battery": readings_sub["s.battery"]},
            now=_NOW,
            horizon_hours=1,
            lookback_days=7,
        )
        assert steps[0]["kw"] == pytest.approx(0.7)

    def test_net_kw_clamped_to_zero(self) -> None:
        # subtract exceeds sum: net would be negative, must be clamped to 0
        readings_sum = _make_readings("s.load", 7, _NOW, {14: 0.1})
        readings_sub = _make_readings("s.battery", 7, _NOW, {14: 0.5})
        steps = build_forecast(
            sum_readings={"s.load": readings_sum["s.load"]},
            subtract_readings={"s.battery": readings_sub["s.battery"]},
            now=_NOW,
            horizon_hours=1,
            lookback_days=7,
        )
        assert steps[0]["kw"] == 0.0

    def test_sums_multiple_sum_entities(self) -> None:
        r1 = _make_readings("s.l1", 7, _NOW, {14: 0.4})
        r2 = _make_readings("s.l2", 7, _NOW, {14: 0.6})
        steps = build_forecast(
            sum_readings={"s.l1": r1["s.l1"], "s.l2": r2["s.l2"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=7,
        )
        assert steps[0]["kw"] == pytest.approx(1.0)

    def test_horizon_beyond_24h_tiles_profile(self) -> None:
        # A distinctive profile: different value per hour (already kWh/h)
        hour_values = {h: float(h) * 0.1 for h in range(24)}
        readings_sum = _make_readings("s.load", 7, _NOW, hour_values)
        steps = build_forecast(
            sum_readings={"s.load": readings_sum["s.load"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=48,
            lookback_days=7,
        )
        assert len(steps) == 48
        # Step at index 24 should match step at index 0 (same hour of day)
        assert steps[0]["kw"] == pytest.approx(steps[24]["kw"])
        assert steps[1]["kw"] == pytest.approx(steps[25]["kw"])

    def test_sums_two_entities_at_same_hour(self) -> None:
        """Multiple sum entities with different kWh/h values are combined correctly."""
        # 1.0 kWh/h + 0.5 kWh/h = 1.5 kWh/h
        r1 = _make_readings("s.a", 7, _NOW, {14: 1.0})
        r2 = _make_readings("s.b", 7, _NOW, {14: 0.5})
        steps = build_forecast(
            sum_readings={"s.a": r1["s.a"], "s.b": r2["s.b"]},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=7,
        )
        assert steps[0]["kw"] == pytest.approx(1.5)

    def test_decay_weights_recent_readings_more(self) -> None:
        """With decay > 1, more recent days contribute more to the per-hour average."""
        # Two days at hour 14: oldest = 0.1 kWh/h, newest = 0.2 kWh/h.
        # lookback_days=2, decay=4.0:
        #   oldest weight = 4.0 ** (0 / 1) = 1.0
        #   newest weight = 4.0 ** (1 / 1) = 4.0
        # Weighted mean = (0.1 * 1 + 0.2 * 4) / (1 + 4) = 0.9 / 5 = 0.18 kWh/h.
        # A plain average would give (0.1 + 0.2) / 2 = 0.15 kWh/h.
        start = (_NOW - timedelta(days=2)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        readings = [
            {"start": (start + timedelta(hours=14)).isoformat(), "mean": 0.1},
            {"start": (start + timedelta(days=1, hours=14)).isoformat(), "mean": 0.2},
        ]
        steps = build_forecast(
            sum_readings={"sensor.p": readings},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
            lookback_decay=4.0,
        )
        # The first step is at hour 14 (same as _NOW's hour).
        assert steps[0]["kw"] == pytest.approx(0.18)
