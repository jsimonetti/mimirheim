"""Tests for pv_ml_learner.predictor."""

from __future__ import annotations

import calendar
import datetime
import json
import time

import joblib
import pytest

from pv_ml_learner.config import HyperparamConfig, TrainingConfig
from pv_ml_learner.dataset_builder import TrainingRow
from pv_ml_learner.storage import McRow


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


def _make_training_row(hour_utc: int, kwh: float = 1.5) -> TrainingRow:
    t = time.gmtime(hour_utc)
    d = datetime.date(t.tm_year, t.tm_mon, t.tm_mday)
    return TrainingRow(
        hour_utc=hour_utc,
        ghi_wm2=200.0,
        wind_ms=3.0,
        temp_c=12.0,
        rain_mm=0.5,
        hour_of_day=t.tm_hour,
        month=t.tm_mon,
        week_nr=d.isocalendar()[1],
        quarter=(t.tm_mon - 1) // 3 + 1,
        kwh_actual=kwh,
    )


def _make_mc_row(step_ts: int, ghi: float = 200.0) -> McRow:
    return McRow(
        step_ts=step_ts,
        ghi_wm2=ghi,
        temp_c=12.0,
        wind_ms=3.0,
        rain_mm=0.5,
        cloud_pct=40.0,
    )


def _fast_grid() -> HyperparamConfig:
    return HyperparamConfig(
        n_estimators=[10],
        max_depth=[3],
        learning_rate=[0.1],
        subsample=[1.0],
        min_child_weight=[1],
    )


def _make_rows(n_months: int = 3) -> list[TrainingRow]:
    rows = []
    for month in range(1, n_months + 1):
        for h in range(20):
            day = (h // 10) + 1
            hour = h % 10 + 8
            ts = _ts(2024, month, day, hour)
            rows.append(_make_training_row(ts, kwh=0.5 + h * 0.05))
    return rows


@pytest.fixture()
def trained_model(tmp_path):
    """Train a small model and return (model_path, metadata_path) pointing to it."""
    from pv_ml_learner.trainer import train_model

    model_path = str(tmp_path / "model.joblib")
    metadata_path = str(tmp_path / "metadata.json")
    tc = TrainingConfig(
        min_months_required=3, n_cv_splits=2, hyperparams=_fast_grid(),
        train_trigger_topic="t/train", inference_trigger_topic="t/infer",
    )
    train_model(_make_rows(n_months=3), tc, model_path, metadata_path)
    return model_path, metadata_path


class TestModelNotReady:
    def test_no_model_raises(self, tmp_path) -> None:
        from pv_ml_learner.predictor import ModelNotReadyError, predict_forecast

        model_path = str(tmp_path / "model.joblib")
        metadata_path = str(tmp_path / "metadata.json")
        base_ts = _ts(2024, 6, 15, 6)
        mc_rows = [_make_mc_row(base_ts + i * 3600, ghi=200.0) for i in range(5)]

        with pytest.raises(ModelNotReadyError):
            predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)


class TestPredictions:
    def test_night_step_kw_is_zero(self, trained_model) -> None:
        """Steps with GHI ≤ 5 W/m² produce kw = 0.0."""
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 0)
        mc_rows = [
            _make_mc_row(base_ts, ghi=0.0),      # night
            _make_mc_row(base_ts + 3600, ghi=3.0),  # night — below threshold
            _make_mc_row(base_ts + 7200, ghi=200.0),  # day
        ]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)
        assert len(steps) == 3
        assert steps[0].kw == pytest.approx(0.0)
        assert steps[1].kw == pytest.approx(0.0)
        assert steps[2].kw >= 0.0

    def test_predictions_clamped_above_zero(self, trained_model) -> None:
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 10)
        mc_rows = [_make_mc_row(base_ts + i * 3600, ghi=200.0) for i in range(5)]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)
        assert all(s.kw >= 0.0 for s in steps)

    def test_predictions_clamped_at_peak_power(self, trained_model) -> None:
        """kw never exceeds peak_power_kwp * 1.1."""
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 10)
        peak = 5.0
        mc_rows = [_make_mc_row(base_ts + i * 3600, ghi=200.0) for i in range(5)]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=peak)
        assert all(s.kw <= peak * 1.1 + 1e-9 for s in steps)

    def test_output_count_matches_input(self, trained_model) -> None:
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 6)
        mc_rows = [_make_mc_row(base_ts + i * 3600, ghi=200.0) for i in range(12)]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)
        assert len(steps) == 12

    def test_confidence_within_bounds(self, trained_model) -> None:
        """All confidence values are within [0.30, 0.95]."""
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 6)
        mc_rows = [_make_mc_row(base_ts + i * 3600, ghi=200.0) for i in range(48)]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)
        for step in steps:
            assert 0.30 <= step.confidence <= 0.95, (
                f"confidence {step.confidence} out of [0.30, 0.95]"
            )

    def test_step_ts_is_utc_aware(self, trained_model) -> None:
        from pv_ml_learner.predictor import predict_forecast

        model_path, metadata_path = trained_model
        base_ts = _ts(2024, 6, 15, 10)
        mc_rows = [_make_mc_row(base_ts)]
        steps = predict_forecast(mc_rows, model_path, metadata_path, peak_power_kwp=10.0)
        assert steps[0].ts.tzinfo == datetime.timezone.utc
