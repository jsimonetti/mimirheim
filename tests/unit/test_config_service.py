"""Unit tests for mimirheim.io.config_service.ConfigServiceClient.

Fakes the paho MQTT client with unittest.mock.MagicMock, per the existing
pattern in tests/unit/test_mqtt_client.py, and asserts on the last-will
registration and the published Descriptor payload. No live broker involved;
see tests/integration/test_config_service_roundtrip.py for the real-broker
round trip.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import GridConfig, MimirheimConfig, MqttConfig
from mimirheim.io.config_service import DISPLAY_NAME, OWNER_ID, ConfigServiceClient
from mimirheim_shared.config_service import CLEARING_PAYLOAD, Descriptor, descriptor_topic


def _make_config() -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", port=1883, client_id="test"),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
    )


def _make_reason_code(is_failure: bool) -> MagicMock:
    reason_code = MagicMock()
    reason_code.is_failure = is_failure
    return reason_code


class TestLastWillRegistration:
    def test_registers_last_will_before_any_connect_call(self) -> None:
        """The last-will must clear the Descriptor topic with an empty retained payload."""
        paho_mock = MagicMock()

        ConfigServiceClient(_make_config(), paho_mock)

        paho_mock.will_set.assert_called_once_with(
            descriptor_topic(OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
        paho_mock.connect.assert_not_called()


class TestPublishOnConnect:
    def test_publishes_descriptor_on_successful_connect(self) -> None:
        paho_mock = MagicMock()
        client = ConfigServiceClient(_make_config(), paho_mock)

        client._on_connect(paho_mock, None, None, _make_reason_code(is_failure=False), None)

        paho_mock.publish.assert_called_once()
        args, kwargs = paho_mock.publish.call_args
        assert args[0] == descriptor_topic(OWNER_ID)
        payload = kwargs.get("payload", args[1] if len(args) > 1 else None)
        assert kwargs.get("qos") == 1
        assert kwargs.get("retain") is True

        descriptor = Descriptor.model_validate_json(payload)
        assert descriptor.owner_id == OWNER_ID
        assert descriptor.display_name == DISPLAY_NAME
        assert descriptor.form_spec == MIMIRHEIM_CONFIG_FORM_SPEC
        assert descriptor.json_schema == MimirheimConfig.model_json_schema()

    def test_does_not_publish_on_failed_connect(self) -> None:
        paho_mock = MagicMock()
        client = ConfigServiceClient(_make_config(), paho_mock)

        client._on_connect(paho_mock, None, None, _make_reason_code(is_failure=True), None)

        paho_mock.publish.assert_not_called()


class TestStartStop:
    def test_start_connects_and_starts_network_loop(self) -> None:
        paho_mock = MagicMock()
        config = _make_config()
        client = ConfigServiceClient(config, paho_mock)

        client.start()

        paho_mock.connect.assert_called_once_with(config.mqtt.host, config.mqtt.port)
        paho_mock.loop_start.assert_called_once()

    def test_stop_clears_descriptor_and_disconnects(self) -> None:
        paho_mock = MagicMock()
        client = ConfigServiceClient(_make_config(), paho_mock)

        client.stop()

        paho_mock.publish.assert_called_once_with(
            descriptor_topic(OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
        paho_mock.disconnect.assert_called_once()
        paho_mock.loop_stop.assert_called_once()
