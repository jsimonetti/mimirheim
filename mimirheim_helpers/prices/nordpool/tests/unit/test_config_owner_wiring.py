"""Tests for NordpoolDaemon's Config Service protocol wiring.

Covers only the wiring between NordpoolDaemon and ConfigOwnerSupport (config-
editor-v3 ticket 06): ConfigOwnerSupport's own behavior is unit-tested in
mimirheim_helpers/common/tests/unit/test_config_owner.py. These tests prove
that wiring does not disturb NordpoolDaemon's existing trigger-handling
behavior (HelperDaemon's own subscribe/discovery/debounce logic), only adds
to it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    descriptor_topic,
    validate_and_write_request_topic,
)

from nordpool.__main__ import CONFIG_OWNER_ID, NordpoolDaemon
from nordpool.config import MqttConfig, NordpoolApiConfig, NordpoolConfig


def _reason(code: int):
    class _ReasonCode(int):
        @property
        def is_failure(self) -> bool:
            return int(self) != 0

    return _ReasonCode(code)


def _make_daemon(tmp_path: Path) -> NordpoolDaemon:
    config = NordpoolConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        trigger_topic="mimir/input/tools/prices/trigger",
        nordpool=NordpoolApiConfig(area="NL"),
    )
    return NordpoolDaemon(config, tmp_path / "config.yaml")


class TestLastWill:
    def test_registers_a_last_will_clearing_the_descriptor_on_construction(
        self, tmp_path: Path
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            _make_daemon(tmp_path)

        client_cls.return_value.will_set.assert_called_once_with(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )


class TestOnConnect:
    def test_publishes_the_descriptor_and_subscribes_alongside_the_trigger_topic(
        self, tmp_path: Path
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _make_daemon(tmp_path)
        client = MagicMock()

        daemon._on_connect(client, None, None, _reason(0), None)

        subscribed = [call.args[0] for call in client.subscribe.call_args_list]
        assert validate_and_write_request_topic(CONFIG_OWNER_ID) in subscribed
        assert "mimir/input/tools/prices/trigger" in subscribed
        published_topics = [call.args[0] for call in client.publish.call_args_list]
        assert descriptor_topic(CONFIG_OWNER_ID) in published_topics

    def test_a_refused_connection_does_not_publish_or_subscribe_for_config_service(
        self, tmp_path: Path
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _make_daemon(tmp_path)
        client = MagicMock()

        daemon._on_connect(client, None, None, _reason(5), None)

        client.subscribe.assert_not_called()
        client.publish.assert_not_called()


class TestOnMessage:
    def test_config_service_requests_are_handled_and_do_not_reach_the_trigger_logic(
        self, tmp_path: Path
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _make_daemon(tmp_path)
        message = SimpleNamespace(
            topic=validate_and_write_request_topic(CONFIG_OWNER_ID),
            payload=b"not json",
            retain=False,
        )

        with patch.object(daemon, "_run_cycle") as run_cycle:
            daemon._on_message(MagicMock(), None, message)

        run_cycle.assert_not_called()

    def test_other_messages_still_reach_the_normal_dispatch(self, tmp_path: Path) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _make_daemon(tmp_path)
        message = SimpleNamespace(topic="homeassistant/status", payload=b"offline", retain=True)

        # Should not raise, and should fall through to HelperDaemon's own
        # dispatch (which ignores an "offline" HA status message).
        daemon._on_message(MagicMock(), None, message)


class TestOnShutdown:
    def test_clears_the_retained_descriptor(self, tmp_path: Path) -> None:
        with patch("helper_common.daemon.mqtt.Client"):
            daemon = _make_daemon(tmp_path)
        client = MagicMock()
        daemon._client = client

        daemon._on_shutdown()

        client.publish.assert_called_once_with(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
