"""FormSpec authored for PvFetcherConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``pv_fetcher.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in ``pv_fetcher.__main__``)
combines with ``PvFetcherConfig`` to build its Config Service Descriptor.

``PvFetcherConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``pv_fetcher`` nests
this module's own ``ForecastSolarApiConfig``, ``ArrayConfig`` (a Named
Collection, one entry per PV array per ADR-0008), and
``ConfidenceDecayConfig``, so their FormSpecs are authored here. Per
ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete`` checks this
FormSpec recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

FORECAST_SOLAR_API_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "api_key": FieldSpec(
            label="API key",
            description=(
                "forecast.solar API key for a paid tier. Leave unset to use the "
                "free anonymous tier (60 requests/hour, one-day horizon)."
            ),
            tier=Tier.EXPERT,
        ),
    }
)

ARRAY_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "output_topic": FieldSpec(
            label="Output topic",
            description=(
                "MQTT topic for the forecast payload. Retained. Defaults to "
                "'{mimir_topic_prefix}/input/pv/{array_key}/forecast' when unset."
            ),
            tier=Tier.EXPERT,
        ),
        "latitude": FieldSpec(
            label="Latitude",
            description="Site latitude in decimal degrees (positive = north).",
            tier=Tier.BASIC,
        ),
        "longitude": FieldSpec(
            label="Longitude",
            description="Site longitude in decimal degrees (positive = east).",
            tier=Tier.BASIC,
        ),
        "declination": FieldSpec(
            label="Panel tilt",
            description="Panel tilt in degrees from horizontal. 0 = flat, 90 = vertical.",
            tier=Tier.BASIC,
        ),
        "azimuth": FieldSpec(
            label="Panel azimuth",
            description="Panel azimuth: deviation from south in degrees. 0=south, -90=east, 90=west.",
            tier=Tier.BASIC,
        ),
        "peak_power_kwp": FieldSpec(
            label="Peak power (kWp)",
            description="Array peak power in kWp.",
            tier=Tier.BASIC,
        ),
    }
)

CONFIDENCE_DECAY_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "hours_0_to_6": FieldSpec(
            label="Confidence 0-6 h",
            description="Confidence assigned to steps 0-6 hours ahead.",
            tier=Tier.EXPERT,
        ),
        "hours_6_to_24": FieldSpec(
            label="Confidence 6-24 h",
            description="Confidence assigned to steps 6-24 hours ahead.",
            tier=Tier.EXPERT,
        ),
        "hours_24_to_48": FieldSpec(
            label="Confidence 24-48 h",
            description="Confidence assigned to steps 24-48 hours ahead.",
            tier=Tier.EXPERT,
        ),
        "hours_48_plus": FieldSpec(
            label="Confidence 48+ h",
            description="Confidence assigned to steps more than 48 hours ahead.",
            tier=Tier.EXPERT,
        ),
    }
)

PV_FETCHER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "forecast_solar": FieldSpec(
            label="forecast.solar API",
            description="forecast.solar API configuration.",
            tab="forecast.solar",
            tier=Tier.BASIC,
            nested_form_spec=FORECAST_SOLAR_API_CONFIG_FORM_SPEC,
        ),
        "arrays": FieldSpec(
            label="PV arrays",
            description=(
                "Named map of PV array configurations. Each key is used as the "
                "mimirheim pv_arrays device name for topic derivation unless the "
                "array's output_topic is explicitly set."
            ),
            tab="forecast.solar",
            tier=Tier.BASIC,
            nested_form_spec=ARRAY_CONFIG_FORM_SPEC,
        ),
        "confidence_decay": FieldSpec(
            label="Confidence decay",
            description="Per-horizon-band confidence values.",
            tab="forecast.solar",
            tier=Tier.EXPERT,
            nested_form_spec=CONFIDENCE_DECAY_CONFIG_FORM_SPEC,
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
        "trigger_topic": FieldSpec(
            label="Trigger topic",
            description="MQTT topic that triggers one fetch-and-publish cycle.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "signal_mimir": FieldSpec(
            label="Signal mimirheim",
            description="Publish an empty trigger message to mimirheim after all arrays are published, so it solves immediately.",
            tab="MQTT",
            tier=Tier.BASIC,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after publishing all arrays.",
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
