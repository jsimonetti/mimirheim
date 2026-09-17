"""Unit tests for helper_common.awaiting_configuration.run_awaiting_configuration.

Covers the Awaiting Configuration Operational State for helper daemons
(ADR-0009, ADR-0011, mirroring mimirheim core's own
``mimirheim.io.awaiting_configuration`` and its test file
``tests/unit/test_awaiting_configuration.py``): a Config Owner connected to
MQTT on Broker Settings alone, serving only the Config Service protocol with
none of the helper's own function running.

``ConfigOwnerSupport`` itself is exercised in test_config_owner.py; these
tests are about the wiring around it — client construction, connect/message
callback dispatch, and the run loop's shutdown conditions — with
``ConfigOwnerSupport`` replaced by a MagicMock so failures here point at this
module, not at the Config Service protocol implementation.
"""

from __future__ import annotations

import logging
import ssl
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from pydantic import BaseModel, ConfigDict

from helper_common.awaiting_configuration import run_awaiting_configuration
from helper_common.config import MqttConfig
from mimirheim_shared.formspec import FieldSpec, FormSpec

# Bound before any test patches threading.Event, so a real one can still be
# built for the tests that need a genuine cross-thread signal.
_RealEvent = threading.Event


class _ToyHelperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: str


_TOY_FORM_SPEC = FormSpec(fields={"area": FieldSpec(label="Area", description="Price area code.")})

_LOGGER = logging.getLogger("test-awaiting-configuration")


def _set_event() -> threading.Event:
    """A pre-set Event, so the run loop falls straight through its first check."""
    event = _RealEvent()
    event.set()
    return event


def _run(
    *,
    mqtt_config: MqttConfig | None = None,
    config_path: Path = Path("unused-config.yaml"),
    detail: str = "area: Field required",
) -> None:
    run_awaiting_configuration(
        mqtt_config=mqtt_config or MqttConfig(host="broker.local", client_id="toy-helper"),
        config_path=config_path,
        detail=detail,
        owner_id="toy-helper",
        display_name="Toy Helper",
        model_cls=_ToyHelperConfig,
        form_spec=_TOY_FORM_SPEC,
        logger=_LOGGER,
    )


def _reason(is_failure: bool) -> Any:
    reason_code = MagicMock()
    reason_code.is_failure = is_failure
    return reason_code


class TestBuildClient:
    def test_no_tls_by_default(self) -> None:
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run()

        client_cls.return_value.tls_set.assert_not_called()

    def test_tls_requires_a_valid_certificate_by_default(self) -> None:
        mqtt_config = MqttConfig(host="broker.local", tls=True)
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run(mqtt_config=mqtt_config)

        client_cls.return_value.tls_set.assert_called_once_with(cert_reqs=ssl.CERT_REQUIRED)
        client_cls.return_value.tls_insecure_set.assert_not_called()

    def test_tls_allow_insecure_skips_verification(self) -> None:
        mqtt_config = MqttConfig(host="broker.local", tls=True, tls_allow_insecure=True)
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run(mqtt_config=mqtt_config)

        client_cls.return_value.tls_set.assert_called_once_with(cert_reqs=ssl.CERT_NONE)
        client_cls.return_value.tls_insecure_set.assert_called_once_with(True)

    def test_credentials_are_set_when_a_username_is_configured(self) -> None:
        mqtt_config = MqttConfig(host="broker.local", username="u", password="p")
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run(mqtt_config=mqtt_config)

        client_cls.return_value.username_pw_set.assert_called_once_with("u", "p")


class TestConnect:
    def test_registers_last_will_before_connecting(self) -> None:
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run()

        client = client_cls.return_value
        will_set_call = next(i for i, c in enumerate(client.method_calls) if c[0] == "will_set")
        connect_call = next(i for i, c in enumerate(client.method_calls) if c[0] == "connect")
        assert will_set_call < connect_call

    def test_connects_using_broker_settings_and_starts_the_loop(self) -> None:
        mqtt_config = MqttConfig(host="broker.local", port=8883, client_id="toy-helper")
        with patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls:
            with patch("helper_common.awaiting_configuration.threading.Event", _set_event):
                _run(mqtt_config=mqtt_config)

        client = client_cls.return_value
        client.connect.assert_called_once_with("broker.local", 8883)
        client.loop_start.assert_called_once()


