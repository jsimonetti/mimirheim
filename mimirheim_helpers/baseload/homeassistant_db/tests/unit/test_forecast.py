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


class TestBuildForecastMergesPerTimestamp:
    """Entities are combined per timestamp before the hour-of-day average.

    These tests pin the semantics that make a sensor handover work: when an
    old entity stops recording and a new entity starts, the two together must
    read as one continuous series, not as two full-strength profiles added
    together.
    """

    @staticmethod
    def _readings_for_days(
        base_now: datetime,
        lookback_days: int,
        day_offsets: list[int],
        hour: int,
        value: float,
    ) -> list[dict]:
        """Hourly readings at ``hour`` for the given day offsets only."""
        start = (base_now - timedelta(days=lookback_days)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return [
            {
                "start": (start + timedelta(days=d, hours=hour)).isoformat(),
                "mean": value,
            }
            for d in day_offsets
        ]

    def test_sensor_handover_is_not_double_counted(self) -> None:
        # Old entity recorded 4.0 kWh/h at hour 14 on days 0-10, then stopped.
        # New entity recorded 4.0 kWh/h at hour 14 on days 11-13. Together they
        # describe one sensor at 4.0 kWh/h every day; the forecast must be 4.0,
        # not 8.0.
        old = self._readings_for_days(_NOW, 14, list(range(0, 11)), 14, 4.0)
        new = self._readings_for_days(_NOW, 14, list(range(11, 14)), 14, 4.0)
        steps = build_forecast(
            sum_readings={"s.envoy_old": old, "s.envoy_new": new},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=14,
        )
        assert steps[0]["kw"] == pytest.approx(4.0)

    def test_missing_sum_entity_at_a_timestamp_counts_as_zero(self) -> None:
        # Entity A is present on both days at 1.0. Entity B is present on day 0
        # only at 1.0 and missing on day 1. Per-timestamp merge gives
        # day 0: 2.0, day 1: 1.0, mean 1.5. Per-entity averaging would give 2.0.
        a = self._readings_for_days(_NOW, 2, [0, 1], 14, 1.0)
        b = self._readings_for_days(_NOW, 2, [0], 14, 1.0)
        steps = build_forecast(
            sum_readings={"s.a": a, "s.b": b},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
        )
        assert steps[0]["kw"] == pytest.approx(1.5)

    def test_missing_subtract_entity_at_a_timestamp_counts_as_zero(self) -> None:
        # Sum entity 1.0 on both days. Subtract entity 0.4 on day 0 only.
        # day 0: 0.6, day 1: 1.0, mean 0.8. Per-entity averaging would give 0.6.
        load = self._readings_for_days(_NOW, 2, [0, 1], 14, 1.0)
        ev = self._readings_for_days(_NOW, 2, [0], 14, 0.4)
        steps = build_forecast(
            sum_readings={"s.load": load},
            subtract_readings={"s.ev": ev},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
        )
        assert steps[0]["kw"] == pytest.approx(0.8)

    def test_subtract_only_timestamp_is_ignored(self) -> None:
        # The sum entity has hour 14 on day 0 only. The subtract entity has
        # hour 14 on both days. Day 1 has nothing to subtract from, so it must
        # not enter the average as a negative hour. Expected: 1.0 - 0.3 = 0.7.
        load = self._readings_for_days(_NOW, 2, [0], 14, 1.0)
        ev = self._readings_for_days(_NOW, 2, [0, 1], 14, 0.3)
        steps = build_forecast(
            sum_readings={"s.load": load},
            subtract_readings={"s.ev": ev},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
        )
        assert steps[0]["kw"] == pytest.approx(0.7)

    def test_negative_hours_are_averaged_before_clamping(self) -> None:
        # day 0: 1.0 - 0.0 = 1.0; day 1: 0.0 - 0.6 = -0.6 (sum entity reads
        # zero, subtract reads 0.6). Mean = 0.2. Clamping each timestamp first
        # would give (1.0 + 0.0) / 2 = 0.5 and bias the forecast upward.
        load = [
            *self._readings_for_days(_NOW, 2, [0], 14, 1.0),
            *self._readings_for_days(_NOW, 2, [1], 14, 0.0),
        ]
        ev = self._readings_for_days(_NOW, 2, [1], 14, 0.6)
        steps = build_forecast(
            sum_readings={"s.load": load},
            subtract_readings={"s.ev": ev},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
        )
        assert steps[0]["kw"] == pytest.approx(0.2)

    def test_decay_weights_apply_to_merged_series(self) -> None:
        # Handover across two days with decay 4.0: old entity day 0 = 0.1,
        # new entity day 1 = 0.2. Weighted mean = (0.1*1 + 0.2*4) / 5 = 0.18.
        old = self._readings_for_days(_NOW, 2, [0], 14, 0.1)
        new = self._readings_for_days(_NOW, 2, [1], 14, 0.2)
        steps = build_forecast(
            sum_readings={"s.old": old, "s.new": new},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=2,
            lookback_decay=4.0,
        )
        assert steps[0]["kw"] == pytest.approx(0.18)

    def test_merge_keys_on_instant_not_string(self) -> None:
        # The same instant written two ways ("+00:00" offset and "Z" suffix)
        # must merge into one timestamp. Keying on the raw string would keep
        # them apart and average 1.0 instead of summing to 2.0.
        ts = (_NOW - timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        a = [{"start": ts.isoformat(), "mean": 1.0}]
        b = [{"start": ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "mean": 1.0}]
        assert a[0]["start"] != b[0]["start"]
        steps = build_forecast(
            sum_readings={"s.a": a, "s.b": b},
            subtract_readings={},
            now=_NOW,
            horizon_hours=1,
            lookback_days=1,
        )
        assert steps[0]["kw"] == pytest.approx(2.0)
