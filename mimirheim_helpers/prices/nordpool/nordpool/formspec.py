"""FormSpec authored for NordpoolConfig, nordpool's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``nordpool.config`` per ADR-0002
(``mimirheim_shared/docs/adr``): ``NordpoolConfig`` itself carries no
FormSpec-level presentation metadata. This module is what ``nordpool``'s
``describe()`` step (wired via ``helper_common.config_owner.ConfigOwnerSupport``
in ``nordpool.__main__``) combines with ``NordpoolConfig`` to build its Config
Service Descriptor.

Only ``NordpoolConfig``'s own top-level fields are covered here, matching
``mimirheim_shared.alignment.assert_form_spec_complete``'s own scope (a
model's immediate fields, not nested models recursively).
"""

from __future__ import annotations

from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

NORDPOOL_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "mqtt": FieldSpec(
            label="MQTT", description="MQTT broker connection parameters.", tier=Tier.BASIC
        ),
        "nordpool": FieldSpec(
            label="Nordpool API",
            description="Nordpool area code and import/export pricing formulas.",
            tier=Tier.BASIC,
        ),
        "signal_mimir": FieldSpec(
            label="Signal mimirheim",
            description="Publish an empty trigger message to mimirheim after publishing prices, so it solves immediately.",
            tier=Tier.BASIC,
        ),
        "ha_discovery": FieldSpec(
            label="Home Assistant discovery",
            description="Optional Home Assistant MQTT discovery settings for this tool.",
            tier=Tier.EXPERT,
        ),
        "mimir_topic_prefix": FieldSpec(
            label="mimirheim topic prefix",
            description=(
                "The mqtt.topic_prefix configured in mimirheim core. Used to derive "
                "the default output and trigger topics."
            ),
            tier=Tier.EXPERT,
        ),
        "trigger_topic": FieldSpec(
            label="Trigger topic",
            description="MQTT topic that fires one fetch-and-publish cycle when a message arrives.",
            tier=Tier.EXPERT,
        ),
        "output_topic": FieldSpec(
            label="Output topic",
            description=(
                "MQTT topic the retained price payload is published to. Defaults to "
                "the standard mimirheim price topic when unset."
            ),
            tier=Tier.EXPERT,
        ),
        "stats_topic": FieldSpec(
            label="Stats topic",
            description="MQTT topic where per-cycle run statistics are published.",
            tier=Tier.EXPERT,
        ),
        "mimir_trigger_topic": FieldSpec(
            label="mimirheim trigger topic",
            description="The trigger topic to signal after publishing prices.",
            tier=Tier.EXPERT,
            # Only meaningful when signal_mimir is enabled; see spec.md user
            # story 4 (conditional visibility for feature-gated fields).
            visible_if=Comparison(
                field="signal_mimir", operator=ComparisonOperator.EQ, value=True
            ),
        ),
    }
)