class TestOnConnect:
    def test_success_calls_config_owner_on_connect(self) -> None:
        with (
            patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls,
            patch("helper_common.awaiting_configuration.ConfigOwnerSupport") as owner_cls,
            patch("helper_common.awaiting_configuration.threading.Event", _set_event),
        ):
            _run()
            client = client_cls.return_value
            client.on_connect(client, None, None, _reason(False), None)

        owner_cls.return_value.on_connect.assert_called_once_with(client)

    def test_failure_does_not_call_config_owner_on_connect(self) -> None:
        with (
            patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls,
            patch("helper_common.awaiting_configuration.ConfigOwnerSupport") as owner_cls,
            patch("helper_common.awaiting_configuration.threading.Event", _set_event),
        ):
            _run()
            client = client_cls.return_value
            client.on_connect(client, None, None, _reason(True), None)

        owner_cls.return_value.on_connect.assert_not_called()


class TestOnMessage:
    def test_dispatches_to_config_owner_handle_message(self) -> None:
        with (
            patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls,
            patch("helper_common.awaiting_configuration.ConfigOwnerSupport") as owner_cls,
            patch("helper_common.awaiting_configuration.threading.Event", _set_event),
        ):
            _run()
            client = client_cls.return_value
            message = MagicMock()
            client.on_message(client, None, message)

        owner_cls.return_value.handle_message.assert_called_once_with(client, message)


class TestRun:
    def test_a_termination_signal_ends_the_loop_and_clears_state(self) -> None:
        with (
            patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls,
            patch("helper_common.awaiting_configuration.ConfigOwnerSupport") as owner_cls,
            patch("helper_common.awaiting_configuration.threading.Event", _set_event),
        ):
            _run()

        client = client_cls.return_value
        owner_cls.return_value.clear_descriptor.assert_called_once_with(client)
        client.disconnect.assert_called_once()
        client.loop_stop.assert_called_once()

    def test_a_restart_requested_event_ends_the_loop(self) -> None:
        with (
            patch("helper_common.awaiting_configuration.mqtt.Client") as client_cls,
            patch("helper_common.awaiting_configuration.ConfigOwnerSupport") as owner_cls,
            patch("helper_common.awaiting_configuration.signal.signal"),
        ):
            owner_cls.return_value.restart_requested = _RealEvent()
            owner_cls.return_value.restart_requested.set()
            _run()

        client = client_cls.return_value
        owner_cls.return_value.clear_descriptor.assert_called_once_with(client)
        client.disconnect.assert_called_once()

    def test_installs_handlers_for_sigterm_and_sigint(self) -> None:
        import signal as signal_module

        installed: list[int] = []

        def _record(signum: int, handler: Any) -> None:
            installed.append(signum)

        with (
            patch("helper_common.awaiting_configuration.mqtt.Client"),
            patch("helper_common.awaiting_configuration.signal.signal", _record),
            patch("helper_common.awaiting_configuration.threading.Event", _set_event),
        ):
            _run()

        assert installed == [signal_module.SIGTERM, signal_module.SIGINT]

    def test_a_signal_releases_the_loop(self) -> None:
        """run_awaiting_configuration blocks until the handler sets the event.

        A no-op handler would leave it waiting and this test would time out
        rather than pass.
        """
        handlers: dict[int, Any] = {}
        installed = _RealEvent()

        def _record(signum: int, handler: Any) -> None:
            handlers[signum] = handler
            if len(handlers) == 2:
                installed.set()

        finished = _RealEvent()

        def _target() -> None:
            with (
                patch("helper_common.awaiting_configuration.mqtt.Client"),
                patch("helper_common.awaiting_configuration.ConfigOwnerSupport"),
                patch("helper_common.awaiting_configuration.signal.signal", _record),
            ):
                _run()
            finished.set()

        import signal as signal_module

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        assert installed.wait(timeout=5.0), "run_awaiting_configuration never installed its handlers"
        handlers[signal_module.SIGTERM](signal_module.SIGTERM, None)
        assert finished.wait(timeout=5.0), "run_awaiting_configuration did not return after SIGTERM"
        thread.join(timeout=5.0)

        assert not thread.is_alive()
