"""MQTT wiring that keeps a ConfigOwnerRegistry in sync with retained Descriptors.

Subscribes to the well-known Config Service Descriptor wildcard topic
(`mimirheim_shared.config_service.descriptor_topic("+")`) and updates
`registry.py`'s `ConfigOwnerRegistry` as Descriptors are published or cleared
(see `mimirheim_shared/docs/adr/0001`). This is the Config Editor's only MQTT
responsibility: rendering (`server.py`) is a deliberately separate concern
that never touches MQTT itself.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import paho.mqtt.client as mqtt
from helper_common.daemon import MqttDaemon
from mimirheim_shared.config_service import Descriptor, descriptor_topic
from pydantic import ValidationError

from config_editor_v3.registry import ConfigOwnerRegistry

logger = logging.getLogger(__name__)

# Matches every Config Owner's retained Descriptor topic, regardless of owner_id.
_DESCRIPTOR_TOPIC_WILDCARD = descriptor_topic("+")

# Index of the owner_id segment in "mimirheim/config-service/<owner_id>/descriptor".
_OWNER_ID_TOPIC_INDEX = 2


class ConfigEditorMqttClient(MqttDaemon):
    """Subscribes to every Config Owner's Descriptor topic and updates a registry.

    Args:
        config: Validated configuration with a `.mqtt` attribute (see
            `helper_common.config.MqttConfig`), per `MqttDaemon`'s own contract.
        registry: The registry to update as Descriptors arrive or are cleared.
    """

    def __init__(self, config: Any, registry: ConfigOwnerRegistry) -> None:
        self._registry = registry
        super().__init__(config)

    def start(self) -> None:
        """Connects to the broker and starts the network loop in a background thread."""
        cfg = self._config.mqtt
        self._client.connect(cfg.host, cfg.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Stops the network loop and disconnects cleanly."""
        self._client.loop_stop()
        self._client.disconnect()

    def _on_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        super()._on_connect(client, userdata, flags, reason_code, properties)
        if reason_code.is_failure:
            return
        client.subscribe(_DESCRIPTOR_TOPIC_WILDCARD, qos=1)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        """Registers or removes a Config Owner from a retained Descriptor message.

        An empty payload is the MQTT clearing idiom used both for a Config
        Owner's graceful-shutdown publish and its last-will (see
        `mimirheim_shared.config_service.CLEARING_PAYLOAD`); either way, the
        owner_id parsed from the topic is removed from the registry.

        A payload that fails to parse as JSON or fails Descriptor validation
        is logged and discarded rather than raised: an unhandled exception
        here would propagate into the paho network thread and kill it, taking
        down Descriptor discovery for every other Config Owner along with it.

        Args:
            client: The paho client that received the message. Unused.
            userdata: Unused; part of the paho callback signature.
            message: The paho `MQTTMessage`.
        """
        owner_id = message.topic.split("/")[_OWNER_ID_TOPIC_INDEX]

        if not message.payload:
            self._registry.remove(owner_id)
            logger.info("Config Owner %r disconnected; removed from registry.", owner_id)
            return

        try:
            descriptor = Descriptor.model_validate_json(message.payload)
        except (ValidationError, json.JSONDecodeError):
            logger.exception("Discarding malformed Descriptor on %r.", message.topic)
            return

        self._registry.update(descriptor)
        logger.info("Registered Config Owner %r (%s).", descriptor.owner_id, descriptor.display_name)
