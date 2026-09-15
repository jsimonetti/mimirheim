"""MQTT wiring for mimirheim core's Awaiting Configuration Operational State.

See ``mimirheim_shared/CONTEXT.md``'s Awaiting Configuration entry and
ADR-0009/ADR-0011: while ``mimirheim.yaml`` does not yet fully validate (or
does not exist), mimirheim core has no valid ``MimirheimConfig`` to derive
business topic names, device topics, or a solve loop from. This module
therefore builds a connection from Broker Settings alone (a validated
``MqttConfig``) and serves only the Config Service protocol — Descriptor
discovery, Operational State, ``get_current_values``, ``validate_and_write``,
and ``restart_request`` — running none of mimirheim's own function.

``mimirheim.io.mqtt_client.MqttClient`` is the Operational counterpart, used
once ``mimirheim.yaml`` validates in full. The two share no code beyond
``mimirheim.io.config_service`` and ``mimirheim_shared.config_service``,
both of which are already independent of a live ``MimirheimConfig`` instance;
duplicating the small amount of Config Service wiring here keeps this class
free of every other responsibility ``MqttClient`` carries (data topics,
readiness, the solve queue).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from mimirheim.config.schema import MqttConfig
from mimirheim.io import config_service
from mimirheim_shared.config_service import CLEARING_PAYLOAD, handle_get_current_values

logger = logging.getLogger("mimirheim.mqtt")


class AwaitingConfigurationClient:
    """Serves the Config Service protocol while mimirheim.yaml is invalid or missing.

    Attributes:
        _client: The underlying paho ``Client``, already constructed from
            Broker Settings by the caller.
        _mqtt: The validated Broker Settings used to connect.
        _config_path: Path to mimirheim core's own YAML configuration file;
            the target of a successful validate_and_write.
        _detail: Human-readable summary of why the configuration does not
            currently validate, published on the Operational State topic.
        _on_restart_requested: Called after a Restart Request is
            acknowledged, so ``__main__.py`` can exit and let the container
            supervisor start a fresh process (ADR-0012).
    """

    def __init__(
        self,
        paho_client: Any,
        mqtt: MqttConfig,
        config_path: Path,
        detail: str,
        on_restart_requested: Callable[[], None],
    ) -> None:
        """Construct the client and register callbacks.

        Args:
            paho_client: An already-constructed paho ``Client`` instance,
                built from Broker Settings alone (no ``config.outputs``
                last-will: there is no valid ``MimirheimConfig`` to derive
                an availability topic from while Awaiting Configuration).
            mqtt: The validated Broker Settings to connect with.
            config_path: Path to mimirheim core's own YAML configuration
                file, the target of a successful validate_and_write request.
            detail: A human-readable summary of why the configuration does
                not currently validate, e.g. the ``str()`` of the Pydantic
                ``ValidationError``.
            on_restart_requested: Called once a Restart Request has been
                acknowledged.
        """
        self._client = paho_client
        self._mqtt = mqtt
        self._config_path = config_path
        self._detail = detail
        self._on_restart_requested = on_restart_requested

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def start(self) -> None:
        """Connect to the broker and start the network loop in a background thread."""
        self._client.connect(self._mqtt.host, self._mqtt.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Clear the Descriptor and Operational State, then disconnect cleanly.

        Neither topic has a last-will here (there is no last-will slot spent
        on an availability topic while Awaiting Configuration), so both are
        only cleared on this graceful path — matching ``MqttClient.stop()``'s
        own disclosed trade-off. See ``mimirheim_shared/docs/adr/0005``.
        """
        self._client.publish(
            config_service.TOPIC,
            payload=CLEARING_PAYLOAD,
            qos=1,
            retain=True,
        )
        self._client.publish(
            config_service.STATE_TOPIC,
            payload=CLEARING_PAYLOAD,
            qos=1,
            retain=True,
        )
        self._client.disconnect()
        self._client.loop_stop()

    # ------------------------------------------------------------------
    # Private: paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(
        self, client: Any, userdata: Any, _connect_flags: Any, reason_code: Any, properties: Any
    ) -> None:
        """Called by paho when the broker connection is established or restored.

        Subscribes to the three Config Service request topics and publishes
        the Descriptor and Awaiting Configuration state, both retained, so a
        Config Editor that connects later sees them immediately.

        Args:
            client: The paho client instance.
            userdata: Unused.
            _connect_flags: Connection flags from the broker (unused).
            reason_code: A ``ReasonCode`` object; ``is_failure`` is True when
                the connection was refused.
            properties: MQTT v5 properties (unused in v3.1.1 connections).
        """
        if reason_code.is_failure:
            logger.error("MQTT connect failed: %s", reason_code)
            return

        logger.info(
            "MQTT connected. mimirheim is awaiting configuration: %s", self._detail
        )
        client.subscribe(config_service.REQUEST_TOPIC, qos=1)
        client.subscribe(config_service.GET_CURRENT_VALUES_REQUEST_TOPIC, qos=1)
        client.subscribe(config_service.RESTART_REQUEST_TOPIC, qos=1)

        client.publish(
            config_service.TOPIC,
            payload=config_service.payload_bytes(),
            qos=1,
            retain=True,
        )
        client.publish(
            config_service.STATE_TOPIC,
            payload=config_service.awaiting_configuration_state_payload(self._detail),
            qos=1,
            retain=True,
        )

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        """Called by paho when an MQTT message arrives.

        Only the three Config Service request topics are recognised while
        Awaiting Configuration; anything else is dropped. A malformed
        request envelope cannot be correlated to a response, so it is logged
        and dropped rather than answered — the same handling
        ``mimirheim.io.mqtt_client.MqttClient`` uses for the Operational
        counterpart of each of these topics.

        Args:
            client: The paho client instance.
            userdata: Unused.
            message: A paho ``MQTTMessage`` with ``.topic`` and ``.payload``.
        """
        topic = message.topic

        if topic == config_service.REQUEST_TOPIC:
            try:
                response_payload = config_service.handle_validate_and_write(
                    message.payload, self._config_path
                )
            except Exception:
                logger.exception(
                    "Failed to handle validate_and_write request on %r.", topic
                )
                return
            client.publish(
                config_service.RESPONSE_TOPIC, payload=response_payload, qos=1, retain=False
            )
            return

        if topic == config_service.GET_CURRENT_VALUES_REQUEST_TOPIC:
            try:
                response_payload = handle_get_current_values(message.payload, self._config_path)
            except Exception:
                logger.exception(
                    "Failed to handle get_current_values request on %r.", topic
                )
                return
            client.publish(
                config_service.GET_CURRENT_VALUES_RESPONSE_TOPIC,
                payload=response_payload,
                qos=1,
                retain=False,
            )
            return

        if topic == config_service.RESTART_REQUEST_TOPIC:
            try:
                response_payload = config_service.handle_restart_request(message.payload)
            except Exception:
                logger.exception("Failed to handle restart_request on %r.", topic)
                return
            client.publish(
                config_service.RESTART_RESPONSE_TOPIC,
                payload=response_payload,
                qos=1,
                retain=False,
            )
            self._on_restart_requested()
            return

        logger.debug(
            "Received message on unrecognised topic %r while awaiting configuration; ignoring.",
            topic,
        )
