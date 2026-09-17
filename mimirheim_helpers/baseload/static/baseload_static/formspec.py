"""FormSpec authored for BaseloadConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``baseload_static.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``baseload_static.__main__``) combines with ``BaseloadConfig`` to build its
Config Service Descriptor.

``BaseloadConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``baseload_static``
nests this module's own ``StaticBaseloadConfig``, so its FormSpec is authored
here. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

STATIC_BASELOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "profile_kw": FieldSpec(
            label="Hourly profile (kW)",
            description=(
                "Power values in kilowatts that form the repeating cycle used when "
                "no per-weekday override applies. Typically 24 values, one per "
                "hour-of-day."
            ),
            tier=Tier.BASIC,
        ),
        "weekly_profiles_kw": FieldSpec(
            label="Weekly profiles (kW)",
            description=(
                "Optional per-weekday profiles in kilowatts, keyed by weekday number "
                "(0=Monday ... 6=Sunday). Takes precedence over profile_kw."
            ),
            tier=Tier.EXPERT,
        ),
        "horizon_hours": FieldSpec(
            label="Horizon (hours)",
            description="Number of hourly steps to publish, starting from the current wall-clock hour.",
            tier=Tier.EXPERT,
        ),
    }
)

BASELOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "baseload": FieldSpec(
            label="Baseload profile",
            description="Static load profile and horizon configuration.",
            tab="Baseload profile",
            tier=Tier.BASIC,
            nested_form_spec=STATIC_BASELOAD_CONFIG_FORM_SPEC,
        ),
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tier=Tier.BASIC,
            tab="MQTT",
            nested_form_spec=MQTT_CONFIG_FORM_SPEC,
        ),
        "signal_mimir": FieldSpec(
            label="Signal mimirheim",
            tab="MQTT",
            description="Publish an empty trigger message to mimirheim after publishing the forecast, so it solves immediately.",
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
            description=(
                "The mqtt.topic_prefix configured in mimirheim core. Used to derive "
                "the default output and trigger topics."
            ),
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
            description="MQTT topic that fires one publish cycle when a message arrives.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "output_topic": FieldSpec(
            label="Output topic",
            description=(
                "MQTT topic the retained baseload forecast payload is published to. "
                "Defaults to the standard mimirheim baseload topic when unset."
            ),
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "stats_topic": FieldSpec(
            label="Stats topic",
            description="MQTT topic where per-cycle run statistics are published.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after publishing the forecast.",
            tab="MQTT",
            tier=Tier.EXPERT,
            visible_if=Comparison(
                field="signal_mimir", operator=ComparisonOperator.EQ, value=True
            ),
        ),
    }
)
