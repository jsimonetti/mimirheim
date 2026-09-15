"""Thin wrapper letting a helper daemon opt into the Config Service protocol.

A helper that wants its configuration editable in the same running Config
Editor as mimirheim core constructs one ``ConfigOwnerSupport`` at startup,
alongside its own model and FormSpec, and wires its four methods into its
existing ``MqttDaemon``/``HelperDaemon`` callbacks:

- ``register_last_will``: call once, right after the daemon's own paho
  client is built and before it connects.
- ``on_connect``: call from the daemon's own ``_on_connect``, after
  confirming the connection succeeded.
- ``handle_message``: call from the daemon's own ``_on_message``, before its
  own topic dispatch. Returns True if the message was one of this Config
  Owner's own requests — ``validate_and_write``, ``get_current_values``, or
  ``restart_request`` (handled, regardless of outcome) — so the caller
  knows whether to fall through to its own handling.
- ``clear_descriptor``: call from the daemon's own ``_on_shutdown`` (see
  ``helper_common.daemon.MqttDaemon._on_shutdown``), before the connection
  is closed. Also clears the Operational State topic.

``on_connect`` also publishes this Config Owner's Operational State
(``operational``, or ``awaiting_configuration`` with a detail if
constructed with ``awaiting_configuration_detail`` set), and a Restart
Request received via ``handle_message`` is acknowledged and recorded on the
public ``restart_requested`` event — see ``mimirheim_shared/CONTEXT.md``'s
Operational State and Restart Request entries and ADR-0010/ADR-0012. Acting
on a Restart Request (actually stopping and exiting) is the owning daemon's
own responsibility; ``helper_common.daemon.MqttDaemon.run()`` polls
``restart_requested`` for every daemon that stores this object as
``self._config_owner``.

This is built entirely on ``mimirheim_shared`` (topic naming, the
Descriptor/request/result shapes, and the generic
``handle_validate_and_write`` validate-then-write sequence), which itself
never touches an MQTT client (see ``mimirheim_shared/docs/adr/0005``). This
module is a helper's equivalent of what ``mimirheim.io.config_service`` and
``mimirheim.io.mqtt_client`` do together for mimirheim core.

Unlike mimirheim core, whose one last-will slot is already spent on
``config.outputs.availability`` (see ``mimirheim_shared/docs/adr/0005``), a
helper daemon typically registers no last-will of its own. This lets
``ConfigOwnerSupport`` give the Descriptor a real, crash-safe native
last-will here, rather than the graceful-shutdown-only fallback core uses.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    OperationalState,
    RestartRequest,
    RestartResponse,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    handle_get_current_values,
    handle_validate_and_write,
    operational_state_payload,
    restart_request_topic,
    restart_response_payload,
    restart_response_topic,
    state_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from mimirheim_shared.formspec import FormSpec

logger = logging.getLogger(__name__)


class ConfigOwnerSupport:
    """Wires one helper's MQTT connection into the Config Service protocol.

    Attributes:
        owner_id: The Config Owner's stable identifier, e.g. ``"nordpool"``.
    """

    def __init__(
        self,
        owner_id: str,
        display_name: str,
        model: type[BaseModel],
        form_spec: FormSpec,
        config_path: Path,
        awaiting_configuration_detail: str | None = None,
    ) -> None:
        """Build the Descriptor and precompute this Config Owner's topics.

        Args:
            owner_id: The Config Owner's stable identifier. Must be stable
                across restarts and configuration changes: the Config Editor
                keys its registry by this value.
            display_name: Human-readable name shown by the Config Editor.
            model: The helper's own validation model.
            form_spec: The FormSpec paired with ``model``. Not checked for
                alignment here; the helper's own test suite calls
                ``mimirheim_shared.alignment.assert_form_spec_complete``
                for that.
            config_path: Path to the helper's own YAML configuration file,
                the one it was started with and the target of a successful
                ``validate_and_write``.
            awaiting_configuration_detail: When set, this Config Owner is in
                the Awaiting Configuration Operational State (its own
                configuration does not currently validate, or does not
                exist); this is the human-readable reason, published on the
                Operational State topic instead of ``"operational"``. See
                ``mimirheim_shared/CONTEXT.md``'s Awaiting Configuration
                entry and ADR-0009/ADR-0011. ``None`` (the default) means
                this Config Owner is running its own function normally.
        """
        self.owner_id = owner_id
        self._model = model
        self._config_path = config_path
        self._awaiting_configuration_detail = awaiting_configuration_detail
        self._descriptor_topic = descriptor_topic(owner_id)
        self._request_topic = validate_and_write_request_topic(owner_id)
        self._response_topic = validate_and_write_response_topic(owner_id)
        self._get_current_values_request_topic = get_current_values_request_topic(owner_id)
        self._get_current_values_response_topic = get_current_values_response_topic(owner_id)
        self._state_topic = state_topic(owner_id)
        self._restart_request_topic = restart_request_topic(owner_id)
        self._restart_response_topic = restart_response_topic(owner_id)
        self._descriptor_payload = descriptor_payload(
            build_descriptor(owner_id, display_name, model, form_spec)
        )
        # Set by handle_message once a Restart Request has been acknowledged
        # (ADR-0012). The owning daemon's run loop (helper_common.daemon.
        # MqttDaemon.run, or a manually-driven daemon's own loop) waits on
        # this alongside its own shutdown-signal event so a Restart Request
        # ends the process the same way SIGTERM does.
        self.restart_requested = threading.Event()

    def register_last_will(self, client: Any) -> None:
        """Register a crash-safe last-will that clears the retained Descriptor.

        Must be called before ``client.connect(...)``; paho only accepts a
        last-will on a not-yet-connected client.

        Args:
            client: The helper's own paho client, not yet connected.
        """
        client.will_set(self._descriptor_topic, payload=CLEARING_PAYLOAD, qos=1, retain=True)

    def on_connect(self, client: Any) -> None:
        """Subscribe for Config Service requests and publish the Descriptor and Operational State.

        Call from the helper's own ``_on_connect``, after confirming the
        connection succeeded (a refused connection has nothing to subscribe
        or publish to).

        Args:
            client: The connected paho client.
        """
        client.subscribe(self._request_topic, qos=1)
        client.subscribe(self._get_current_values_request_topic, qos=1)
        client.subscribe(self._restart_request_topic, qos=1)
        client.publish(
            self._descriptor_topic, payload=self._descriptor_payload, qos=1, retain=True
        )
        client.publish(
            self._state_topic, payload=self._state_payload(), qos=1, retain=True
        )

    def handle_message(self, client: Any, message: Any) -> bool:
        """Handle ``message`` if it is one of this Config Owner's own request topics.

        Call from the helper's own ``_on_message`` before its own topic
        dispatch. Recognises validate_and_write, get_current_values, and
        restart_request requests.

        Args:
            client: The connected paho client, used to publish the result.
            message: The paho ``MQTTMessage`` under consideration.

        Returns:
            True if ``message.topic`` was one of this Config Owner's request
            topics (handled, regardless of outcome), so the caller should
            not also try its own dispatch. False otherwise.
        """
        if message.topic == self._restart_request_topic:
            self._handle_restart_request(client, message)
            return True

        if message.topic == self._get_current_values_request_topic:
            try:
                response_payload = handle_get_current_values(message.payload, self._config_path)
            except Exception:
                # Deliberately broad, same reason as the validate_and_write
                # handler below: this runs on the paho network thread, and a
                # malformed request envelope has no request_id to reply with.
                logger.exception(
                    "Failed to handle get_current_values request for %r on %r.",
                    self.owner_id,
                    message.topic,
                )
                return True
            client.publish(
                self._get_current_values_response_topic, payload=response_payload, qos=1, retain=False
            )
            return True

        if message.topic != self._request_topic:
            return False
        try:
            response_payload = handle_validate_and_write(
                message.payload, self._config_path, self._model
            )
        except Exception:
            # Deliberately broad: this runs on the paho network thread, where
            # an escaping exception would take down MQTT message handling
            # entirely. A malformed request envelope has no request_id to
            # reply with, so it is logged and dropped rather than answered; a
            # well-formed envelope carrying invalid Candidate Values is not an
            # exception here at all — handle_validate_and_write reports that
            # as a published failure result instead.
            logger.exception(
                "Failed to handle validate_and_write request for %r on %r.",
                self.owner_id,
                message.topic,
            )
            return True
        client.publish(self._response_topic, payload=response_payload, qos=1, retain=False)
        return True

    def _handle_restart_request(self, client: Any, message: Any) -> None:
        """Acknowledge a Restart Request and record that one arrived.

        Acting on the request (clearing the Descriptor and Operational
        State, disconnecting, exiting) is orchestration only the owning
        daemon's run loop can perform (ADR-0012); this only publishes the
        acknowledgement and sets ``restart_requested`` for that loop to
        notice.

        Args:
            client: The connected paho client, used to publish the ack.
            message: The paho ``MQTTMessage`` received on the restart
                request topic.
        """
        try:
            request = RestartRequest.model_validate_json(message.payload)
        except Exception:
            # Deliberately broad, same reason as validate_and_write's own
            # handler: a malformed request envelope has no request_id to
            # reply with, so it is logged and dropped rather than answered.
            logger.exception(
                "Failed to handle restart_request for %r on %r.",
                self.owner_id,
                message.topic,
            )
            return
        client.publish(
            self._restart_response_topic,
            payload=restart_response_payload(RestartResponse(request_id=request.request_id)),
            qos=1,
            retain=False,
        )
        self.restart_requested.set()

    def _state_payload(self) -> bytes:
        """Serialise this Config Owner's current Operational State.

        Returns:
            Awaiting Configuration (with its detail) if constructed with
            ``awaiting_configuration_detail`` set, Operational otherwise.
        """
        if self._awaiting_configuration_detail is not None:
            state = OperationalState(
                state="awaiting_configuration", detail=self._awaiting_configuration_detail
            )
        else:
            state = OperationalState(state="operational")
        return operational_state_payload(state)

    def clear_descriptor(self, client: Any) -> None:
        """Explicitly clear the retained Descriptor and Operational State on a graceful shutdown.

        The native last-will registered by ``register_last_will`` only fires
        on an ungraceful disconnect: a clean shutdown sends MQTT's own
        DISCONNECT packet first, which suppresses the will. Call this before
        the helper's own ``client.disconnect()`` so the publish has a chance
        to reach the broker.

        Args:
            client: The still-connected paho client.
        """
        client.publish(self._descriptor_topic, payload=CLEARING_PAYLOAD, qos=1, retain=True)
        client.publish(self._state_topic, payload=CLEARING_PAYLOAD, qos=1, retain=True)
