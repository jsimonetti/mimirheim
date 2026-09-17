"""FormSpec authored for PvLearnerConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``pv_ml_learner.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``pv_ml_learner.__main__``) combines with ``PvLearnerConfig`` to build its
Config Service Descriptor.

``PvLearnerConfig``'s ``mqtt`` field nests ``helper_common``'s shared
``MqttConfig`` model; per ADR-0007, this FormSpec references
``helper_common.formspec``'s FormSpec for that field rather than
re-authoring its labels. ``ha_discovery`` nests ``pv_ml_learner``'s own
``HaDiscoveryConfig``, a subclass of ``helper_common``'s
``HomeAssistantConfig`` that only changes a default value and adds no new
fields, so the shared ``HOME_ASSISTANT_CONFIG_FORM_SPEC`` still applies.
``pv_ml_learner`` nests several of its own models -- ``KnmiConfig``,
``MeteoserverConfig``, ``HomeAssistantConfig``, ``ArrayConfig`` (a Named
Collection, one entry per PV array per ADR-0008), ``StorageConfig``,
``TrainingConfig`` (which itself nests ``HyperparamConfig``) -- so their
FormSpecs are authored here. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

KNMI_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "station_id": FieldSpec(
            label="KNMI station ID",
            description="KNMI station ID. 260 = De Bilt.",
            tier=Tier.BASIC,
        ),
    }
)

METEOSERVER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "api_key": FieldSpec(
            label="API key",
            description="Meteoserver API key.",
            tier=Tier.BASIC,
        ),
        "latitude": FieldSpec(
            label="Latitude",
            description="Site latitude in decimal degrees.",
            tier=Tier.BASIC,
        ),
        "longitude": FieldSpec(
            label="Longitude",
            description="Site longitude in decimal degrees.",
            tier=Tier.BASIC,
        ),
        "forecast_horizon_hours": FieldSpec(
            label="Forecast horizon (h)",
            description="Number of hourly forecast steps to use (1-54).",
            tier=Tier.EXPERT,
        ),
    }
)

HOMEASSISTANT_DB_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "db_url": FieldSpec(
            label="HA DB URL",
            description=(
                "SQLAlchemy connection URL for the HA recorder database, e.g. "
                "sqlite:////config/home-assistant_v2.db."
            ),
            tier=Tier.BASIC,
        ),
    }
)

ARRAY_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "peak_power_kwp": FieldSpec(
            label="Peak power (kWp)",
            description="Installed PV peak power in kWp.",
            tier=Tier.BASIC,
        ),
        "output_topic": FieldSpec(
            label="Output topic",
            description=(
                "MQTT topic for the retained forecast payload. Defaults to "
                "'{mimir_topic_prefix}/input/pv/{array_key}/forecast' when unset."
            ),
            tier=Tier.EXPERT,
        ),
        "sum_entity_ids": FieldSpec(
            label="Sum entity IDs",
            description="Entity IDs to sum for hourly PV production.",
            tier=Tier.BASIC,
        ),
        "model_path": FieldSpec(
            label="Model path",
            description="Path where the trained model is serialised.",
            tier=Tier.EXPERT,
        ),
        "metadata_path": FieldSpec(
            label="Metadata path",
            description="Path for the JSON metadata file written alongside the model.",
            tier=Tier.EXPERT,
        ),
        "exclude_limiting_entity_ids": FieldSpec(
            label="Exclude limiting entities",
            description=(
                "Binary/numeric sensors indicating active inverter limiting. "
                "Matching training hours are excluded from the dataset."
            ),
            tier=Tier.EXPERT,
        ),
    }
)

STORAGE_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "db_path": FieldSpec(
            label="Storage DB path",
            description="Path to the shared SQLite database.",
            tier=Tier.BASIC,
        ),
    }
)

HYPERPARAM_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "n_estimators": FieldSpec(
            label="n_estimators",
            description="Number of boosting rounds to try.",
            tier=Tier.EXPERT,
        ),
        "max_depth": FieldSpec(
            label="max_depth",
            description="Maximum tree depth values to try.",
            tier=Tier.EXPERT,
        ),
        "learning_rate": FieldSpec(
            label="learning_rate",
            description="Learning rate (eta) values to try.",
            tier=Tier.EXPERT,
        ),
        "subsample": FieldSpec(
            label="subsample",
            description="Row subsampling fraction values to try.",
            tier=Tier.EXPERT,
        ),
        "min_child_weight": FieldSpec(
            label="min_child_weight",
            description="Minimum child weight values to try.",
            tier=Tier.EXPERT,
        ),
    }
)

TRAINING_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "train_trigger_topic": FieldSpec(
            label="Train trigger topic",
            description="MQTT topic that triggers a training run.",
            tier=Tier.BASIC,
        ),
        "inference_trigger_topic": FieldSpec(
            label="Inference trigger topic",
            description="MQTT topic that triggers an inference run.",
            tier=Tier.BASIC,
        ),
        "min_months_required": FieldSpec(
            label="Min months required",
            description="Minimum distinct calendar months required to train.",
            tier=Tier.EXPERT,
        ),
        "hyperparams": FieldSpec(
            label="Hyperparameters",
            description="XGBoost grid search configuration.",
            tier=Tier.EXPERT,
            nested_form_spec=HYPERPARAM_CONFIG_FORM_SPEC,
        ),
        "n_cv_splits": FieldSpec(
            label="CV splits",
            description="Number of TimeSeriesSplit CV folds.",
            tier=Tier.EXPERT,
        ),
    }
)

PV_LEARNER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "knmi": FieldSpec(
            label="KNMI",
            description="KNMI weather station configuration.",
            tab="Weather data",
            tier=Tier.BASIC,
            nested_form_spec=KNMI_CONFIG_FORM_SPEC,
        ),
        "meteoserver": FieldSpec(
            label="Meteoserver",
            description="Meteoserver hourly weather forecast API configuration.",
            tab="Weather data",
            tier=Tier.BASIC,
            nested_form_spec=METEOSERVER_CONFIG_FORM_SPEC,
        ),
        "homeassistant": FieldSpec(
            label="Home Assistant",
            description="Home Assistant recorder database configuration.",
            tab="Home Assistant",
            tier=Tier.BASIC,
            nested_form_spec=HOMEASSISTANT_DB_CONFIG_FORM_SPEC,
        ),
        "arrays": FieldSpec(
            label="PV arrays",
            description=(
                "Named map of PV array configurations. Each key is used as the "
                "array identifier and mimirheim pv_arrays device name."
            ),
            tab="PV arrays",
            tier=Tier.BASIC,
            nested_form_spec=ARRAY_CONFIG_FORM_SPEC,
        ),
        "storage": FieldSpec(
            label="Storage",
            description="Shared SQLite storage configuration.",
            tab="Training",
            tier=Tier.BASIC,
            nested_form_spec=STORAGE_CONFIG_FORM_SPEC,
        ),
        "training": FieldSpec(
            label="Training",
            description="Training and inference trigger configuration.",
            tab="Training",
            tier=Tier.BASIC,
            nested_form_spec=TRAINING_CONFIG_FORM_SPEC,
        ),
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tier=Tier.BASIC,
            tab="MQTT",
            nested_form_spec=MQTT_CONFIG_FORM_SPEC,
        ),
        "mimir_topic_prefix": FieldSpec(
            label="mimirheim topic prefix",
            description=(
                "The mqtt.topic_prefix configured in mimirheim core. Used to derive "
                "default array output and trigger topics."
            ),
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "signal_mimir": FieldSpec(
            label="Signal mimirheim",
            description="Publish an empty trigger message to mimirheim after each forecast cycle, so it solves immediately.",
            tab="MQTT",
            tier=Tier.BASIC,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after each forecast cycle.",
            tab="MQTT",
            tier=Tier.EXPERT,
            visible_if=Comparison(
                field="signal_mimir", operator=ComparisonOperator.EQ, value=True
            ),
        ),
        "ha_discovery": FieldSpec(
            label="Home Assistant discovery",
            description="Optional Home Assistant MQTT discovery settings for this tool.",
            tab="MQTT",
            tier=Tier.BASIC,
            nested_form_spec=HOME_ASSISTANT_CONFIG_FORM_SPEC,
        ),
        "stats_topic": FieldSpec(
            label="Stats topic",
            description="MQTT topic where per-cycle run statistics are published.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
    }
)
