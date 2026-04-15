"""Configuration schema for the mimirheim pv_ml_learner daemon.

This module defines the Pydantic models that represent the pv_ml_learner YAML
configuration file. It is the single source of truth for field names, types,
constraints, and defaults.

What this module does not do:
- It does not import from mimirheim core or any other tool.
- It does not perform any I/O, HTTP, or MQTT operations.
- It does not import from other pv_ml_learner modules.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from helper_common.config import MqttConfig, apply_mqtt_env_overrides
import helper_common.topics as _topics


class KnmiConfig(BaseModel):
    """KNMI weather station configuration.

    knmi-py does not require an API key. The public KNMI script endpoint is
    used directly. History is backfilled automatically to cover the full HA
    actuals window.

    Attributes:
        station_id: KNMI station number. 260 = De Bilt. Use the nearest
            station that provides Q, FH, T, and RH columns.
    """

    model_config = ConfigDict(extra="forbid")

    station_id: int = Field(
        description="KNMI station ID. 260 = De Bilt.",
        json_schema_extra={"ui_label": "KNMI station ID", "ui_group": "basic"},
    )


class MeteoserverConfig(BaseModel):
    """Meteoserver hourly weather forecast API configuration.

    The endpoint is:
        https://data.meteoserver.nl/api/uurverwachting.php?lat=LAT&long=LON&key=KEY

    Attributes:
        api_key: Meteoserver API key (query-string authentication).
        latitude: Site latitude in decimal degrees.
        longitude: Site longitude in decimal degrees.
        forecast_horizon_hours: Number of hourly steps to store and use for
            inference. The API returns up to ~54 hours; the tail is discarded.
    """

    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(description="Meteoserver API key.", json_schema_extra={"ui_label": "API key", "ui_group": "basic"})
    latitude: float = Field(description="Site latitude in decimal degrees.", json_schema_extra={"ui_label": "Latitude", "ui_group": "basic"})
    longitude: float = Field(description="Site longitude in decimal degrees.", json_schema_extra={"ui_label": "Longitude", "ui_group": "basic"})
    forecast_horizon_hours: int = Field(
        default=48,
        ge=1,
        le=54,
        description="Number of hourly forecast steps to use (1–54).",
        json_schema_extra={"ui_label": "Forecast horizon (h)", "ui_group": "advanced"},
    )


class HomeAssistantConfig(BaseModel):
    """Home Assistant database reader configuration.

    Per-array entity IDs and limiting sensor lists are configured on each
    ArrayConfig entry, not here. This model only holds the shared database
    connection details.

    Attributes:
        db_path: Path to the Home Assistant SQLite database file.
    """

    model_config = ConfigDict(extra="forbid")

    db_path: str = Field(description="Path to the HA SQLite database file.", json_schema_extra={"ui_label": "HA DB path", "ui_group": "basic"})


class ArrayConfig(BaseModel):
    """Configuration for a single PV array.

    Each array has its own production sensors, forecast output topic, and
    model storage paths. Multiple arrays can be configured when the
    installation has more than one inverter or independently metered array.

    Attributes:
        name: Short identifier for this array. Must be unique across all
            arrays. Used in log messages and to distinguish actuals rows
            in the database.
        peak_power_kwp: Installed peak power in kWp. Predictions above
            peak_power_kwp * 1.1 are clamped to that ceiling.
        output_topic: MQTT topic on which the forecast is published (retained).
        sum_entity_ids: One or more HA energy sensor entity IDs whose hourly
            production is summed for this array.
        model_path: Path where the trained XGBoost model is serialised.
        metadata_path: Path for the JSON metadata file written alongside the
            model.
        exclude_limiting_entity_ids: Optional binary or numeric sensors that
            indicate when this array's inverter was actively limiting
            production. Matching training hours are excluded from the dataset.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        description="Unique identifier for this array.",
        json_schema_extra={"ui_label": "Array name", "ui_group": "basic"},
    )
    peak_power_kwp: float = Field(
        gt=0,
        description="Installed PV peak power in kWp.",
        json_schema_extra={"ui_label": "Peak power (kWp)", "ui_group": "basic"},
    )
    output_topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic for the retained forecast payload. "
            "Defaults to '{mimir_topic_prefix}/input/pv/{name}/forecast' when not set."
        ),
        json_schema_extra={"ui_label": "Output topic", "ui_group": "advanced", "ui_placeholder": "{mimir_topic_prefix}/input/pv/{name}/forecast"},
    )
    sum_entity_ids: list[str] = Field(
        min_length=1,
        description="Entity IDs to sum for hourly PV production.",
        json_schema_extra={"ui_label": "Sum entity IDs", "ui_group": "basic"},
    )
    model_path: str = Field(description="joblib model file path.", json_schema_extra={"ui_label": "Model path", "ui_group": "advanced"})
    metadata_path: str = Field(description="JSON metadata file path.", json_schema_extra={"ui_label": "Metadata path", "ui_group": "advanced"})
    exclude_limiting_entity_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Binary/numeric sensors indicating active inverter limiting. "
            "Matching training hours are excluded from the dataset."
        ),
        json_schema_extra={"ui_label": "Exclude limiting entities", "ui_group": "advanced"},
    )


