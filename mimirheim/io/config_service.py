"""Wires mimirheim core into the Config Service protocol as a Config Owner.

Publishes a retained Descriptor (mimirheim_shared.config_service.Descriptor,
built from MimirheimConfig plus MIMIRHEIM_CONFIG_FORM_SPEC) on connect, and
registers a last-will that clears it on ungraceful disconnect.

Mimirheim's primary MqttClient (mimirheim.io.mqtt_client) already registers
its own last-will, clearing config.outputs.availability -- and MQTT permits
exactly one last-will per connection. ConfigServiceClient is therefore
designed to run on a second, dedicated paho client (see mimirheim/__main__.py),
so neither last-will clobbers the other. mimirheim_shared itself never owns
or constructs an MQTT connection; see mimirheim_shared/docs/adr/0005. This
module is where mimirheim core supplies one, following the same
injected-client pattern as MqttClient.
"""

from __future__ import annotations

import logging
from typing import Any

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
)

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig

logger = logging.getLogger("mimirheim.config_service")

# Stable regardless of user configuration (mqtt.client_id may vary per
# deployment or be auto-generated); the Config Editor needs a fixed identity
# for mimirheim core across restarts and reconfiguration.
OWNER_ID = "mimirheim-core"
DISPLAY_NAME = "Mimirheim"


class ConfigServiceClient:
    """Publishes mimirheim core's Config Service Descriptor on a dedicated MQTT connection.

    Attributes:
        _client: The injected paho client. Constructed and connected by the
            caller (mimirheim/__main__.py), never by this class.
        _topic: The well-known retained Descriptor topic for mimirheim core.
        _payload: The pre-serialised Descriptor payload, computed once at
            construction since MimirheimConfig plus MIMIRHEIM_CONFIG_FORM_SPEC
            do not change over the process lifetime.
    """

    def __init__(self, config: MimirheimConfig, paho_client: Any) -> None:
        """Construct the client and register its last-will.

        Args:
            config: Static system configuration, used only for the broker
                host/port in ``start()``.
            paho_client: An already-constructed, not-yet-connected paho
                ``Client`` instance, dedicated to the Config Service protocol.
        """
        self._client = paho_client
        self._config = config
        self._topic = descriptor_topic(OWNER_ID)
        self._payload = descriptor_payload(
            build_descriptor(OWNER_ID, DISPLAY_NAME, MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)
        )

        # Must be registered before connect(): MQTT only delivers the last-will
        # to the broker as part of the CONNECT packet. Registering it here
        # means an ungraceful disconnect (crash, network loss) clears the
        # retained Descriptor automatically, without this process's
        # involvement.
        self._client.will_set(self._topic, payload=CLEARING_PAYLOAD, qos=1, retain=True)
        self._client.on_connect = self._on_connect

    def start(self) -> None:
        """Connect to the broker and start the network loop in a background thread."""
        self._client.connect(self._config.mqtt.host, self._config.mqtt.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Clear the retained Descriptor and disconnect cleanly.

        Publishing the clearing payload before disconnecting ensures the
        Descriptor is removed from the broker even on a clean shutdown (the
        last-will only fires on an unclean disconnect).
        """
        self._client.publish(self._topic, payload=CLEARING_PAYLOAD, qos=1, retain=True)
        self._client.disconnect()
        self._client.loop_stop()

    def _on_connect(
        self, client: Any, userdata: Any, _connect_flags: Any, reason_code: Any, properties: Any
    ) -> None:
        """Called by paho when the broker connection is established or restored.

        Args:
            client: The paho client instance.
            userdata: Unused.
            _connect_flags: Connection flags from the broker (unused; part of
                the paho callback signature).
            reason_code: A ``ReasonCode`` object; ``is_failure`` is True when
                the connection was refused.
            properties: MQTT v5 properties (unused).
        """
        if reason_code.is_failure:
            logger.error("Config Service MQTT connect failed: %s", reason_code)
            return
        client.publish(self._topic, payload=self._payload, qos=1, retain=True)
