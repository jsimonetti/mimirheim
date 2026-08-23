"""Unit tests for the MqttDaemon lifecycle that every helper inherits.

``helper_common/daemon.py`` sat at 66% coverage, and the uncovered third was
the part every helper depends on and none of them re-implements: TLS setup,
the connect and disconnect logging, ``run()``'s signal handling and shutdown,
and the HA discovery wiring in ``HelperDaemon._on_connect``.

The paho client is replaced throughout. These tests are about what the base
class does with it, not about paho.
"""
from __future__ import annotations

import logging
import signal
import ssl
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

from helper_common.config import HomeAssistantConfig, MqttConfig
from helper_common.cycle import CycleResult
from helper_common.daemon import HelperDaemon, MqttDaemon


# Bound before any test patches threading.Event, so the factories below build a
# real one instead of re-entering the patch.
_RealEvent = threading.Event


def _set_event() -> threading.Event:
    """A pre-set Event, so run() falls straight through its wait()."""
    event = _RealEvent()
    event.set()
    return event


def _config(**overrides: Any) -> SimpleNamespace:
    """A config object shaped like a helper's, with the fields the base reads."""
    mqtt_kwargs = overrides.pop("mqtt", {})
    return SimpleNamespace(
        mqtt=MqttConfig(host="broker.local", client_id="test", **mqtt_kwargs),
        trigger_topic="mimir/input/test/trigger",
        output_topic="mimir/input/test",
        **overrides,
    )


class _Daemon(HelperDaemon):
    """Minimal concrete HelperDaemon."""

    TOOL_NAME = "test_tool"

    def __init__(self, config: Any) -> None:
        self.cycles = 0
        super().__init__(config)

    def _run_cycle(self, client: mqtt.Client) -> CycleResult | None:
        self.cycles += 1
        return None


def _reason(code: int) -> Any:
    """A stand-in for a paho ReasonCode that compares equal to an int."""

    class _ReasonCode(int):
        @property
        def is_failure(self) -> bool:
            return int(self) != 0

    return _ReasonCode(code)


# ---------------------------------------------------------------------------
# Client construction
# ---------------------------------------------------------------------------


