"""MQTT wiring for Descriptor discovery, validate_and_write, get_current_values, and restart_request.

Subscribes to the well-known Config Service Descriptor wildcard topic
(`mimirheim_shared.config_service.descriptor_topic("+")`) and updates
`registry.py`'s `ConfigOwnerRegistry` as Descriptors are published or cleared
(see `mimirheim_shared/docs/adr/0001`). Also publishes `validate_and_write`,
`get_current_values`, and `restart_request` requests on behalf of the HTTP
server (`server.py`) and blocks the calling thread until the matching
response arrives, so the Config Editor's own MQTT handling stays confined to
this one module: rendering never touches MQTT itself, it only calls
`submit_validate_and_write`, `get_current_values`, and
`submit_restart_request`.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from helper_common.config_owner import ConfigOwnerSupport
from helper_common.daemon import MqttDaemon
from mimirheim_shared.config_service import (
    Descriptor,
    GetCurrentValuesRequest,
    GetCurrentValuesResult,
    RestartRequest,
    RestartResponse,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    restart_request_topic,
    restart_response_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from pydantic import ValidationError

from config_editor.config import CONFIG_OWNER_DISPLAY_NAME, CONFIG_OWNER_ID, ConfigEditorConfig
from config_editor.formspec import CONFIG_EDITOR_CONFIG_FORM_SPEC
from config_editor.registry import ConfigOwnerRegistry

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

# Matches every Config Owner's restart_request response topic, regardless of
# owner_id; correlated by RestartResponse.request_id, same as above.
_RESTART_RESPONSE_WILDCARD = restart_response_topic("+")

# Index of the owner_id segment in "mimirheim/config-service/<owner_id>/descriptor".
_OWNER_ID_TOPIC_INDEX = 2


@dataclass
class _PendingRequest:
    """Bookkeeping for one in-flight validate_and_write, get_current_values, or restart_request.

    `result` is filled in and `ready` set by `_handle_validate_and_write_response`,
    `_handle_get_current_values_response`, or `_handle_restart_response` (on
    the paho network thread); `submit_validate_and_write`/`get_current_values`/
    `submit_restart_request` (on an HTTP request-handling thread) blocks on
    `ready` and then reads `result`. Bundled into one object, rather than one
    dict per request kind keyed by the same request_id, so they can never
    drift out of sync. Request IDs are freshly generated UUIDs regardless of
    request kind, so every kind shares one pending pool without risk of
    collision.
    """

    ready: threading.Event
    result: ValidateAndWriteResult | GetCurrentValuesResult | RestartResponse | None = None


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
        config_path: Path to the Config Editor's own YAML configuration file,
            the one it was started with and the target of a successful
            validate_and_write against its own owner_id (see
            `helper_common.config_owner.ConfigOwnerSupport`; the Config
            Editor is a Config Owner of its own configuration the same way
            every other helper is).
    """

    def __init__(self, config: Any, registry: ConfigOwnerRegistry, config_path: Path) -> None:
        self._registry = registry
        # Guards _pending. Written from HTTP request-handling threads
        # (submit_validate_and_write registers a pending request and later
        # reads its result) and from the paho network thread (_on_message
        # delivers the result and sets the matching event); a single lock
        # keeps a request's registration and its delivery from racing.
        self._pending_lock = threading.Lock()
        self._pending: dict[str, _PendingRequest] = {}
        super().__init__(config)
        self._config_owner = ConfigOwnerSupport(
            owner_id=CONFIG_OWNER_ID,
            display_name=CONFIG_OWNER_DISPLAY_NAME,
            model=ConfigEditorConfig,
            form_spec=CONFIG_EDITOR_CONFIG_FORM_SPEC,
            config_path=config_path,
        )
        # Must be registered before self._client.connect() (called by start()).
        self._config_owner.register_last_will(self._client)

    @property
    def restart_requested(self) -> threading.Event:
        """Set once a Restart Request for this process's own owner_id is acknowledged.

        `config_editor.__main__.main` polls this alongside its own
        stop-signal event (ADR-0012), the same way
        `helper_common.daemon.MqttDaemon.run` polls a subclass's
        `self._config_owner.restart_requested` -- this process drives its
        own run loop instead of using `MqttDaemon.run()`, so that polling
        happens in `__main__.py` rather than here.
        """
        return self._config_owner.restart_requested

    def start(self) -> None:
        """Connects to the broker and starts the network loop in a background thread."""
        cfg = self._config.mqtt
        self._client.connect(cfg.host, cfg.port)
        self._client.loop_start()

    def stop(self) -> None:
        """Clears this owner's retained state and disconnects cleanly."""
        self._config_owner.clear_descriptor(self._client)
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
        client.subscribe(_RESTART_RESPONSE_WILDCARD, qos=1)
        self._config_owner.on_connect(client)

    def _on_message(self, client: mqtt.Client, userdata: Any, message: Any) -> None:
        """Dispatches an incoming message to the Descriptor or validate_and_write handler.

        Checks this process's own Config Owner requests first (a request the
        Config Editor's own Config Service Descriptor advertises, e.g. its
        own `validate_and_write` or `restart_request` topic): those topics
        do not fit the Descriptor/response dispatch below and would
        otherwise be discarded as malformed Descriptors.

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
        if self._config_owner.handle_message(client, message):
            return
        owner_id = message.topic.split("/")[_OWNER_ID_TOPIC_INDEX]
        if message.topic == validate_and_write_response_topic(owner_id):
            self._handle_validate_and_write_response(message)
            return
        if message.topic == get_current_values_response_topic(owner_id):
            self._handle_get_current_values_response(message)
            return
        if message.topic == restart_response_topic(owner_id):
            self._handle_restart_response(message)
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

    def _handle_restart_response(self, message: Any) -> None:
        """Delivers a restart_request response to whichever caller is awaiting it.

        Mirrors `_handle_validate_and_write_response` exactly, for the
        restart_request/response pair instead.

        Args:
            message: The paho `MQTTMessage`.
        """
        try:
            result = RestartResponse.model_validate_json(message.payload)
        except (ValidationError, json.JSONDecodeError):
            logger.exception("Discarding malformed restart_request response on %r.", message.topic)
            return

        with self._pending_lock:
            pending = self._pending.get(result.request_id)
            if pending is None:
                logger.debug(
                    "Discarding restart_request response for unknown or timed-out request_id %r.",
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

    def submit_restart_request(self, owner_id: str, timeout: float = 10.0) -> RestartResponse:
        """Publishes a restart_request and blocks until the Config Owner acknowledges it.

        Mirrors `submit_validate_and_write` exactly, for the restart_request/
        response pair instead (ADR-0012): the Config Owner acknowledges, then
        exits, relying on its container supervisor to start a fresh process.
        Restarting is deliberately never triggered automatically by a
        successful `submit_validate_and_write` (a Save); it is only ever
        published in response to an explicit call to this method (`server.py`
        wires this to the owner page's Restart button, behind a confirmation),
        so that restarting a Config Owner's process stays a conscious,
        separate decision from saving its configuration.

        Args:
            owner_id: The Config Owner's stable identifier.
            timeout: Seconds to wait for an acknowledgement before giving up.

        Returns:
            The Config Owner's `RestartResponse`.

        Raises:
            TimeoutError: If no response arrives within `timeout` seconds.
        """
        request_id = str(uuid.uuid4())
        pending = _PendingRequest(ready=threading.Event())
        with self._pending_lock:
            self._pending[request_id] = pending

        try:
            request = RestartRequest(request_id=request_id)
            self._client.publish(
                restart_request_topic(owner_id),
                request.model_dump_json().encode("utf-8"),
                qos=1,
            )
            if not pending.ready.wait(timeout):
                raise TimeoutError(
                    f"Timed out waiting for a restart acknowledgement from {owner_id!r}."
                )
            assert pending.result is not None  # ready implies _handle_restart_response set it
            return pending.result
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)
