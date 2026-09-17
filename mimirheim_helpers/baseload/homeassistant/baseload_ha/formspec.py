"""FormSpec authored for BaseloadConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``baseload_ha.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in ``baseload_ha.__main__``)
combines with ``BaseloadConfig`` to build its Config Service Descriptor.

``BaseloadConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``baseload_ha`` nests
this module's own ``EntityConfig`` and ``HaConfig``, so their FormSpecs are
authored here. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

ENTITY_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "entity_id": FieldSpec(
            label="Entity ID",
            description="Home Assistant entity ID, e.g. 'sensor.kitchen_power'.",
            tier=Tier.BASIC,
        ),
        "unit": FieldSpec(
            label="Unit",
            description="Unit in which this entity reports power.",
            tier=Tier.BASIC,
            option_labels={"W": "Watts (W)", "kW": "Kilowatts (kW)"},
        ),
    }
)

HA_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "url": FieldSpec(
            label="HA URL",
            description="Base URL of the HA instance including scheme and port.",
            tier=Tier.BASIC,
        ),
        "token": FieldSpec(
            label="HA token",
            description="Long-Lived Access Token generated in HA.",
            tier=Tier.BASIC,
        ),
        "sum_entities": FieldSpec(
            label="Sum entities",
            description="Entities whose hourly mean power is summed.",
            tier=Tier.BASIC,
            nested_form_spec=ENTITY_CONFIG_FORM_SPEC,
        ),
        "subtract_entities": FieldSpec(
            label="Subtract entities",
            description=(
                "Entities subtracted from the sum, typically steered loads mimirheim "
                "already controls."
            ),
            tier=Tier.EXPERT,
            nested_form_spec=ENTITY_CONFIG_FORM_SPEC,
        ),
        "lookback_days": FieldSpec(
            label="Lookback days",
            description="Number of previous days of history to average.",
            tier=Tier.EXPERT,
        ),
        "lookback_decay": FieldSpec(
            label="Lookback decay",
            description="Recency weight ratio applied over the lookback window.",
            tier=Tier.EXPERT,
        ),
        "horizon_hours": FieldSpec(
            label="Horizon (hours)",
            description="Number of hours of forecast to publish.",
            tier=Tier.EXPERT,
        ),
    }
)

BASELOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "homeassistant": FieldSpec(
            label="Home Assistant",
            description="HA connection and entity configuration.",
            tab="Home Assistant",
            tier=Tier.BASIC,
            nested_form_spec=HA_CONFIG_FORM_SPEC,
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
            description="MQTT topic that fires one fetch-and-publish cycle when a message arrives.",
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
