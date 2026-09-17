"""FormSpec authored for PvOpenMeteoConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``pv_openmeteo.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``pv_openmeteo.__main__``) combines with ``PvOpenMeteoConfig`` to build its
Config Service Descriptor.

``PvOpenMeteoConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``pv_openmeteo``
nests four levels of its own models -- ``HorizonPoint`` inside
``PlaneConfig`` inside ``ArrayConfig`` (a Named Collection, one entry per PV
array per ADR-0008) inside the root -- plus ``OpenMeteoApiConfig``,
``SiteConfig``, and ``ConfidenceDecayConfig``, so their FormSpecs are
authored here, one per level. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

OPEN_METEO_API_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "api_key": FieldSpec(
            label="API key",
            description="Open-Meteo API key for a commercial subscription. Leave unset for the free public endpoint.",
            tier=Tier.EXPERT,
        ),
        "base_url": FieldSpec(
            label="API base URL",
            description="API base URL. Change for a commercial or self-hosted endpoint.",
            tier=Tier.EXPERT,
        ),
        "weather_model": FieldSpec(
            label="Weather model",
            description="Open-Meteo weather model name. Leave unset to let Open-Meteo choose per location.",
            tier=Tier.EXPERT,
        ),
        "forecast_days": FieldSpec(
            label="Forecast days",
            description="Days of forecast to request, including today.",
            tier=Tier.EXPERT,
        ),
        "past_hours": FieldSpec(
            label="Past hours retained",
            description="Hours of already-elapsed forecast to keep in the published payload.",
            tier=Tier.EXPERT,
        ),
    }
)

SITE_CONFIG_FORM_SPEC = FormSpec(
    fields={
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
    }
)

HORIZON_POINT_FORM_SPEC = FormSpec(
    fields={
        "azimuth": FieldSpec(
            label="Bearing",
            description="Compass bearing in degrees: 0 = north, 90 = east, 180 = south.",
            tier=Tier.EXPERT,
        ),
        "elevation": FieldSpec(
            label="Elevation",
            description="Obstacle elevation above the horizontal in degrees.",
            tier=Tier.EXPERT,
        ),
    }
)

PLANE_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "label": FieldSpec(
            label="Label",
            description="Optional plane name, used in log messages only.",
            tier=Tier.BASIC,
        ),
        "latitude": FieldSpec(
            label="Latitude override",
            description="Plane latitude. Defaults to the site latitude.",
            tier=Tier.EXPERT,
        ),
        "longitude": FieldSpec(
            label="Longitude override",
            description="Plane longitude. Defaults to the site longitude.",
            tier=Tier.EXPERT,
        ),
        "declination": FieldSpec(
            label="Panel tilt",
            description="Panel tilt in degrees from horizontal. 0 = flat, 90 = vertical.",
            tier=Tier.BASIC,
        ),
        "azimuth": FieldSpec(
            label="Panel azimuth",
            description="Panel orientation in degrees from south: 0 = south, -90 = east, 90 = west.",
            tier=Tier.BASIC,
        ),
        "peak_power_kwp": FieldSpec(
            label="Peak power (kWp)",
            description="Nameplate DC power of this plane in kWp.",
            tier=Tier.BASIC,
        ),
        "inverter_kwp": FieldSpec(
            label="Plane inverter (kW)",
            description="AC limit of this plane's own inverter in kW. Leave unset when the array shares one inverter.",
            tier=Tier.BASIC,
        ),
        "efficiency_factor": FieldSpec(
            label="Efficiency factor",
            description="Linear system efficiency: inverter, cabling, soiling, degradation.",
            tier=Tier.BASIC,
        ),
        "tracking": FieldSpec(
            label="Tracking",
            description="Axis tracking mode.",
            tier=Tier.EXPERT,
            option_labels={
                "none": "Fixed mount",
                "azimuth": "Azimuth tracking",
                "tilt": "Tilt tracking",
                "dual": "Dual-axis tracking",
            },
        ),
        "damping_morning": FieldSpec(
            label="Morning damping",
            description="Fraction of production removed at sunrise, tapering to none at solar noon.",
            tier=Tier.EXPERT,
        ),
        "damping_evening": FieldSpec(
            label="Evening damping",
            description="Fraction of production removed at sunset, tapering from none at solar noon.",
            tier=Tier.EXPERT,
        ),
        "max_snowcover_depth_cm": FieldSpec(
            label="Snow cover depth (cm)",
            description="Snow depth at which the plane produces nothing. 0 disables the correction.",
            tier=Tier.EXPERT,
        ),
        "horizon": FieldSpec(
            label="Horizon profile",
            description="Skyline profile in ascending compass bearing. Leave unset to disable horizon shading.",
            tier=Tier.EXPERT,
            nested_form_spec=HORIZON_POINT_FORM_SPEC,
        ),
        "partial_shading": FieldSpec(
            label="Partial shading",
            description="Credit diffuse light while the horizon blocks the direct beam.",
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
        "inverter_kwp": FieldSpec(
            label="Shared inverter (kW)",
            description="AC limit of the inverter shared by all planes in this array, in kW.",
            tier=Tier.BASIC,
        ),
        "planes": FieldSpec(
            label="Planes",
            description="Coplanar module groups making up this array.",
            tier=Tier.BASIC,
            nested_form_spec=PLANE_CONFIG_FORM_SPEC,
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

PV_OPENMETEO_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "open_meteo": FieldSpec(
            label="Open-Meteo API",
            description="Open-Meteo API configuration shared by every array.",
            tab="Open-Meteo",
            tier=Tier.BASIC,
            nested_form_spec=OPEN_METEO_API_CONFIG_FORM_SPEC,
        ),
        "site": FieldSpec(
            label="Site",
            description="Geographic location of the installation, inherited by every plane.",
            tab="Open-Meteo",
            tier=Tier.BASIC,
            nested_form_spec=SITE_CONFIG_FORM_SPEC,
        ),
        "arrays": FieldSpec(
            label="PV arrays",
            description=(
                "Named map of PV arrays. Each key is used as the mimirheim "
                "pv_arrays device name for topic derivation unless the array's "
                "output_topic is explicitly set."
            ),
            tab="Open-Meteo",
            tier=Tier.BASIC,
            nested_form_spec=ARRAY_CONFIG_FORM_SPEC,
        ),
        "confidence_decay": FieldSpec(
            label="Confidence decay",
            description="Per-horizon-band confidence values.",
            tab="Open-Meteo",
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
                "default array and trigger topics."
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
            description="Publish an empty trigger message to mimirheim after publishing, so it solves immediately.",
            tab="MQTT",
            tier=Tier.BASIC,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after publishing.",
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
