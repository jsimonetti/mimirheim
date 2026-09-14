"""FormSpec authored for ReporterConfig, this helper's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``reporter.config`` per ADR-0002
(``mimirheim_shared/docs/adr``): the config models carry no FormSpec-level
presentation metadata of their own. This module is what this helper's
``describe()`` step (wired via ``helper_common.config_owner.ConfigOwnerSupport``
in ``reporter.__main__``) combines with ``ReporterConfig`` to build its
Config Service Descriptor.

``ReporterConfig``'s ``mqtt`` field nests ``helper_common``'s shared
``MqttConfig`` model; per ADR-0007, this FormSpec references
``helper_common.formspec``'s FormSpec for that field rather than
re-authoring its labels. ``reporter`` nests its own
``ReporterReportingSection``, so its FormSpec is authored here. Per
ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete`` checks
this FormSpec recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier

REPORTER_REPORTING_SECTION_FORM_SPEC = FormSpec(
    fields={
        "dump_dir": FieldSpec(
            label="Dump directory",
            description="Directory shared with mimirheim, containing solve dump pairs.",
            tier=Tier.BASIC,
        ),
        "output_dir": FieldSpec(
            label="Output directory",
            description="Directory where the reporter writes HTML reports.",
            tier=Tier.BASIC,
        ),
        "max_reports": FieldSpec(
            label="Max reports",
            description="Maximum retained HTML reports. 0 = unlimited.",
            tier=Tier.EXPERT,
        ),
        "notify_topic": FieldSpec(
            label="Notify topic",
            description="MQTT topic to subscribe to for dump-available notifications.",
            tier=Tier.EXPERT,
        ),
    }
)

REPORTER_CONFIG_FORM_SPEC = FormSpec(
    fields={
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
                "the default notify_topic."
            ),
            tab="MQTT",
            tier=Tier.EXPERT,
        ),
        "reporting": FieldSpec(
            label="Reporting",
            description="Reporting paths and retention settings.",
            tab="Reporting",
            tier=Tier.BASIC,
            nested_form_spec=REPORTER_REPORTING_SECTION_FORM_SPEC,
        ),
    }
)
