"""Tests for pv_ml_learner.trainer."""

from __future__ import annotations

import calendar
import datetime
import json
import time

import pytest

from pv_ml_learner.config import HyperparamConfig, TrainingConfig
from pv_ml_learner.dataset_builder import TrainingRow


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


def _make_row(hour_utc: int, ghi: float = 200.0, kwh: float = 1.5) -> TrainingRow:
    t = time.gmtime(hour_utc)
    d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
    return TrainingRow(
        hour_utc=hour_utc,
        ghi_wm2=ghi,
        wind_ms=3.0,
        temp_c=12.0,
        rain_mm=0.5,
        hour_of_day=t.tm_hour,
        month=t.tm_mon,
        week_nr=d.isocalendar()[1],
        quarter=(t.tm_mon - 1) // 3 + 1,
        kwh_actual=kwh,
    )


def _make_rows(n_months: int = 3, hours_per_month: int = 20) -> list[TrainingRow]:
    """Generate synthetic training rows spanning ``n_months`` calendar months."""
    rows = []
    for month in range(1, n_months + 1):
        for h in range(hours_per_month):
            day = (h // 10) + 1
            hour = h % 10 + 8  # daytime hours 8–17
            ts = _ts(2024, month, day, hour)
            rows.append(_make_row(ts, ghi=100.0 + h * 5, kwh=0.5 + h * 0.05))
    return rows


def _fast_grid() -> HyperparamConfig:
    """Minimal hyperparameter grid that keeps tests fast (single combination)."""
    return HyperparamConfig(
        n_estimators=[10],
        max_depth=[3],
        learning_rate=[0.1],
        subsample=[1.0],
        min_child_weight=[1],
    )


def _paths(tmp_path) -> tuple[str, str]:
    """Return (model_path, metadata_path) as strings under tmp_path."""
    return str(tmp_path / "model.joblib"), str(tmp_path / "metadata.json")


class TestInsufficientData:
    def test_insufficient_months_raises(self, tmp_path: object) -> None:
        """InsufficientDataError is raised when distinct months < min_months_required."""
        from pv_ml_learner.trainer import InsufficientDataError, train_model

        rows = _make_rows(n_months=2)
        tc = TrainingConfig(
            min_months_required=6, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        model_path, metadata_path = _paths(tmp_path)
        with pytest.raises(InsufficientDataError):
            train_model(rows, tc, model_path, metadata_path)

    def test_threshold_met_does_not_raise(self, tmp_path: object) -> None:
        from pv_ml_learner.trainer import train_model

        rows = _make_rows(n_months=3)
        tc = TrainingConfig(
            min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        model_path, metadata_path = _paths(tmp_path)
        # Should not raise
        train_model(rows, tc, model_path, metadata_path)


class TestSuccessfulTraining:
    def test_model_file_written(self, tmp_path: object) -> None:
        from pathlib import Path

        from pv_ml_learner.trainer import train_model

        model_path, metadata_path = _paths(tmp_path)
        rows = _make_rows(n_months=3)
        tc = TrainingConfig(
            min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        train_model(rows, tc, model_path, metadata_path)
        assert Path(model_path).exists()

    def test_metadata_has_required_keys(self, tmp_path: object) -> None:
        from pathlib import Path

        from pv_ml_learner.trainer import train_model

        model_path, metadata_path = _paths(tmp_path)
        rows = _make_rows(n_months=3)
        tc = TrainingConfig(
            min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        train_model(rows, tc, model_path, metadata_path)

        meta = json.loads(Path(metadata_path).read_text())
        assert "trained_at_utc" in meta
        assert "validation_mae_kwh" in meta
        assert "distinct_months" in meta
        assert meta["distinct_months"] == 3

    def test_second_training_run_overwrites_model(self, tmp_path: object) -> None:
        from pathlib import Path

        from pv_ml_learner.trainer import train_model

        model_path, metadata_path = _paths(tmp_path)
        tc = TrainingConfig(
            min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        rows = _make_rows(n_months=3)
        train_model(rows, tc, model_path, metadata_path)
        mtime_first = Path(model_path).stat().st_mtime

        import time as _time
        _time.sleep(0.05)  # ensure mtime differs

        train_model(rows, tc, model_path, metadata_path)
        mtime_second = Path(model_path).stat().st_mtime

        assert mtime_second > mtime_first

    def test_feature_list_in_metadata_matches_training_matrix(
        self, tmp_path: object
    ) -> None:
        """The feature list saved in metadata matches build_training_matrix columns."""
        import json
        from pathlib import Path

        from pv_ml_learner.features import build_training_matrix
        from pv_ml_learner.trainer import train_model

        model_path, metadata_path = _paths(tmp_path)
        rows = _make_rows(n_months=3)
        tc = TrainingConfig(
            min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
            train_trigger_topic="t/train", inference_trigger_topic="t/infer",
        )
        train_model(rows, tc, model_path, metadata_path)

        meta = json.loads(Path(metadata_path).read_text())
        X, _y = build_training_matrix(rows)
        assert meta["feature_list"] == list(X.columns)
