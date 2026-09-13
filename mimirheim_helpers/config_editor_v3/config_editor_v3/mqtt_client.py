"""MQTT wiring for Descriptor discovery, validate_and_write, and get_current_values.

Subscribes to the well-known Config Service Descriptor wildcard topic
(`mimirheim_shared.config_service.descriptor_topic("+")`) and updates
`registry.py`'s `ConfigOwnerRegistry` as Descriptors are published or cleared
(see `mimirheim_shared/docs/adr/0001`). Also publishes `validate_and_write`
and `get_current_values` requests on behalf of the HTTP server (`server.py`)
and blocks the calling thread until the matching response arrives, so the
Config Editor's own MQTT handling stays confined to this one module:
rendering never touches MQTT itself, it only calls `submit_validate_and_write`
and `get_current_values`.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from typing import Any

import paho.mqtt.client as mqtt
from helper_common.daemon import MqttDaemon
from mimirheim_shared.config_service import (
    Descriptor,
    GetCurrentValuesRequest,
    GetCurrentValuesResult,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from pydantic import ValidationError

from config_editor_v3.registry import ConfigOwnerRegistry

logger = logging.getLogger(__name__)

# Matches every Config Owner's retained Descriptor topic, regardless of owner_id.
_DESCRIPTOR_TOPIC_WILDCARD = descriptor_topic("+")

# Matches every Config Owner's validate_and_write response topic, regardless
# of owner_id; a specific response is correlated to its request by
# ValidateAndWriteResult.request_id, not by owner_id alone (see
# submit_validate_and_write).
_VALIDATE_AND_WRITE_RESPONSE_WILDCARD = validate_and_write_response_topic("+")

# Matches every Config Owner's get_current_values response topic, regardless
# of owner_id; correlated by GetCurrentValuesResult.request_id, same as above.
_GET_CURRENT_VALUES_RESPONSE_WILDCARD = get_current_values_response_topic("+")

# Index of the owner_id segment in "mimirheim/config-service/<owner_id>/descriptor".
_OWNER_ID_TOPIC_INDEX = 2


@dataclass
class _PendingRequest:
    """Bookkeeping for one in-flight validate_and_write or get_current_values request.

    `result` is filled in and `ready` set by `_handle_validate_and_write_response`
    or `_handle_get_current_values_response` (on the paho network thread);
    `submit_validate_and_write`/`get_current_values` (on an HTTP
    request-handling thread) blocks on `ready` and then reads `result`.
    Bundled into one object, rather than two dicts keyed by the same
    request_id, so the two can never drift out of sync. Request IDs are
    freshly generated UUIDs regardless of request kind, so both kinds share
    one pending pool without risk of collision.
    """

    ready: threading.Event
    result: ValidateAndWriteResult | GetCurrentValuesResult | None = None


class ConfigEditorMqttClient(MqttDaemon):
    """Subscribes to every Config Owner's Descriptor topic and updates a registry.

    Also publishes validate_and_write requests on behalf of the HTTP server
    (`server.py`) and blocks the calling thread until the matching response
    arrives (see `submit_validate_and_write`), so the Config Editor's own
    MQTT handling stays confined to this one module.

    Args:
        config: Validated configuration with a `.mqtt` attribute (see
            `helper_common.config.MqttConfig`), per `MqttDaemon`'s own contract.
        registry: The registry to update as Descriptors arrive or are cleared.
    """

    def __init__(self, config: Any, registry: ConfigOwnerRegistry) -> None:
        self._registry = registry
        # Guards _pending. Written from HTTP request-handling threads
        # (submit_validate_and_write registers a pending request and later
        # reads its result) and from the paho network thread (_on_message
        # delivers the result and sets the matching event); a single lock
        # keeps a request's registration and its delivery from racing.
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
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
        client.subscribe(_VALIDATE_AND_WRITE_RESPONSE_WILDCARD, qos=1)
        client.subscribe(_GET_CURRENT_VALUES_RESPONSE_WILDCARD, qos=1)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        """Dispatches an incoming message to the Descriptor or validate_and_write handler.

        Both Descriptor and validate_and_write response topics share the
        `mimirheim/config-service/<owner_id>/...` prefix and owner_id
        position, so the owner_id parsed from the topic is used to compute
        the exact validate_and_write response topic that owner_id would use
        and compare against it, rather than matching a separately
        maintained literal suffix.

        Args:
            client: The paho client that received the message. Unused.
            userdata: Unused; part of the paho callback signature.
            message: The paho `MQTTMessage`.
        """
        owner_id = message.topic.split("/")[_OWNER_ID_TOPIC_INDEX]
        if message.topic == validate_and_write_response_topic(owner_id):
            self._handle_validate_and_write_response(message)
            return
        if message.topic == get_current_values_response_topic(owner_id):
            self._handle_get_current_values_response(message)
            return
        self._handle_descriptor_message(message)

    def _handle_descriptor_message(self, message: Any) -> None:
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

    def _handle_validate_and_write_response(self, message: Any) -> None:
        """Delivers a validate_and_write result to whichever caller is awaiting it.

        A payload that fails to parse as JSON or fails
        `ValidateAndWriteResult` validation is logged and discarded, for the
        same reason as an unparseable Descriptor (see
        `_handle_descriptor_message`). A well-formed result whose
        `request_id` matches no pending request (already timed out, or a
        response to a request this process never made) is silently ignored:
        there is no caller left to deliver it to.

        Args:
            message: The paho `MQTTMessage`.
        """
        try:
            result = ValidateAndWriteResult.model_validate_json(message.payload)
        except (ValidationError, json.JSONDecodeError):
            logger.exception("Discarding malformed validate_and_write result on %r.", message.topic)
            return

        with self._pending_lock:
            pending = self._pending.get(result.request_id)
            if pending is None:
                logger.debug(
                    "Discarding validate_and_write result for unknown or timed-out request_id %r.",
                    result.request_id,
                )
                return
            pending.result = result
            pending.ready.set()

    def _handle_get_current_values_response(self, message: Any) -> None:
        """Delivers a get_current_values result to whichever caller is awaiting it.

        Mirrors `_handle_validate_and_write_response` exactly, for the
        get_current_values request/response pair instead.

        Args:
            message: The paho `MQTTMessage`.
        """
        try:
            result = GetCurrentValuesResult.model_validate_json(message.payload)
        except (ValidationError, json.JSONDecodeError):
            logger.exception("Discarding malformed get_current_values result on %r.", message.topic)
            return

        with self._pending_lock:
            pending = self._pending.get(result.request_id)
            if pending is None:
                logger.debug(
                    "Discarding get_current_values result for unknown or timed-out request_id %r.",
                    result.request_id,
                )
                return
            pending.result = result
            pending.ready.set()

    def submit_validate_and_write(
        self, owner_id: str, values: dict[str, Any], timeout: float = 10.0
    ) -> ValidateAndWriteResult:
        """Publishes a validate_and_write request and blocks until the Config Owner replies.

        Called from an HTTP request-handling thread (`server.py`), not the
        paho network thread: this method blocks the calling thread on a
        `threading.Event` that `_handle_validate_and_write_response` sets
        from the network thread when the matching response arrives,
        correlated by a freshly generated `request_id` (see
        `mimirheim_shared.config_service`'s module docstring for why
        `request_id` rather than a native MQTT request/response feature).

        Args:
            owner_id: The Config Owner's stable identifier.
            values: Candidate Values to validate and, on success, write.
            timeout: Seconds to wait for a response before giving up.

        Returns:
            The Config Owner's `ValidateAndWriteResult`.

        Raises:
            TimeoutError: If no response arrives within `timeout` seconds.
        """
        request_id = str(uuid.uuid4())
        pending = _PendingRequest(ready=threading.Event())
        with self._pending_lock:
            self._pending[request_id] = pending

        try:
            request = ValidateAndWriteRequest(request_id=request_id, values=values)
            self._client.publish(
                validate_and_write_request_topic(owner_id),
                request.model_dump_json().encode("utf-8"),
                qos=1,
            )
            if not pending.ready.wait(timeout):
                raise TimeoutError(
                    f"Timed out waiting for a validate_and_write response from {owner_id!r}."
                )
            assert pending.result is not None  # ready implies _handle_validate_and_write_response set it
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def get_current_values(self, owner_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Publishes a get_current_values request and blocks until the Config Owner replies.

        Mirrors `submit_validate_and_write` exactly, for the
        get_current_values request/response pair instead.

        Args:
            owner_id: The Config Owner's stable identifier.
            timeout: Seconds to wait for a response before giving up.

        Returns:
            The Config Owner's current configuration values.

        Raises:
            TimeoutError: If no response arrives within `timeout` seconds.
        """
        request_id = str(uuid.uuid4())
        pending = _PendingRequest(ready=threading.Event())
        with self._pending_lock:
            self._pending[request_id] = pending

        try:
            request = GetCurrentValuesRequest(request_id=request_id)
            self._client.publish(
                get_current_values_request_topic(owner_id),
                request.model_dump_json().encode("utf-8"),
                qos=1,
            )
            if not pending.ready.wait(timeout):
                raise TimeoutError(
                    f"Timed out waiting for a get_current_values response from {owner_id!r}."
                )
            assert pending.result is not None  # ready implies _handle_get_current_values_response set it
            return pending.result.values
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
