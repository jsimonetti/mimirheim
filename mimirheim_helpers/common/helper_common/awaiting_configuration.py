"""Generic Awaiting Configuration MQTT loop, shared by every helper daemon.

When ``helper_common.config.load_helper_config`` cannot validate a helper's
full configuration (the file is missing, unreadable, not valid YAML, or fails
the helper's own Pydantic model) but the ``mqtt:`` Broker Settings section
*does* validate, the helper is not able to run its own function, but it is
still able to connect to MQTT. ``run_awaiting_configuration`` is what it runs
instead: a minimal MQTT client that serves only the Config Service protocol
(Descriptor, get_current_values, validate_and_write, restart_request) via
``helper_common.config_owner.ConfigOwnerSupport``, so the Config Editor can
still discover the helper, show it as awaiting configuration with a reason,
and fix its configuration file.

This is the helper-daemon counterpart of ``mimirheim.io.awaiting_configuration``
in mimirheim core. See ADR-0009 and ADR-0011 in
``.scratch/config-owner-startup-resilience/spec.md``.

This module has no imports from any specific helper tool.
"""

from __future__ import annotations

import logging
import signal
import ssl
import threading
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from pydantic import BaseModel

from helper_common.config import MqttConfig
from helper_common.config_owner import ConfigOwnerSupport
from mimirheim_shared.formspec import FormSpec

# How often the run loop checks the ConfigOwnerSupport's restart_requested
# event while waiting for a Restart Request or termination signal. Mirrors
# helper_common.daemon's own polling interval for the same reason: a Restart
# Request arrives over MQTT, not as an OS signal, so there is nothing to
# interrupt a plain wait() with.
_RESTART_POLL_INTERVAL_S = 0.5


def _build_paho_client(mqtt_config: MqttConfig) -> mqtt.Client:
    """Build a bare paho client from Broker Settings, with TLS and credentials wired.

    Mirrors ``helper_common.daemon.MqttDaemon._build_client``; duplicated
    rather than shared because that method also wires HA discovery and
    trigger-topic subscriptions this minimal client must not have.
    """
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=mqtt_config.client_id)
    if mqtt_config.tls:
        cert_reqs = ssl.CERT_NONE if mqtt_config.tls_allow_insecure else ssl.CERT_REQUIRED
        client.tls_set(cert_reqs=cert_reqs)
        if mqtt_config.tls_allow_insecure:
            client.tls_insecure_set(True)
    if mqtt_config.username is not None:
        client.username_pw_set(mqtt_config.username, mqtt_config.password)
    return client


def run_awaiting_configuration(
    mqtt_config: MqttConfig,
    config_path: Path,
    detail: str,
    owner_id: str,
    display_name: str,
    model_cls: type[BaseModel],
    form_spec: FormSpec,
    logger: logging.Logger,
) -> None:
    """Serve only the Config Service protocol until a Restart Request or termination signal.

    Blocks the calling thread. Connects to the broker named by ``mqtt_config``,
    publishes a Descriptor and an Awaiting Configuration Operational State
    (carrying ``detail``), and answers get_current_values and
    validate_and_write requests against ``config_path`` the same way a fully
    running helper would. Returns once a Restart Request is acknowledged or
    the process receives SIGTERM/SIGINT, after clearing the Descriptor and
    Operational State and disconnecting cleanly.

    The caller (``helper_common.config.load_helper_config``) always exits the
    process immediately after this function returns; restarting is how the
    operator's corrected configuration takes effect.

    Args:
        mqtt_config: Already-validated Broker Settings to connect with.
        config_path: Path to the helper's own YAML configuration file, the
            target of a successful validate_and_write.
        detail: Human-readable reason the full configuration did not
            validate, published on the Operational State topic.
        owner_id: This helper's stable Config Owner identifier.
        display_name: Human-readable name shown by the Config Editor.
        model_cls: The helper's Pydantic config model, used to validate a
            Candidate Values payload and to build the Descriptor's schema.
        form_spec: The FormSpec paired with ``model_cls``.
        logger: The calling helper's logger.
    """
    config_owner = ConfigOwnerSupport(
        owner_id=owner_id,
        display_name=display_name,
        model=model_cls,
        form_spec=form_spec,
        config_path=config_path,
        awaiting_configuration_detail=detail,
    )
    client = _build_paho_client(mqtt_config)
    config_owner.register_last_will(client)

    def _on_connect(
        cl: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any
    ) -> None:
        if reason_code.is_failure:
            logger.error("MQTT connect failed: %s", reason_code)
            return
        logger.warning(
            "%s (%s) is awaiting configuration: %s", display_name, owner_id, detail
        )
        config_owner.on_connect(cl)

    def _on_message(cl: mqtt.Client, userdata: Any, message: Any) -> None:
        config_owner.handle_message(cl, message)

    client.on_connect = _on_connect
    client.on_message = _on_message

    stop_event = threading.Event()

    def _request_shutdown(signum: int, frame: object) -> None:
        logger.info("Signal %d received; shutting down.", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _request_shutdown)
    signal.signal(signal.SIGINT, _request_shutdown)

    client.connect(mqtt_config.host, mqtt_config.port)
    client.loop_start()

    while not stop_event.is_set():
        if config_owner.restart_requested.is_set():
            logger.info("Restart requested; shutting down.")
            stop_event.set()
            break
        stop_event.wait(timeout=_RESTART_POLL_INTERVAL_S)

    config_owner.clear_descriptor(client)
    client.disconnect()
    client.loop_stop()
