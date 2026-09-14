"""FormSpec authored for EpexPredictorPricesConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``epexpredictor_prices.config``
per ADR-0002 (``mimirheim_shared/docs/adr``): the config models carry no
FormSpec-level presentation metadata of their own. This module is what this
helper's ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``epexpredictor_prices.__main__``) combines with
``EpexPredictorPricesConfig`` to build its Config Service Descriptor.

``EpexPredictorPricesConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. This module authors
its own ``EpexPredictorApiConfig`` and ``ConfidenceDecayConfig`` nested
FormSpecs, since those models are defined here. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this FormSpec
recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

EPEXPREDICTOR_API_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "area": FieldSpec(
            label="EPEX area",
            description="EPEX bidding zone code (e.g. 'DE', 'NL', 'SE3').",
            tier=Tier.BASIC,
        ),
        "base_url": FieldSpec(
            label="API base URL",
            description="EpexPredictor API base URL. Change for a self-hosted instance.",
            tier=Tier.EXPERT,
        ),
        "horizon_hours": FieldSpec(
            label="Horizon (hours)",
            description="How many hours ahead to request from the API.",
            tier=Tier.BASIC,
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
            description="Length of one published price step.",
            tier=Tier.BASIC,
            option_labels={"hourly": "Hourly", "quarter_hourly": "Quarter-hourly"},
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
        "known_until_confidence": FieldSpec(
            label="Known-until confidence override",
            description=(
                "Overrides the confidence of every step at or before the API's "
                "knownUntil timestamp with this fixed value. Empty applies the "
                "decay bands uniformly, including to real data."
            ),
            tier=Tier.EXPERT,
        ),
    }
)

EPEXPREDICTOR_PRICES_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "epexpredictor": FieldSpec(
            label="EpexPredictor API",
            description="EPEX area code and import/export pricing formulas.",
            tab="EpexPredictor API",
            tier=Tier.BASIC,
            nested_form_spec=EPEXPREDICTOR_API_CONFIG_FORM_SPEC,
        ),
        "confidence_decay": FieldSpec(
            label="Confidence decay",
            description="Confidence values assigned to forecast steps by how far ahead they are.",
            tab="EpexPredictor API",
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
