"""Tests for the scheduler's Config Service protocol wiring.

Covers only the wiring between ``_make_paho_client``/``main`` and
``ConfigOwnerSupport`` (config-editor-v3 ticket 06): ConfigOwnerSupport's own
behavior is unit-tested in
mimirheim_helpers/common/tests/unit/test_config_owner.py. The scheduler has
no ``MqttDaemon``/``HelperDaemon`` base class, so these tests exercise the
hand-rolled paho callbacks built by ``_make_paho_client`` directly, proving
the wiring does not disturb the scheduler's own connect/disconnect logging.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from helper_common.config_owner import ConfigOwnerSupport
from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    descriptor_topic,
    get_current_values_request_topic,
    validate_and_write_request_topic,
)

from scheduler.__main__ import CONFIG_OWNER_ID, CONFIG_OWNER_DISPLAY_NAME, _make_paho_client
from scheduler.config import SchedulerConfig
from scheduler.formspec import SCHEDULER_CONFIG_FORM_SPEC


def _reason(code: int):
    class _ReasonCode(int):
        @property
        def is_failure(self) -> bool:
            return int(self) != 0

    return _ReasonCode(code)


def _make_config_owner(tmp_path: Path) -> ConfigOwnerSupport:
    return ConfigOwnerSupport(
        owner_id=CONFIG_OWNER_ID,
        display_name=CONFIG_OWNER_DISPLAY_NAME,
        model=SchedulerConfig,
        form_spec=SCHEDULER_CONFIG_FORM_SPEC,
        config_path=tmp_path / "config.yaml",
    )


def _make_config() -> SchedulerConfig:
    return SchedulerConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "schedules": [{"0 6 * * *": "mimir/scheduler/morning"}],
        }
    )


class TestLastWill:
    def test_registers_a_last_will_clearing_the_descriptor_before_connecting(
        self, tmp_path: Path
    ) -> None:
        config_owner = _make_config_owner(tmp_path)
        with patch("scheduler.__main__.paho.Client") as client_cls:
            _make_paho_client(_make_config(), config_owner)

        client = client_cls.return_value
        client.will_set.assert_called_once_with(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
        # The last-will must be registered before connect(), not after: paho
        # only accepts a last-will on a not-yet-connected client.
        call_names = [call[0] for call in client.mock_calls]
        assert call_names.index("will_set") < call_names.index("connect")


class TestOnConnect:
    def test_publishes_the_descriptor_and_subscribes_on_a_successful_connection(
        self, tmp_path: Path
    ) -> None:
        config_owner = _make_config_owner(tmp_path)
        with patch("scheduler.__main__.paho.Client") as client_cls:
            client = client_cls.return_value
            _make_paho_client(_make_config(), config_owner)

        client.on_connect(client, None, None, _reason(0), None)

        subscribed = [call.args[0] for call in client.subscribe.call_args_list]
        assert validate_and_write_request_topic(CONFIG_OWNER_ID) in subscribed
        assert get_current_values_request_topic(CONFIG_OWNER_ID) in subscribed
        published_topics = [call.args[0] for call in client.publish.call_args_list]
        assert descriptor_topic(CONFIG_OWNER_ID) in published_topics

    def test_a_failed_connection_does_not_publish_or_subscribe_for_config_service(
        self, tmp_path: Path
    ) -> None:
        config_owner = _make_config_owner(tmp_path)
        with patch("scheduler.__main__.paho.Client") as client_cls:
            client = client_cls.return_value
            _make_paho_client(_make_config(), config_owner)

        client.on_connect(client, None, None, _reason(5), None)

        client.subscribe.assert_not_called()
        client.publish.assert_not_called()


class TestOnMessage:
    def test_config_service_requests_are_dispatched_to_the_config_owner(
        self, tmp_path: Path
    ) -> None:
        config_owner = _make_config_owner(tmp_path)
        with patch("scheduler.__main__.paho.Client") as client_cls:
            client = client_cls.return_value
            _make_paho_client(_make_config(), config_owner)

        message = MagicMock(
            topic=validate_and_write_request_topic(CONFIG_OWNER_ID), payload=b"not json"
        )

        with patch.object(config_owner, "handle_message") as handle_message:
            client.on_message(client, None, message)

        handle_message.assert_called_once_with(client, message)


class TestClearDescriptor:
    def test_clears_the_retained_descriptor(self, tmp_path: Path) -> None:
        config_owner = _make_config_owner(tmp_path)
        client = MagicMock()

        config_owner.clear_descriptor(client)

        client.publish.assert_called_once_with(
            descriptor_topic(CONFIG_OWNER_ID), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
