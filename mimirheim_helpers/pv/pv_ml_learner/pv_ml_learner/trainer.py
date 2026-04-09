"""XGBoost model trainer for PV production forecasting.

This module accepts a list of ``TrainingRow`` instances and trains an XGBoost
regression model using ``GridSearchCV`` with ``TimeSeriesSplit`` cross-validation.
The trained model and associated metadata are written to disk via ``joblib``.

The module does not read from or write to the SQLite database, perform network
I/O, or publish MQTT messages.  The caller is responsible for loading training
data and passing correct configuration objects.
"""

from __future__ import annotations

import datetime
import json
import logging
from pathlib import Path

import joblib
from sklearn.model_selection import GridSearchCV, TimeSeriesSplit
from xgboost import XGBRegressor

from pv_ml_learner.config import TrainingConfig
from pv_ml_learner.dataset_builder import TrainingRow
from pv_ml_learner.features import build_training_matrix

logger = logging.getLogger(__name__)


class InsufficientDataError(Exception):
    """Raised when the training dataset does not span enough calendar months.

    A model trained on fewer months than ``config.training.min_months_required``
    would lack seasonal variation and would produce unreliable forecasts for
    months it has not seen.  Rather than train a biased model, the trainer
    raises this exception and the caller skips the publish step.
    """


def _count_distinct_months(rows: list[TrainingRow]) -> int:
    """Return the number of distinct calendar months (1–12) present in ``rows``."""
    return len({r.month for r in rows})


def train_model(
    rows: list[TrainingRow],
    training_config: TrainingConfig,
    model_path: str,
    metadata_path: str,
) -> None:
    """Train an XGBoost regression model and persist the result to disk.

    Performs the following steps:

    1. Count distinct calendar months in ``rows``. Raise ``InsufficientDataError``
       if fewer than ``training_config.min_months_required`` are present.
    2. Build the feature matrix and target series using ``build_training_matrix``.
    3. Run ``GridSearchCV`` over the hyperparameter grid defined in
       ``training_config.hyperparams`` with ``TimeSeriesSplit`` cross-validation.
    4. Refit the best estimator on the entire training set.
    5. Write the model to ``model_path`` via ``joblib.dump``.
    6. Write a JSON metadata file to ``metadata_path`` containing
       training timestamp, validation MAE, distinct month count, and feature list.

    Args:
        rows: Filtered training rows from ``dataset_builder.build_training_rows``.
        training_config: Scheduling and hyperparameter configuration.
        model_path: File path where the serialised model is written.
        metadata_path: File path where the JSON metadata is written.

    Raises:
        InsufficientDataError: If the dataset covers fewer calendar months than
            ``training_config.min_months_required``.
    """
    distinct_months = _count_distinct_months(rows)
    if distinct_months < training_config.min_months_required:
        missing = training_config.min_months_required - distinct_months
        present = sorted({r.month for r in rows})
        raise InsufficientDataError(
            f"Training requires {training_config.min_months_required} distinct calendar months "
            f"but only {distinct_months} are present (months {present}). "
            f"Need {missing} more month(s) of PV actuals before training can proceed."
        )

    X, y = build_training_matrix(rows)
    feature_list = list(X.columns)

    hp = training_config.hyperparams
    param_grid = {
        "n_estimators": hp.n_estimators,
        "max_depth": hp.max_depth,
        "learning_rate": hp.learning_rate,
        "subsample": hp.subsample,
        "min_child_weight": hp.min_child_weight,
    }

    base_estimator = XGBRegressor(
        objective="reg:squarederror",
        tree_method="hist",
        random_state=42,
        verbosity=0,
    )

    cv = TimeSeriesSplit(n_splits=training_config.n_cv_splits)
    search = GridSearchCV(
        estimator=base_estimator,
        param_grid=param_grid,
        scoring="neg_mean_absolute_error",
        cv=cv,
        refit=True,
        n_jobs=1,
    )

    logger.info(
        "Starting grid search over %d parameter combinations with %d CV splits "
        "on %d training rows spanning %d months.",
        len(list(_iter_combinations(param_grid))),
        training_config.n_cv_splits,
        len(rows),
        distinct_months,
    )

    search.fit(X, y)

    best_mae = -float(search.best_score_)
    logger.info(
        "Best parameters: %s | CV MAE: %.4f kWh", search.best_params_, best_mae
    )

    model_path_obj = Path(model_path)
    model_path_obj.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(search.best_estimator_, model_path_obj)
    logger.info("Model written to %s.", model_path_obj)

    # All rows in the training set are daylight hours (night excluded by
    # dataset_builder), so the mean is the mean daylight production.  This is
    # stored so the predictor can compute relative_error without needing the
    # original training rows.
    mean_daylight_kwh = sum(r.kwh_actual for r in rows) / len(rows)

    metadata = {
        "trained_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "validation_mae_kwh": best_mae,
        "mean_actual_kwh_daylight": mean_daylight_kwh,
        "distinct_months": distinct_months,
        "feature_list": feature_list,
        "best_params": search.best_params_,
        "n_training_rows": len(rows),
    }
    metadata_path_obj = Path(metadata_path)
    metadata_path_obj.parent.mkdir(parents=True, exist_ok=True)
    metadata_path_obj.write_text(json.dumps(metadata, indent=2))
    logger.info("Metadata written to %s.", metadata_path_obj)


def _iter_combinations(param_grid: dict[str, list]) -> object:
    """Yield one item per parameter combination (for logging purposes only)."""
    from itertools import product

    keys = list(param_grid.keys())
    values = [param_grid[k] for k in keys]
    for combo in product(*values):
        yield dict(zip(keys, combo))