class StorageConfig(BaseModel):
    """Path for the shared persistent SQLite database.

    Model and metadata paths are configured per-array in ArrayConfig.
    The SQLite database (KNMI observations, Meteoserver forecasts, and all
    PV actuals keyed by array name) is shared across all arrays.

    Attributes:
        db_path: Path to the SQLite database.
    """

    model_config = ConfigDict(extra="forbid")

    db_path: str = Field(description="SQLite database path.", json_schema_extra={"ui_label": "Storage DB path", "ui_group": "basic"})


class HyperparamConfig(BaseModel):
    """XGBoost hyperparameter search grid.

    Each field is a list of values to try in the TimeSeriesSplit grid search.
    Remove the field (or omit the entire ``hyperparams`` block) to use the
    default single-value grid.

    Attributes:
        n_estimators: Number of boosting rounds to try.
        max_depth: Maximum tree depth values to try.
        learning_rate: Learning rate (eta) values to try.
        subsample: Row subsampling fraction values to try.
        min_child_weight: Minimum child weight values to try.
    """

    model_config = ConfigDict(extra="forbid")

    n_estimators: list[int] = Field(default=[200], json_schema_extra={"ui_label": "n_estimators", "ui_group": "advanced"})
    max_depth: list[int] = Field(default=[5], json_schema_extra={"ui_label": "max_depth", "ui_group": "advanced"})
    learning_rate: list[float] = Field(default=[0.08], json_schema_extra={"ui_label": "learning_rate", "ui_group": "advanced"})
    subsample: list[float] = Field(default=[0.9], json_schema_extra={"ui_label": "subsample", "ui_group": "advanced"})
    min_child_weight: list[int] = Field(default=[1], json_schema_extra={"ui_label": "min_child_weight", "ui_group": "advanced"})


class TrainingConfig(BaseModel):
    """Model training and inference trigger configuration.

    Training and inference are triggered by publishing any message to the
    respective MQTT topics. Scheduling is the responsibility of the caller
    (Home Assistant automation, external cron, Node-RED, etc.).

    Attributes:
        train_trigger_topic: MQTT topic that triggers a full training cycle:
            ingest new KNMI and HA data, retrain all arrays, then immediately
            run an inference cycle.
        inference_trigger_topic: MQTT topic that triggers an inference-only
            cycle: fetch a fresh Meteoserver forecast, run all arrays, publish.
        min_months_required: Minimum distinct calendar months required to
            train. Default 12. Lower temporarily while data accumulates.
        hyperparams: XGBoost grid search configuration.
        n_cv_splits: Number of TimeSeriesSplit CV folds.
    """

    model_config = ConfigDict(extra="forbid")

    train_trigger_topic: str = Field(
        description="MQTT topic that triggers a training run.",
        json_schema_extra={"ui_label": "Train trigger topic", "ui_group": "basic"},
    )
    inference_trigger_topic: str = Field(
        description="MQTT topic that triggers an inference run.",
        json_schema_extra={"ui_label": "Inference trigger topic", "ui_group": "basic"},
    )
    min_months_required: int = Field(
        default=12,
        ge=1,
        description="Minimum distinct calendar months required to train.",
        json_schema_extra={"ui_label": "Min months required", "ui_group": "advanced"},
    )
    hyperparams: HyperparamConfig = Field(
        default_factory=HyperparamConfig,
        description="XGBoost grid search configuration.",
        json_schema_extra={"ui_label": "Hyperparameters", "ui_group": "advanced"},
    )
    n_cv_splits: int = Field(
        default=5,
        ge=2,
        description="Number of TimeSeriesSplit CV folds.",
        json_schema_extra={"ui_label": "CV splits", "ui_group": "advanced"},
    )


