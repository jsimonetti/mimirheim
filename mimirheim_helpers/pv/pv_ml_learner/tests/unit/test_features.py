"""Tests for pv_ml_learner.features."""

from __future__ import annotations

import calendar
import datetime

import pytest

from pv_ml_learner.dataset_builder import TrainingRow
from pv_ml_learner.storage import McRow


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


def _row(
    hour_utc: int,
    ghi: float = 200.0,
    wind: float | None = 3.0,
    temp: float | None = 12.0,
    rain: float = 0.5,
    kwh: float = 1.5,
) -> TrainingRow:
    import time

    t = time.gmtime(hour_utc)
    import datetime as _dt

    week_nr = _dt.date(t.tm_year, t.tm_mon, t.tm_mday).isocalendar()[1]
    return TrainingRow(
        hour_utc=hour_utc,
        ghi_wm2=ghi,
        wind_ms=wind,
        temp_c=temp,
        rain_mm=rain,
        hour_of_day=t.tm_hour,
        month=t.tm_mon,
        week_nr=week_nr,
        quarter=(t.tm_mon - 1) // 3 + 1,
        kwh_actual=kwh,
    )


def _mc_row(step_ts: int, ghi: float = 200.0, temp: float = 12.0,
            wind: float = 3.0, rain: float = 0.5, cloud: float = 60.0) -> McRow:
    return McRow(
        step_ts=step_ts,
        ghi_wm2=ghi,
        temp_c=temp,
        wind_ms=wind,
        rain_mm=rain,
        cloud_pct=cloud,
    )


class TestBuildTrainingMatrix:
    def test_expected_columns_with_all_features(self) -> None:
        """When wind and temp are present, all expected columns appear."""
        from pv_ml_learner.features import build_training_matrix

        ts = _ts(2024, 6, 15, 10)
        rows = [_row(ts)]
        X, y = build_training_matrix(rows)

        assert set(X.columns) == {
            "ghi_wm2", "wind_ms", "temp_c", "rain_mm",
            "hour", "month", "week_nr", "quarter",
        }
        assert y.iloc[0] == pytest.approx(1.5)

    def test_wind_absent_when_all_none(self) -> None:
        """When all training rows have wind_ms=None, the column is excluded."""
        from pv_ml_learner.features import build_training_matrix

        ts = _ts(2024, 6, 15, 10)
        rows = [_row(ts, wind=None)]
        X, _y = build_training_matrix(rows)
        assert "wind_ms" not in X.columns

    def test_temp_absent_when_all_none(self) -> None:
        """When all training rows have temp_c=None, the column is excluded."""
        from pv_ml_learner.features import build_training_matrix

        ts = _ts(2024, 6, 15, 10)
        rows = [_row(ts, temp=None)]
        X, _y = build_training_matrix(rows)
        assert "temp_c" not in X.columns

    def test_rain_mm_always_present(self) -> None:
        """rain_mm is always included because KNMI trace is already 0.0."""
        from pv_ml_learner.features import build_training_matrix

        ts = _ts(2024, 6, 15, 10)
        rows = [_row(ts)]
        X, _y = build_training_matrix(rows)
        assert "rain_mm" in X.columns

    def test_quarter_values(self) -> None:
        """quarter is 1 for January, 2 for April, 3 for July, 4 for October."""
        from pv_ml_learner.features import build_training_matrix

        months_expected = [(1, 1), (4, 2), (7, 3), (10, 4)]
        for month, expected_quarter in months_expected:
            ts = _ts(2024, month, 15, 10)
            rows = [_row(ts)]
            X, _y = build_training_matrix(rows)
            assert X["quarter"].iloc[0] == expected_quarter, (
                f"month {month} → quarter should be {expected_quarter}"
            )

    def test_week_nr_year_boundary(self) -> None:
        """ISO week numbers around year-end are handled correctly."""
        from pv_ml_learner.features import build_training_matrix

        # 2024-12-30 is ISO week 1 of 2025
        ts = _ts(2024, 12, 30, 10)
        rows = [_row(ts)]
        X, _y = build_training_matrix(rows)
        assert X["week_nr"].iloc[0] == 1

    def test_multiple_rows_shape(self) -> None:
        from pv_ml_learner.features import build_training_matrix

        rows = [_row(_ts(2024, 6, 15, h)) for h in range(6, 20)]
        X, y = build_training_matrix(rows)
        assert X.shape[0] == 14
        assert y.shape[0] == 14


class TestBuildInferenceRow:
    def test_columns_match_training_with_all_features(self) -> None:
        """Inference row has the same columns as training matrix."""
        from pv_ml_learner.features import build_training_matrix, build_inference_row

        ts = _ts(2024, 6, 15, 10)
        train_rows = [_row(ts)]
        X_train, _y = build_training_matrix(train_rows)
        feature_list = list(X_train.columns)

        step_ts = datetime.datetime(2024, 6, 16, 10, tzinfo=datetime.timezone.utc)
        X_inf = build_inference_row(step_ts, _mc_row(int(step_ts.timestamp())),
                                    feature_list)

        assert list(X_inf.columns) == feature_list

    def test_columns_match_when_wind_absent(self) -> None:
        """When training excluded wind_ms, inference row also omits it."""
        from pv_ml_learner.features import build_training_matrix, build_inference_row

        ts = _ts(2024, 6, 15, 10)
        train_rows = [_row(ts, wind=None)]
        X_train, _y = build_training_matrix(train_rows)
        feature_list = list(X_train.columns)

        step_ts = datetime.datetime(2024, 6, 16, 10, tzinfo=datetime.timezone.utc)
        X_inf = build_inference_row(step_ts, _mc_row(int(step_ts.timestamp())),
                                    feature_list)

        assert "wind_ms" not in X_inf.columns
        assert list(X_inf.columns) == feature_list

    def test_quarter_computed_correctly_in_inference(self) -> None:
        from pv_ml_learner.features import build_training_matrix, build_inference_row

        ts = _ts(2024, 7, 15, 10)
        train_rows = [_row(ts)]
        X_train, _y = build_training_matrix(train_rows)
        feature_list = list(X_train.columns)

        step_ts = datetime.datetime(2024, 10, 15, 10, tzinfo=datetime.timezone.utc)
        X_inf = build_inference_row(step_ts, _mc_row(int(step_ts.timestamp())),
                                    feature_list)

        assert X_inf["quarter"].iloc[0] == 4
