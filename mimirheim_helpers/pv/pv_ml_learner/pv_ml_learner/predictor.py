"""Inference engine: loads trained model and produces hourly PV power forecasts.

This module provides ``predict_forecast``, which loads the serialised XGBoost
model and metadata from disk, builds inference feature rows from a list of
Meteoserver forecast steps, predicts AC output in kW, applies output clamping,
and attaches calibrated confidence values.

It does not write to disk, perform network I/O, or interact with MQTT.  The
caller supplies already-fetched ``McRow`` instances and configuration paths.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass
from pathlib import Path

import joblib
import pandas as pd

# predictor has no config imports needed beyond standard library
from pv_ml_learner.features import build_inference_row
from pv_ml_learner.storage import McRow

logger = logging.getLogger(__name__)

# Hours with Meteoserver GHI at or below this threshold are treated as
# night-time.  The model may not have reliable estimates at these irradiance
# levels, and a forced zero is more useful than a tiny noisy prediction.
_NIGHT_GHI_THRESHOLD_WM2 = 5.0

# Maximum confidence value.  Capped below 1.0 because no output-only model is
# perfectly calibrated; expressing 100% confidence would be misleading.
_CONFIDENCE_MAX = 0.95

# Minimum confidence value applied regardless of model error.  Provides a
# minimum usefulness guarantee so the MIMIRHEIM planner does not discard the forecast
# entirely during the warm-up period.
_CONFIDENCE_MIN = 0.30


@dataclass
class ForecastStep:
    """One hourly step in the PV production forecast.

    Attributes:
        ts: UTC-aware datetime of the start of the forecast hour.
        kw: Predicted AC output for the hour, in kilowatts.  Always >= 0.
        confidence: Calibrated confidence in [0.30, 0.95].  Decreases with
            horizon distance and with relative model error.
    """

    ts: datetime.datetime
    kw: float
    confidence: float


class ModelNotReadyError(Exception):
    """Raised when no trained model is found at the configured path.

    The daemon catches this at inference time, logs the error, and skips the
    publish step.  A training run should be triggered to resolve this condition.
    """


def _compute_confidence(
    step_index: int,
    validation_mae: float,
    mean_daylight_kwh: float,
) -> float:
    """Return the calibrated confidence value for one forecast step.

    The formula comes from Critical Concern 8 in the plan.  It applies a
    decay factor based on horizon distance (0–6 h, 6–24 h, 24–48 h) and a
    base confidence derived from the model's relative error on held-out data.

    Args:
        step_index: 0-based position of the step in the forecast sequence.
            Treated as the horizon in hours.
        validation_mae: Mean absolute error in kWh/h from cross-validation,
            stored in the model metadata file.
        mean_daylight_kwh: Mean production over daylight hours across the
            training dataset, stored in the model metadata file.

    Returns:
        A float in [0.30, 0.95].
    """
    if mean_daylight_kwh <= 0.0:
        return _CONFIDENCE_MIN

    relative_error = validation_mae / mean_daylight_kwh
    base = min(_CONFIDENCE_MAX, max(0.60, 1.0 - relative_error))

    if step_index < 6:
        factor = 1.0
    elif step_index < 24:
        factor = 0.90
    else:
        factor = 0.80

    return max(_CONFIDENCE_MIN, base * factor)


def predict_forecast(
    mc_rows: list[McRow],
    model_path: str,
    metadata_path: str,
    peak_power_kwp: float,
) -> list[ForecastStep]:
    """Produce an hourly PV output forecast from Meteoserver weather steps.

    Loads the trained model and metadata from ``model_path`` and
    ``metadata_path``, converts each ``McRow`` into the feature representation
    used during training, calls the model, and returns ``ForecastStep`` objects.

    Steps with GHI <= 5 W/m2 receive ``kw = 0.0``.  All ``kw`` values are
    clamped to [0, ``peak_power_kwp`` * 1.1].

    Args:
        mc_rows: Meteoserver forecast steps in ascending ``step_ts`` order.
        model_path: File path to the serialised joblib model.
        metadata_path: File path to the JSON metadata written alongside the model.
        peak_power_kwp: Nameplate DC capacity of the PV array in kWp.

    Returns:
        A list of ``ForecastStep`` objects, one per input row, in the same order.

    Raises:
        ModelNotReadyError: If no serialised model exists at ``model_path``.
    """
    model_path_obj = Path(model_path)
    if not model_path_obj.exists():
        raise ModelNotReadyError(
            f"No trained model found at '{model_path_obj}'. "
            "Run a training cycle before requesting a forecast."
        )

    model = joblib.load(model_path_obj)

    metadata: dict = json.loads(Path(metadata_path).read_text())
    feature_list: list[str] = metadata["feature_list"]
    validation_mae: float = float(metadata["validation_mae_kwh"])
    mean_daylight_kwh: float = float(metadata.get("mean_actual_kwh_daylight", 1.0))

    max_kw = peak_power_kwp * 1.1
    steps: list[ForecastStep] = []

    for idx, mc_row in enumerate(mc_rows):
        ts = datetime.datetime.fromtimestamp(mc_row.step_ts, tz=datetime.timezone.utc)
        confidence = _compute_confidence(idx, validation_mae, mean_daylight_kwh)

        if mc_row.ghi_wm2 <= _NIGHT_GHI_THRESHOLD_WM2:
            steps.append(ForecastStep(ts=ts, kw=0.0, confidence=confidence))
            continue

        X = build_inference_row(ts, mc_row, feature_list)
        raw_kw: float = float(model.predict(X)[0])
        kw = max(0.0, min(raw_kw, max_kw))

        steps.append(ForecastStep(ts=ts, kw=kw, confidence=confidence))

    return steps