class TestBuildClient:
    def test_no_tls_by_default(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config())

        client_cls.return_value.tls_set.assert_not_called()

    def test_tls_requires_a_valid_certificate_by_default(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config(mqtt={"tls": True}))

        client_cls.return_value.tls_set.assert_called_once_with(
            cert_reqs=ssl.CERT_REQUIRED
        )
        client_cls.return_value.tls_insecure_set.assert_not_called()

    def test_tls_allow_insecure_skips_verification(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config(mqtt={"tls": True, "tls_allow_insecure": True}))

        client_cls.return_value.tls_set.assert_called_once_with(cert_reqs=ssl.CERT_NONE)
        client_cls.return_value.tls_insecure_set.assert_called_once_with(True)

    def test_tls_allow_insecure_alone_does_nothing(self) -> None:
        """The field is documented as having no effect when tls is false."""
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config(mqtt={"tls_allow_insecure": True}))

        client_cls.return_value.tls_set.assert_not_called()
        client_cls.return_value.tls_insecure_set.assert_not_called()

    def test_credentials_are_set_when_a_username_is_configured(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config(mqtt={"username": "u", "password": "p"}))

        client_cls.return_value.username_pw_set.assert_called_once_with("u", "p")

    def test_credentials_are_not_set_without_a_username(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            MqttDaemon(_config())

        client_cls.return_value.username_pw_set.assert_not_called()


# ---------------------------------------------------------------------------
# run()
# ---------------------------------------------------------------------------


class TestRun:
    def test_connects_starts_the_loop_and_shuts_down_cleanly(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            daemon = MqttDaemon(_config())
        client = client_cls.return_value

        with patch("helper_common.daemon.threading.Event", _set_event):
            daemon.run()

        client.connect.assert_called_once_with("broker.local", 1883)
        client.loop_start.assert_called_once()
        client.loop_stop.assert_called_once()
        client.disconnect.assert_called_once()

    def test_installs_handlers_for_sigterm_and_sigint(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())
        installed = []

        def _record(signum: int, handler: Any) -> None:
            installed.append(signum)

        with (
            patch("helper_common.daemon.signal.signal", _record),
            patch("helper_common.daemon.threading.Event", _set_event),
        ):
            daemon.run()

        assert installed == [signal.SIGTERM, signal.SIGINT]

    def test_the_signal_handler_releases_the_stop_event(self) -> None:
        """run() blocks until the handler sets the event, so this proves it does.

        A no-op handler would leave run() waiting and this test would time out
        rather than pass.
        """
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())
        handlers: dict[int, Any] = {}
        installed = _RealEvent()

        def _record(signum: int, handler: Any) -> None:
            handlers[signum] = handler
            if len(handlers) == 2:
                installed.set()

        finished = _RealEvent()

        def _run() -> None:
            daemon.run()
            finished.set()

        with patch("helper_common.daemon.signal.signal", _record):
            thread = threading.Thread(target=_run, daemon=True)
            thread.start()
            assert installed.wait(timeout=5.0), "run() never installed its handlers"
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert finished.wait(timeout=5.0), "run() did not return after SIGTERM"
            thread.join(timeout=5.0)

        assert not thread.is_alive()


# ---------------------------------------------------------------------------
# Connect and disconnect logging
# ---------------------------------------------------------------------------


class TestConnectLogging:
    def test_a_refused_connection_is_logged_as_an_error(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())

        with caplog.at_level(logging.ERROR):
            daemon._on_connect(MagicMock(), None, None, _reason(5), None)

        assert "MQTT connect failed" in caplog.text

    def test_a_successful_connection_names_the_broker(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())

        with caplog.at_level(logging.INFO):
            daemon._on_connect(MagicMock(), None, None, _reason(0), None)

        assert "broker.local:1883" in caplog.text

    def test_an_unexpected_disconnect_warns(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())

        with caplog.at_level(logging.WARNING):
            daemon._on_disconnect(MagicMock(), None, None, _reason(7), None)

        assert "disconnected unexpectedly" in caplog.text

    def test_a_clean_disconnect_is_silent(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Shutdown disconnects cleanly; warning there would cry wolf."""
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = MqttDaemon(_config())

        with caplog.at_level(logging.WARNING):
            daemon._on_disconnect(MagicMock(), None, None, _reason(0), None)

        assert caplog.records == []


# ---------------------------------------------------------------------------
# Stats publication
# ---------------------------------------------------------------------------


class TestPublishStats:
    def test_nothing_is_published_without_a_stats_topic(self) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            daemon = MqttDaemon(_config())

        daemon._publish_stats(datetime.now(tz=timezone.utc), 1.0, CycleResult())

        client_cls.return_value.publish.assert_not_called()


# ---------------------------------------------------------------------------
# HelperDaemon: subscriptions and discovery
# ---------------------------------------------------------------------------


class TestHelperDaemonOnConnect:
    def test_subscribes_to_the_trigger_topic_and_ha_status(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config())
        client = MagicMock()

        daemon._on_connect(client, None, None, _reason(0), None)

        subscribed = [call.args[0] for call in client.subscribe.call_args_list]
        assert subscribed == ["mimir/input/test/trigger", "homeassistant/status"]

    def test_a_refused_connection_subscribes_to_nothing(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config())
        client = MagicMock()

        daemon._on_connect(client, None, None, _reason(5), None)

        client.subscribe.assert_not_called()

    def test_discovery_is_not_published_when_absent(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config())

        with patch("helper_common.daemon.publish_trigger_discovery") as pub:
            daemon._on_connect(MagicMock(), None, None, _reason(0), None)

        pub.assert_not_called()

    def test_discovery_is_not_published_when_disabled(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config(ha_discovery=HomeAssistantConfig(enabled=False)))

        with patch("helper_common.daemon.publish_trigger_discovery") as pub:
            daemon._on_connect(MagicMock(), None, None, _reason(0), None)

        pub.assert_not_called()

    def test_discovery_is_published_when_enabled(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config(ha_discovery=HomeAssistantConfig(enabled=True)))

        with patch("helper_common.daemon.publish_trigger_discovery") as pub:
            daemon._on_connect(MagicMock(), None, None, _reason(0), None)

        pub.assert_called_once()
        assert pub.call_args.kwargs["tool_name"] == "test_tool"


class TestToolLabel:
    def test_defaults_to_a_title_cased_tool_name(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config(ha_discovery=HomeAssistantConfig(enabled=True)))

        assert daemon._tool_label() == "Test Tool"

    def test_a_configured_device_name_wins(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(
                _config(
                    ha_discovery=HomeAssistantConfig(
                        enabled=True, device_name="My Tool"
                    )
                )
            )

        assert daemon._tool_label() == "My Tool"

    def test_falls_back_to_the_tool_name_without_ha_config(self) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _Daemon(_config())

        assert daemon._tool_label() == "Test Tool"
