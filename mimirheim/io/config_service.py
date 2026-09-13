"""Builds mimirheim core's Config Service Descriptor.

Mimirheim core shares its one existing MQTT connection for the Config
Service protocol (see mimirheim_shared/docs/adr/0005):
``mimirheim.io.mqtt_client.MqttClient`` owns that connection and calls into
this module for the Descriptor's well-known topic and serialised payload,
publishing it retained on connect and clearing it on graceful shutdown. This
module never touches an MQTT client itself, matching
``mimirheim_shared.config_service``'s own no-owned-connection design.
"""

from __future__ import annotations

from mimirheim_shared.config_service import build_descriptor, descriptor_payload, descriptor_topic

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig

# Stable regardless of user configuration (mqtt.client_id may vary per
# deployment or be auto-generated); the Config Editor needs a fixed identity
# for mimirheim core across restarts and reconfiguration.
OWNER_ID = "mimirheim-core"
DISPLAY_NAME = "Mimirheim"

# The well-known, retained topic mimirheim core's Descriptor is published to.
TOPIC = descriptor_topic(OWNER_ID)


def payload_bytes() -> bytes:
    """Serialise mimirheim core's Config Service Descriptor to publish retained.

    A plain function rather than a module-level constant: it is called once,
    at ``MqttClient`` construction, so there is no benefit to computing it
    before any caller actually needs it.

    Returns:
        UTF-8 encoded JSON bytes ready to publish to ``TOPIC``.
    """
    return descriptor_payload(
        build_descriptor(OWNER_ID, DISPLAY_NAME, MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)
    )
