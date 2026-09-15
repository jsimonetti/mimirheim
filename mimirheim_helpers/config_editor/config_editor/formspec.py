"""FormSpec authored for ConfigEditorConfig, the Config Editor's own Config Owner presentation layer.

Kept as a separate, colocated artifact from ``config_editor.config`` per
ADR-0002 (``mimirheim_shared/docs/adr``): ``ConfigEditorConfig`` itself
carries no FormSpec-level presentation metadata. This module is what the
Config Editor's own ``describe()`` step (wired via
``helper_common.config_owner.ConfigOwnerSupport`` in
``config_editor.mqtt_client``) combines with ``ConfigEditorConfig`` to build
its own Config Service Descriptor -- the Config Editor becomes a Config
Owner of itself (config-owner-startup-resilience ticket 03), the same way
every other helper is a Config Owner of its own configuration.

``ConfigEditorConfig``'s ``mqtt`` field nests ``helper_common``'s shared
``MqttConfig`` model; per ADR-0007, this FormSpec references
``helper_common.formspec.MQTT_CONFIG_FORM_SPEC`` for that field rather than
re-authoring its labels. Per ADR-0006,
``mimirheim_shared.alignment.assert_form_spec_complete`` checks this
FormSpec recursively, at every depth.
"""

from __future__ import annotations

from helper_common.formspec import MQTT_CONFIG_FORM_SPEC
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier

CONFIG_EDITOR_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "mqtt": FieldSpec(
            label="MQTT",
            description="Broker connection parameters used to discover Config Owners.",
            tier=Tier.BASIC,
            tab="MQTT",
            nested_form_spec=MQTT_CONFIG_FORM_SPEC,
        ),
        "port": FieldSpec(
            label="HTTP port",
            description="TCP port the editor's own HTTP server listens on.",
            tier=Tier.EXPERT,
        ),
        "log_level": FieldSpec(
            label="Log level",
            description="Python logging level: DEBUG, INFO, WARNING.",
            tier=Tier.EXPERT,
        ),
        "allowed_ip": FieldSpec(
            label="Allowed IP",
            description=(
                "If set, only HTTP connections from this address are accepted. "
                "Overridden by the CONFIG_EDITOR_ALLOWED_IP environment variable "
                "when running as a Home Assistant add-on."
            ),
            tier=Tier.EXPERT,
        ),
    }
)
