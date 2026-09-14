"""FormSpecs authored for helper_common's shared nested config models.

``MqttConfig`` and ``HomeAssistantConfig`` (``helper_common.config``) are
nested by every helper's own root config model. Per ADR-0007
(``mimirheim_shared/docs/adr``), a nested model's FormSpec is authored once,
in the same package that defines the model, and referenced -- not duplicated
-- by every FormSpec that nests it. This module is that authoring for
helper_common's own shared models; each helper's own ``formspec.py`` imports
these constants for its ``mqtt`` and ``ha_discovery`` fields rather than
re-describing their labels and help text.
"""

from __future__ import annotations

from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

MQTT_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "host": FieldSpec(
            label="Broker host", description="Broker hostname or IP address.", tier=Tier.BASIC
        ),
        "port": FieldSpec(
            label="Broker port", description="Broker TCP port.", tier=Tier.BASIC
        ),
        "username": FieldSpec(
            label="Username", description="Broker username.", tier=Tier.BASIC
        ),
        "password": FieldSpec(
            label="Password", description="Broker password.", tier=Tier.BASIC
        ),
        "client_id": FieldSpec(
            label="Client ID",
            description="MQTT client identifier. Defaults to a tool-specific value when not set.",
            tier=Tier.EXPERT,
        ),
        "tls": FieldSpec(
            label="Enable TLS",
            description="Enable TLS for the broker connection.",
            tier=Tier.EXPERT,
        ),
        "tls_allow_insecure": FieldSpec(
            label="Allow insecure TLS",
            description="Skip broker certificate verification when TLS is enabled.",
            tier=Tier.EXPERT,
        ),
    }
)

HOME_ASSISTANT_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "enabled": FieldSpec(
            label="Enable HA discovery",
            description="Enable HA MQTT discovery.",
            tier=Tier.BASIC,
        ),
        "discovery_prefix": FieldSpec(
            label="Discovery prefix",
            description="HA MQTT discovery topic prefix.",
            tier=Tier.EXPERT,
            visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
        ),
        "device_name": FieldSpec(
            label="HA device name",
            description="Display name for the HA device. Defaults to tool name.",
            tier=Tier.EXPERT,
            visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
        ),
        "forecast_sensor": FieldSpec(
            label="Enable forecast sensor",
            description="Publish an additional HA sensor entity for the helper's forecast output topic.",
            tier=Tier.EXPERT,
            visible_if=Comparison(field="enabled", operator=ComparisonOperator.EQ, value=True),
        ),
    }
)
