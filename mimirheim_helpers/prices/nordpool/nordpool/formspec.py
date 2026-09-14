"""FormSpec authored for NordpoolConfig, nordpool's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``nordpool.config`` per ADR-0002
(``mimirheim_shared/docs/adr``): ``NordpoolConfig`` itself carries no
FormSpec-level presentation metadata. This module is what ``nordpool``'s
``describe()`` step (wired via ``helper_common.config_owner.ConfigOwnerSupport``
in ``nordpool.__main__``) combines with ``NordpoolConfig`` to build its Config
Service Descriptor.

``NordpoolConfig``'s ``mqtt`` and ``ha_discovery`` fields nest
``helper_common``'s shared ``MqttConfig`` and ``HomeAssistantConfig`` models;
per ADR-0007, this FormSpec references ``helper_common.formspec``'s FormSpecs
for those fields rather than re-authoring their labels. ``nordpool`` nests
this module's own ``NordpoolApiConfig``, so its FormSpec is authored here.
Per ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete`` checks
this FormSpec recursively, at every depth.
"""

from __future__ import annotations

from pynordpool.const import AREAS

from helper_common.formspec import HOME_ASSISTANT_CONFIG_FORM_SPEC, MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

# Display labels for the "area" select box, e.g. "NO2 -- Norway 2". Built from
# the same pynordpool.const.AREAS registry nordpool.config.NordpoolApiConfig
# derives its area Literal from, so the two never drift apart.
_AREA_OPTION_LABELS = {code: f"{name} ({code})" for code, name in AREAS.items() if code != "SYS"}

NORDPOOL_API_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "area": FieldSpec(
            label="Nordpool area",
            description="Nordpool price area code (e.g. 'NO2', 'NL', 'SE3').",
            tier=Tier.BASIC,
            option_labels=_AREA_OPTION_LABELS,
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

NORDPOOL_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "nordpool": FieldSpec(
            label="Nordpool API",
            description="Nordpool area code and import/export pricing formulas.",
            tab="Nordpool API",
            tier=Tier.BASIC,
            nested_form_spec=NORDPOOL_API_CONFIG_FORM_SPEC,
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
            # Only meaningful when signal_mimir is enabled; see spec.md user
            # story 4 (conditional visibility for feature-gated fields).
            visible_if=Comparison(
                field="signal_mimir", operator=ComparisonOperator.EQ, value=True
            ),
        ),
    }
)