class HaDiscoveryConfig(BaseModel):
    """Home Assistant MQTT discovery configuration.

    Attributes:
        enabled: Whether to publish HA MQTT discovery messages. Default False.
        discovery_prefix: MQTT discovery prefix. Default 'homeassistant'.
        device_name: Device name shown in HA. Default 'MIMIRHEIM PV Learner'.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, json_schema_extra={"ui_label": "Enable HA discovery", "ui_group": "advanced"})
    discovery_prefix: str = Field(default="homeassistant", json_schema_extra={"ui_label": "Discovery prefix", "ui_group": "advanced"})
    device_name: str = Field(default="MIMIRHEIM PV Learner", json_schema_extra={"ui_label": "Device name", "ui_group": "advanced"})


class PvLearnerConfig(BaseModel):
    """Top-level configuration for the pv_ml_learner daemon.

    Attributes:
        mqtt: MQTT broker connection parameters.
        signal_mimir: If True, publish an empty message to mimir_trigger_topic
            after each complete forecast publish cycle (all arrays done).
        mimir_trigger_topic: mimirheim trigger topic. Required when signal_mimir is
            True.
        knmi: KNMI station configuration (shared across all arrays).
        meteoserver: Meteoserver API configuration.
        homeassistant: Home Assistant database configuration.
        arrays: One or more PV array configurations. Each array is trained,
            predicted, and published independently.
        storage: Path for the shared SQLite database.
        training: Training and inference trigger topic configuration.
        ha_discovery: HA MQTT discovery configuration.
        stats_topic: Optional MQTT topic where per-cycle run statistics are
            published after every training or inference cycle.
    """

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="MQTT broker connection settings.", json_schema_extra={"ui_label": "MQTT", "ui_group": "basic"})
    mimir_topic_prefix: str = Field(
        default="mimir",
        description="mimirheim mqtt.topic_prefix. Used to derive default array output and trigger topics.",
        json_schema_extra={"ui_label": "mimirheim topic prefix", "ui_group": "advanced"},
    )
    signal_mimir: bool = Field(
        default=False,
        description="Publish to mimir_trigger_topic after each forecast cycle.",
        json_schema_extra={"ui_label": "Signal mimirheim", "ui_group": "advanced"},
    )
    mimir_trigger_topic: str | None = Field(
        default=None,
        description="mimirheim trigger topic. Defaults to '{mimir_topic_prefix}/input/trigger'.",
        json_schema_extra={"ui_label": "mimirheim trigger topic", "ui_group": "advanced", "ui_placeholder": "{mimir_topic_prefix}/input/trigger"},
    )
    knmi: KnmiConfig = Field(description="KNMI weather station configuration.", json_schema_extra={"ui_label": "KNMI", "ui_group": "basic"})
    meteoserver: MeteoserverConfig = Field(description="Meteoserver API configuration.", json_schema_extra={"ui_label": "Meteoserver", "ui_group": "basic"})
    homeassistant: HomeAssistantConfig = Field(description="Home Assistant database configuration.", json_schema_extra={"ui_label": "Home Assistant", "ui_group": "basic"})
    arrays: list[ArrayConfig] = Field(
        min_length=1,
        description="One or more PV array configurations.",
        json_schema_extra={"ui_label": "PV arrays", "ui_group": "basic"},
    )
    storage: StorageConfig = Field(description="Shared SQLite storage configuration.", json_schema_extra={"ui_label": "Storage", "ui_group": "basic"})
    training: TrainingConfig = Field(description="Training and inference trigger configuration.", json_schema_extra={"ui_label": "Training", "ui_group": "basic"})
    ha_discovery: HaDiscoveryConfig = Field(default_factory=HaDiscoveryConfig, json_schema_extra={"ui_label": "HA discovery", "ui_group": "advanced"})
    stats_topic: str | None = Field(
        default=None,
        description="MQTT topic where per-cycle run statistics are published.",
        json_schema_extra={"ui_label": "Stats topic", "ui_group": "advanced"},
    )

    @model_validator(mode="after")
    def _derive_hioo_topics(self) -> "PvLearnerConfig":
        """Fill in mimirheim-side topics that were not explicitly set.

        Derives ``output_topic`` for each array that has not set one explicitly,
        using the array ``name`` as the mimirheim ``pv_arrays`` device name. Also
        derives the default ``mimir_trigger_topic``.
        """
        p = self.mimir_topic_prefix
        for arr in self.arrays:
            if arr.output_topic is None:
                arr.output_topic = _topics.pv_forecast_topic(p, arr.name)
        if self.mimir_trigger_topic is None:
            self.mimir_trigger_topic = _topics.trigger_topic(p)
        return self

    @model_validator(mode="after")
    def _check_unique_array_names(self) -> "PvLearnerConfig":
        """Raise if two arrays share the same name."""
        names = [a.name for a in self.arrays]
        if len(names) != len(set(names)):
            duplicates = {n for n in names if names.count(n) > 1}
            raise ValueError(
                f"Array names must be unique; duplicates found: {sorted(duplicates)}"
            )
        return self


def load_config(path: str) -> PvLearnerConfig:
    """Load and validate the YAML configuration file.

    Reads the YAML file at ``path``, parses it, and validates it against
    ``PvLearnerConfig``. On failure, prints a human-readable error and exits.

    Args:
        path: Path to the YAML configuration file.

    Returns:
        The validated ``PvLearnerConfig`` instance.

    Raises:
        SystemExit: With exit code 1 if the file cannot be read or
            configuration validation fails.
    """
    try:
        with Path(path).open() as fh:
            raw = yaml.safe_load(fh)
    except OSError as exc:
        print(f"ERROR: Cannot read config file {path!r}: {exc}", file=sys.stderr)
        sys.exit(1)

    apply_mqtt_env_overrides(raw)

    try:
        return PvLearnerConfig.model_validate(raw)
    except PydanticValidationError as exc:
        print(f"ERROR: Invalid configuration in {path!r}:\n{exc}", file=sys.stderr)
        sys.exit(1)
