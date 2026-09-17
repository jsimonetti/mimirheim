"""Tests for ConfigEditorMqttClient's own Config Service protocol wiring.

Covers only the wiring between ConfigEditorMqttClient and ConfigOwnerSupport
(config-owner-startup-resilience ticket 03): the Config Editor becomes a
Config Owner of its own configuration the same way every other helper is a
Config Owner of its own configuration. ConfigOwnerSupport's own behavior is
unit-tested in mimirheim_helpers/common/tests/unit/test_config_owner.py.
These tests prove that wiring does not disturb ConfigEditorMqttClient's
existing Descriptor-discovery behavior (see test_mqtt_client.py), only adds
to it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from helper_common.config import MqttConfig
from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    descriptor_topic,
    get_current_values_request_topic,
    restart_request_topic,
    state_topic,
    validate_and_write_request_topic,
)

from config_editor.config import CONFIG_OWNER_ID
from config_editor.mqtt_client import ConfigEditorMqttClient
from config_editor.registry import ConfigOwnerRegistry


def _reason(code: int):
    class _ReasonCode(int):
        @property
        def is_failure(self) -> bool:
            return int(self) != 0

    return _ReasonCode(code)


def _make_config() -> object:
    class _Config:
        mqtt = MqttConfig(host="localhost", client_id="test-config-editor")

    return _Config()


def _make_client(tmp_path: Path) -> ConfigEditorMqttClient:
    with patch("helper_common.daemon.mqtt.Client"):
        return ConfigEditorMqttClient(
            _make_config(), ConfigOwnerRegistry(), tmp_path / "config-editor.yaml"
        )


class TestLastWill:
    def test_registers_a_last_will_clearing_the_descriptor_on_construction(
        self, tmp_path: Path
    ) -> None:
        with patch("helper_common.daemon.mqtt.Client") as client_cls:
            ConfigEditorMqttClient(
                _make_config(), ConfigOwnerRegistry(), tmp_path / "config-editor.yaml"
            )

        client_cls.return_value.will_set.assert_called_once_with(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )


class TestOnConnect:
    def test_publishes_the_descriptor_and_subscribes_alongside_discovery_topics(
        self, tmp_path: Path
    ) -> None:
        client_wrapper = _make_client(tmp_path)
        client = MagicMock()

        client_wrapper._on_connect(client, None, None, _reason(0), None)

        subscribed = [call.args[0] for call in client.subscribe.call_args_list]
        assert validate_and_write_request_topic(CONFIG_OWNER_ID) in subscribed
        assert get_current_values_request_topic(CONFIG_OWNER_ID) in subscribed
        assert restart_request_topic(CONFIG_OWNER_ID) in subscribed
        assert descriptor_topic("+") in subscribed
        published_topics = [call.args[0] for call in client.publish.call_args_list]
        assert descriptor_topic(CONFIG_OWNER_ID) in published_topics
        assert state_topic(CONFIG_OWNER_ID) in published_topics

    def test_a_refused_connection_does_not_publish_or_subscribe_for_config_service(
        self, tmp_path: Path
    ) -> None:
        client_wrapper = _make_client(tmp_path)
        client = MagicMock()

        client_wrapper._on_connect(client, None, None, _reason(5), None)

        client.subscribe.assert_not_called()
        client.publish.assert_not_called()


class TestOnMessage:
    def test_own_restart_request_is_handled_and_does_not_reach_registry_dispatch(
        self, tmp_path: Path
    ) -> None:
        client_wrapper = _make_client(tmp_path)
        message = MagicMock()
        message.topic = restart_request_topic(CONFIG_OWNER_ID)
        message.payload = b"not json"

        client_wrapper._on_message(MagicMock(), None, message)

        assert client_wrapper._registry.get(CONFIG_OWNER_ID) is None

    def test_other_messages_still_reach_the_registry_dispatch(self, tmp_path: Path) -> None:
        client_wrapper = _make_client(tmp_path)
        message = MagicMock()
        message.topic = descriptor_topic("nordpool")
        message.payload = b""

        # Should not raise, and should fall through to the existing
        # Descriptor/response dispatch (an empty payload just removes an
        # owner_id that was never registered).
        client_wrapper._on_message(MagicMock(), None, message)


class TestStop:
    def test_clears_the_retained_descriptor_before_disconnecting(self, tmp_path: Path) -> None:
        client_wrapper = _make_client(tmp_path)
        client = MagicMock()
        client_wrapper._client = client

        client_wrapper.stop()

        client.publish.assert_any_call(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
        client.publish.assert_any_call(
            state_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
        client.loop_stop.assert_called_once()
        client.disconnect.assert_called_once()


class TestRestartRequested:
    def test_exposes_the_config_owners_restart_requested_event(self, tmp_path: Path) -> None:
        client_wrapper = _make_client(tmp_path)

        assert client_wrapper.restart_requested is client_wrapper._config_owner.restart_requested
