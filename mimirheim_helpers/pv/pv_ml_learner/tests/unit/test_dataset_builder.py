"""Tests for pv_ml_learner.dataset_builder."""

from __future__ import annotations

import calendar
import pytest
from pv_ml_learner.storage import KnmiRow, PvActualRow


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


def _knmi(hour_utc: int, ghi: float, wind: float | None = 3.0,
          temp: float | None = 12.0, rain: float = 0.0) -> KnmiRow:
    return KnmiRow(hour_utc=hour_utc, station_id=260,
                   ghi_wm2=ghi, wind_ms=wind, temp_c=temp, rain_mm=rain)


def _pv(hour_utc: int, kwh: float) -> PvActualRow:
    return PvActualRow(array_name="main", hour_utc=hour_utc, kwh=kwh)


class TestBuildTrainingRows:
    def test_night_hours_excluded(self) -> None:
        """Hours where GHI <= 5 W/m² are excluded."""
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [
            _knmi(base, ghi=0.0),       # night
            _knmi(base + 3600, ghi=5.0),  # threshold — excluded (≤ 5)
            _knmi(base + 7200, ghi=100.0),  # day
        ]
        pv_rows = [
            _pv(base, 0.0),
            _pv(base + 3600, 0.0),
            _pv(base + 7200, 1.5),
        ]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert len(rows) == 1
        assert rows[0].ghi_wm2 == pytest.approx(100.0)

    def test_negative_actual_excluded(self) -> None:
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [_knmi(base, ghi=100.0)]
        pv_rows = [_pv(base, -0.1)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert rows == []

    def test_missing_knmi_temp_produces_none_not_zero(self) -> None:
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [_knmi(base, ghi=200.0, temp=None)]
        pv_rows = [_pv(base, 1.0)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert len(rows) == 1
        assert rows[0].temp_c is None

    def test_rain_mm_zero_for_trace_precipitation(self) -> None:
        """rain_mm comes from KnmiRow.rain_mm which is already 0.0 for traces."""
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [_knmi(base, ghi=200.0, rain=0.0)]
        pv_rows = [_pv(base, 1.0)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert rows[0].rain_mm == pytest.approx(0.0)

    def test_correct_temporal_features(self) -> None:
        from pv_ml_learner.dataset_builder import build_training_rows

        # 2024-07-15 14:00 UTC → month=7, hour=14, week≈29, quarter=3
        ts = _ts(2024, 7, 15, 14)
        knmi_rows = [_knmi(ts, ghi=300.0)]
        pv_rows = [_pv(ts, 2.0)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert rows[0].hour_of_day == 14
        assert rows[0].month == 7
        assert rows[0].quarter == 3

    def test_hour_with_no_knmi_is_excluded(self) -> None:
        """Hours present in PV actuals but absent in KNMI produce no training row."""
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [_knmi(base, ghi=200.0)]
        pv_rows = [_pv(base, 1.0), _pv(base + 3600, 1.5)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert len(rows) == 1

    def test_limiting_sensor_active_hour_excluded(self) -> None:
        """Hours absent from pv_rows (because ha_actuals excluded them via
        limiting sensor) produce no training row — inner join behaviour."""
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [
            _knmi(base, ghi=200.0),
            _knmi(base + 3600, ghi=180.0),  # limiting sensor active for this hour
        ]
        # Only the first hour has a PV actual; second was dropped by ha_actuals
        pv_rows = [_pv(base, 1.0)]
        rows = build_training_rows(knmi_rows, pv_rows)
        assert len(rows) == 1
        assert rows[0].hour_utc == base

    def test_hour_included_when_limiting_sensor_absent(self) -> None:
        """When a limiting sensor has no recorded value for an hour, ha_actuals
        includes that hour normally; dataset builder must include it too."""
        from pv_ml_learner.dataset_builder import build_training_rows

        base = _ts(2024, 6, 1, 10)
        knmi_rows = [_knmi(base, ghi=200.0)]
        pv_rows = [_pv(base, 1.0)]  # included because limiting sensor had no reading
        rows = build_training_rows(knmi_rows, pv_rows)
        assert len(rows) == 1

    def test_month_distribution_correct(self) -> None:
        """Rows built from data spanning Jan–Jun have month attributes 1–6."""
        from pv_ml_learner.dataset_builder import build_training_rows

        rows = []
        for month in range(1, 7):
            ts = _ts(2024, month, 15, 10)
            rows.extend(
                build_training_rows([_knmi(ts, ghi=100.0)], [_pv(ts, 1.0)])
            )

        assert len(rows) == 6
        assert {r.month for r in rows} == {1, 2, 3, 4, 5, 6}
