"""FormSpec authored for ZonneplanPricesConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``zonneplan_prices.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``zonneplan_prices.__main__``) combines with ``ZonneplanPricesConfig`` to
build its Config Service Descriptor.

``ZonneplanPricesConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``zonneplan_prices``
nests this module's own ``ZonneplanApiConfig``, so its FormSpec is authored
here. Per ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete``
checks this FormSpec recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

ZONNEPLAN_API_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "email": FieldSpec(
            label="Email address",
            description=(
                "Email address registered with the Zonneplan account. Required "
                "for first-time authentication."
            ),
            tier=Tier.BASIC,
        ),
        "token_file": FieldSpec(
            label="Token file path",
            description="Path to the JSON file where OAuth tokens are persisted between restarts.",
            tier=Tier.EXPERT,
        ),
        "import_formula": FieldSpec(
            label="Import price formula",
            description="Python expression for the all-in import price in EUR/kWh.",
            tier=Tier.BASIC,
        ),
        "export_formula": FieldSpec(
            label="Export price formula",
            description="Python expression for the net export price in EUR/kWh.",
            tier=Tier.BASIC,
        ),
        "price_interval": FieldSpec(
            label="Price interval",
            description="Price data resolution requested from Zonneplan.",
            tier=Tier.BASIC,
            option_labels={"hourly": "Hourly", "quarter_hourly": "Quarter-hourly"},
        ),
    }
)

ZONNEPLAN_PRICES_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "zonneplan": FieldSpec(
            label="Zonneplan API",
            description="Zonneplan account credentials and pricing formulas.",
            tab="Zonneplan API",
            tier=Tier.BASIC,
            nested_form_spec=ZONNEPLAN_API_CONFIG_FORM_SPEC,
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
            description="Publish an empty trigger message to mimirheim after publishing prices, so it solves immediately.",
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
        "trigger_topic": FieldSpec(
            label="Trigger topic",
            description="MQTT topic that fires one fetch-and-publish cycle when a message arrives.",
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "output_topic": FieldSpec(
            label="Output topic",
            description=(
                "MQTT topic the retained price payload is published to. Defaults to "
                "the standard mimirheim price topic when unset."
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
            description="The trigger topic to signal after publishing prices.",
            tab="MQTT",
            tier=Tier.EXPERT,
            visible_if=Comparison(
                field="signal_mimir", operator=ComparisonOperator.EQ, value=True
            ),
        ),
    }
)
