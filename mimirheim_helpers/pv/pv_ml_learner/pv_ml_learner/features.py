"""Feature engineering for PV production forecasting.

This module converts ``TrainingRow`` dataclasses into a pandas DataFrame/Series
pair ready for XGBoost training, and converts a single Meteoserver forecast step
into an equivalent inference row.

The feature set is identical at training and inference time.  Optional columns
(``wind_ms``, ``temp_c``) are excluded from the feature matrix when they are
absent from all training rows, and the same feature list must be passed to
``build_inference_row`` at inference time.  Mismatches between the saved feature
list and the columns available in the Meteoserver row raise ``ValueError``.

This module does not read from or write to disk, perform network I/O, or
interact with the database.
"""

from __future__ import annotations

import datetime
import logging

import pandas as pd

from pv_ml_learner.dataset_builder import TrainingRow
from pv_ml_learner.storage import McRow

logger = logging.getLogger(__name__)

# Columns that are always present in both training and inference.
_REQUIRED_COLS: list[str] = ["ghi_wm2", "rain_mm", "hour", "month", "week_nr", "quarter"]

# Optional columns: included only when they are not uniformly None in training data.
_OPTIONAL_COLS: list[str] = ["wind_ms", "temp_c"]


def build_training_matrix(
    rows: list[TrainingRow],
) -> tuple[pd.DataFrame, pd.Series]:
    """Convert a list of training rows into a feature matrix and target series.

    Optional columns (``wind_ms``, ``temp_c``) are excluded when they are
    ``None`` for every row in the list.  This prevents the model receiving a
    column of zeros that would corrupt the learned weights.

    Args:
        rows: Pre-filtered training rows from ``dataset_builder.build_training_rows``.

    Returns:
        A tuple ``(X, y)`` where ``X`` is a ``pd.DataFrame`` of features and
        ``y`` is a ``pd.Series`` of ``kwh_actual`` values aligned by index.
    """
    include_wind = any(r.wind_ms is not None for r in rows)
    include_temp = any(r.temp_c is not None for r in rows)

    records = []
    for row in rows:
        rec: dict[str, object] = {
            "ghi_wm2": row.ghi_wm2,
            "rain_mm": row.rain_mm,
            "hour": row.hour_of_day,
            "month": row.month,
            "week_nr": row.week_nr,
            "quarter": row.quarter,
        }
        if include_wind:
            # When some rows have wind and others do not (e.g. sensor outage),
            # fill the missing ones with NaN.  XGBoost handles NaN natively.
            rec["wind_ms"] = row.wind_ms
        if include_temp:
            rec["temp_c"] = row.temp_c
        records.append(rec)

    X = pd.DataFrame(records)
    y = pd.Series([r.kwh_actual for r in rows], name="kwh_actual")

    # Order columns deterministically: required first, then optional.
    col_order = _REQUIRED_COLS.copy()
    if include_wind:
        col_order.append("wind_ms")
    if include_temp:
        col_order.append("temp_c")
    X = X[col_order]

    return X, y


def build_inference_row(
    step_ts: datetime.datetime,
    mc_row: McRow,
    feature_list: list[str],
) -> pd.DataFrame:
    """Build a single-row feature DataFrame for one Meteoserver forecast step.

    The returned DataFrame has exactly the columns in ``feature_list``, which
    must match the columns produced by ``build_training_matrix`` for the current
    model.  The feature list is stored in the model metadata at training time
    and loaded at inference time.

    Args:
        step_ts: UTC-aware datetime of the forecast step.
        mc_row: Meteoserver forecast data for the step.
        feature_list: Ordered list of column names produced during training.
            Determines which optional columns are included.

    Returns:
        A one-row ``pd.DataFrame`` with columns equal to ``feature_list``.

    Raises:
        ValueError: If a column in ``feature_list`` cannot be derived from
            ``mc_row`` or ``step_ts``.
    """
    week_nr = step_ts.isocalendar()[1]
    quarter = (step_ts.month - 1) // 3 + 1

    available: dict[str, object] = {
        "ghi_wm2": mc_row.ghi_wm2,
        "wind_ms": mc_row.wind_ms,
        "temp_c": mc_row.temp_c,
        "rain_mm": mc_row.rain_mm,
        "hour": step_ts.hour,
        "month": step_ts.month,
        "week_nr": week_nr,
        "quarter": quarter,
    }

    missing = [col for col in feature_list if col not in available]
    if missing:
        raise ValueError(
            f"Inference feature list contains columns that cannot be derived "
            f"from the Meteoserver row: {missing}."
        )

    record = {col: available[col] for col in feature_list}
    return pd.DataFrame([record], columns=feature_list)
