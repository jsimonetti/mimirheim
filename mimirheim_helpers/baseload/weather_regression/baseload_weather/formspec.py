"""FormSpec authored for WeatherBaseloadConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``baseload_weather.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
presentation metadata of their own. ``baseload_weather.__main__`` combines
this FormSpec with ``WeatherBaseloadConfig`` through
``helper_common.config_owner.ConfigOwnerSupport`` to build the Config Service
Descriptor.

The ``mqtt`` and ``ha_discovery`` fields nest ``helper_common``'s shared
models, so per ADR-0007 this module references ``helper_common.formspec``
instead of re-authoring their labels. The nested models defined in
``baseload_weather.config`` get their FormSpecs here. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

ENERGY_ENTITY_FORM_SPEC = FormSpec(
    fields={
        "entity_id": FieldSpec(
            label="Entity ID",
            description="Home Assistant entity ID of an energy counter, e.g. 'sensor.grid_import_kwh'.",
            tier=Tier.BASIC,
        ),
        "unit": FieldSpec(
            label="Unit override",
            description="Unit override. When unset, this is auto-detected from statistics_meta.",
            tier=Tier.EXPERT,
            option_labels={
                "Wh": "Watt-hours (Wh)",
                "kWh": "Kilowatt-hours (kWh)",
                "MWh": "Megawatt-hours (MWh)",
            },
        ),
        "outlier_factor": FieldSpec(
            label="Outlier factor",
            description="Threshold multiplier for P99-based outlier detection of hourly deltas.",
            tier=Tier.EXPERT,
        ),
    }
)

TEMPERATURE_ENTITY_FORM_SPEC = FormSpec(
    fields={
        "entity_id": FieldSpec(
            label="Entity ID",
            description="Home Assistant entity ID of an outdoor temperature sensor with long-term statistics.",
            tier=Tier.BASIC,
        ),
        "unit": FieldSpec(
            label="Unit override",
            description="Unit override. When unset, this is auto-detected from statistics_meta.",
            tier=Tier.EXPERT,
            option_labels={"°C": "Degrees Celsius (°C)", "°F": "Degrees Fahrenheit (°F)"},
        ),
    }
)

RECORDER_FORM_SPEC = FormSpec(
    fields={
        "db_url": FieldSpec(
            label="DB URL",
            description="SQLAlchemy database URL of the Home Assistant recorder database.",
            tier=Tier.BASIC,
        ),
        "sum_entities": FieldSpec(
            label="Sum entities",
            description="Energy counters whose hourly energy is summed into the load.",
            tier=Tier.BASIC,
            nested_form_spec=ENERGY_ENTITY_FORM_SPEC,
        ),
        "subtract_entities": FieldSpec(
            label="Subtract entities",
            description="Energy counters subtracted from the sum, typically loads mimirheim already schedules.",
            tier=Tier.EXPERT,
            nested_form_spec=ENERGY_ENTITY_FORM_SPEC,
        ),
        "temperature_entity": FieldSpec(
            label="Outdoor temperature entity",
            description="Outdoor temperature sensor providing the regression history.",
            tier=Tier.BASIC,
            nested_form_spec=TEMPERATURE_ENTITY_FORM_SPEC,
        ),
    }
)

WEATHER_FORECAST_FORM_SPEC = FormSpec(
    fields={
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
        "base_url": FieldSpec(
            label="API base URL",
            description="Open-Meteo API base URL.",
            tier=Tier.EXPERT,
        ),
        "timeout_s": FieldSpec(
            label="Timeout (s)",
            description="HTTP timeout per request in seconds.",
            tier=Tier.EXPERT,
        ),
    }
)

MODEL_FORM_SPEC = FormSpec(
    fields={
        "timezone": FieldSpec(
            label="Time zone",
            description="IANA time zone of the site, for example 'Europe/Amsterdam'.",
            tier=Tier.BASIC,
        ),
        "lookback_days": FieldSpec(
            label="Lookback days",
            description="Days of history the model is fitted on.",
            tier=Tier.BASIC,
        ),
        "lookback_decay": FieldSpec(
            label="Lookback decay",
            description="Recency weight ratio of the newest to the oldest day in the lookback window.",
            tier=Tier.EXPERT,
        ),
        "smoothing_hours": FieldSpec(
            label="Temperature smoothing (hours)",
            description="Time constant of the outdoor temperature moving average (building thermal mass).",
            tier=Tier.EXPERT,
        ),
        "t_base_min_c": FieldSpec(
            label="Balance temperature minimum (°C)",
            description="Lower end of the balance-temperature search range.",
            tier=Tier.EXPERT,
        ),
        "t_base_max_c": FieldSpec(
            label="Balance temperature maximum (°C)",
            description="Upper end of the balance-temperature search range.",
            tier=Tier.EXPERT,
        ),
        "min_samples": FieldSpec(
            label="Minimum samples",
            description="Minimum effective training hours a fit level needs before it is used.",
            tier=Tier.EXPERT,
        ),
        "min_hdd_variance": FieldSpec(
            label="Minimum heating-degree variance",
            description="Minimum weighted variance (K²) of the heating-degree regressor before a slope is fitted.",
            tier=Tier.EXPERT,
        ),
        "exclude_dates": FieldSpec(
            label="Exclude dates",
            description="Local dates whose load readings are excluded from training.",
            tier=Tier.EXPERT,
        ),
    }
)

WEATHER_BASELOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "homeassistant": FieldSpec(
            label="Home Assistant DB",
            description="Recorder database connection and entities.",
            tab="Home Assistant DB",
            tier=Tier.BASIC,
            nested_form_spec=RECORDER_FORM_SPEC,
        ),
        "weather_forecast": FieldSpec(
            label="Weather forecast",
            description="Open-Meteo temperature forecast settings.",
            tab="Weather",
            tier=Tier.BASIC,
            nested_form_spec=WEATHER_FORECAST_FORM_SPEC,
        ),
        "model": FieldSpec(
            label="Model",
            description="Regression model settings.",
            tab="Model",
            tier=Tier.BASIC,
            nested_form_spec=MODEL_FORM_SPEC,
        ),
        "horizon_hours": FieldSpec(
            label="Horizon (hours)",
            description="Hours of forecast to publish after the current hour.",
            tab="Model",
            tier=Tier.EXPERT,
        ),
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tab="MQTT",
            tier=Tier.BASIC,
            nested_form_spec=MQTT_CONFIG_FORM_SPEC,
        ),
        "signal_mimir": FieldSpec(
            label="Signal mimirheim",
            description="Publish an empty trigger message to mimirheim after publishing the forecast, so it solves immediately.",
            tab="MQTT",
            tier=Tier.BASIC,
        ),
        "ha_discovery": FieldSpec(
            label="Home Assistant discovery",
            description="Optional Home Assistant MQTT discovery settings for this tool.",
            tab="MQTT",
            tier=Tier.BASIC,
            nested_form_spec=HOME_ASSISTANT_CONFIG_FORM_SPEC,
        ),
        "mimir_topic_prefix": FieldSpec(
            label="mimirheim topic prefix",
            description="The mqtt.topic_prefix configured in mimirheim core. Used to derive the default topics.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "mimir_static_load_name": FieldSpec(
            label="mimirheim static load name",
            description="The static_loads device name in mimirheim this tool publishes to.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "trigger_topic": FieldSpec(
            label="Trigger topic",
            description="MQTT topic that fires one cycle when a message arrives.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "output_topic": FieldSpec(
            label="Output topic",
            description="MQTT topic the retained forecast is published to. Defaults to the standard mimirheim base load topic.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "stats_topic": FieldSpec(
            label="Stats topic",
            description="MQTT topic where per-cycle run statistics are published.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "model_stats_topic": FieldSpec(
            label="Model stats topic",
            description="MQTT topic where model fit statistics are published after each successful cycle.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after publishing the forecast.",
            tab="MQTT",
            tier=Tier.EXPERT,
            visible_if=Comparison(field="signal_mimir", operator=ComparisonOperator.EQ, value=True),
        ),
    }
)
