"""Unit tests for mimirheim.io.awaiting_configuration.AwaitingConfigurationClient.

Covers the Awaiting Configuration Operational State (ADR-0009, ADR-0011): a
Config Owner connected to MQTT on Broker Settings alone, serving only the
Config Service protocol — Descriptor, Operational State, get_current_values,
validate_and_write, restart_request — with no data topics and no solve loop.
See tests/unit/test_mqtt_client.py for the Operational counterpart.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from ruamel.yaml import YAML

from mimirheim.config.schema import MqttConfig
from mimirheim.io import config_service
from mimirheim.io.awaiting_configuration import AwaitingConfigurationClient
from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    GetCurrentValuesRequest,
    GetCurrentValuesResult,
    OperationalState,
    RestartRequest,
    RestartResponse,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
)

_FIXTURE_CONFIG_PATH = Path(__file__).parent / "fixtures" / "sample_mimirheim_config.yaml"
_DETAIL = "grid: Field required"


def _make_client(
    config_path: Path | None = None,
    on_restart_requested: MagicMock | None = None,
) -> AwaitingConfigurationClient:
    mqtt = MqttConfig(host="localhost", client_id="mimir")
    paho_mock = MagicMock()
    return AwaitingConfigurationClient(
        paho_client=paho_mock,
        mqtt=mqtt,
        config_path=config_path or Path("unused-mimirheim-config.yaml"),
        detail=_DETAIL,
        on_restart_requested=on_restart_requested or MagicMock(),
    )


def _connect(client: AwaitingConfigurationClient, failed: bool = False) -> None:
    reason_code = MagicMock()
    reason_code.is_failure = failed
    client._on_connect(client._client, None, None, reason_code, None)


class TestConnect:
    def test_connects_using_broker_settings_and_starts_the_network_loop(self) -> None:
        client = _make_client()

        client.start()

        client._client.connect.assert_called_once_with("localhost", 1883)
        client._client.loop_start.assert_called_once_with()

    def test_subscribes_to_the_three_config_service_request_topics_on_connect(self) -> None:
        client = _make_client()

        _connect(client)

        subscribed = {call.args[0] for call in client._client.subscribe.call_args_list}
        assert subscribed == {
            config_service.REQUEST_TOPIC,
            config_service.GET_CURRENT_VALUES_REQUEST_TOPIC,
            config_service.RESTART_REQUEST_TOPIC,
        }

    def test_publishes_descriptor_and_awaiting_configuration_state_retained(self) -> None:
        client = _make_client()

        _connect(client)

        publishes = {call.args[0]: call.kwargs for call in client._client.publish.call_args_list}
        assert publishes[config_service.TOPIC]["payload"] == config_service.payload_bytes()
        assert publishes[config_service.TOPIC]["retain"] is True

        state = OperationalState.model_validate_json(publishes[config_service.STATE_TOPIC]["payload"])
        assert state.state == "awaiting_configuration"
        assert state.detail == _DETAIL
        assert publishes[config_service.STATE_TOPIC]["retain"] is True

    def test_nothing_is_published_or_subscribed_on_failed_connect(self) -> None:
        client = _make_client()

        _connect(client, failed=True)

        client._client.subscribe.assert_not_called()
        client._client.publish.assert_not_called()


class TestStop:
    def test_clears_descriptor_and_state_then_disconnects(self) -> None:
        client = _make_client()

        client.stop()

        publishes = {call.args[0]: call.kwargs for call in client._client.publish.call_args_list}
        assert publishes[config_service.TOPIC]["payload"] == CLEARING_PAYLOAD
        assert publishes[config_service.TOPIC]["retain"] is True
        assert publishes[config_service.STATE_TOPIC]["payload"] == CLEARING_PAYLOAD
        assert publishes[config_service.STATE_TOPIC]["retain"] is True
        client._client.disconnect.assert_called_once_with()
        client._client.loop_stop.assert_called_once_with()


class TestValidateAndWrite:
    def _msg(self, request: ValidateAndWriteRequest) -> MagicMock:
        msg = MagicMock()
        msg.topic = config_service.REQUEST_TOPIC
        msg.payload = request.model_dump_json().encode("utf-8")
        return msg

    def test_valid_candidate_values_are_written_and_success_is_published(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(_FIXTURE_CONFIG_PATH.read_text())
        client = _make_client(config_path=config_path)
        request = ValidateAndWriteRequest(
            request_id="req-1",
            values={"grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0}},
        )

        client._on_message(client._client, None, self._msg(request))

        publishes = [
            call
            for call in client._client.publish.call_args_list
            if call.args[0] == config_service.RESPONSE_TOPIC
        ]
        assert len(publishes) == 1
        result = ValidateAndWriteResult.model_validate_json(publishes[0].kwargs["payload"])
        assert result == ValidateAndWriteResult(request_id="req-1", success=True)
        assert publishes[0].kwargs["retain"] is False

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["grid"]["import_limit_kw"] == 12.0

    def test_malformed_envelope_is_logged_and_dropped(self, caplog) -> None:
        import logging

        client = _make_client()
        msg = MagicMock()
        msg.topic = config_service.REQUEST_TOPIC
        msg.payload = b"not json"

        with caplog.at_level(logging.ERROR, logger="mimirheim.mqtt"):
            client._on_message(client._client, None, msg)

        assert "Traceback" in caplog.text
        assert not any(
            call.args[0] == config_service.RESPONSE_TOPIC
            for call in client._client.publish.call_args_list
        )


class TestGetCurrentValues:
    def test_current_on_disk_values_are_published(self, tmp_path: Path) -> None:
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(_FIXTURE_CONFIG_PATH.read_text())
        client = _make_client(config_path=config_path)
        msg = MagicMock()
        msg.topic = config_service.GET_CURRENT_VALUES_REQUEST_TOPIC
        msg.payload = GetCurrentValuesRequest(request_id="req-2").model_dump_json().encode("utf-8")

        client._on_message(client._client, None, msg)

        publishes = [
            call
            for call in client._client.publish.call_args_list
            if call.args[0] == config_service.GET_CURRENT_VALUES_RESPONSE_TOPIC
        ]
        assert len(publishes) == 1
        result = GetCurrentValuesResult.model_validate_json(publishes[0].kwargs["payload"])
        assert result.request_id == "req-2"
        assert "grid" in result.values


class TestRestartRequest:
    def test_valid_request_is_acknowledged_and_triggers_the_restart_callback(self) -> None:
        on_restart_requested = MagicMock()
        client = _make_client(on_restart_requested=on_restart_requested)
        msg = MagicMock()
        msg.topic = config_service.RESTART_REQUEST_TOPIC
        msg.payload = RestartRequest(request_id="req-3").model_dump_json().encode("utf-8")

        client._on_message(client._client, None, msg)

        publishes = [
            call
            for call in client._client.publish.call_args_list
            if call.args[0] == config_service.RESTART_RESPONSE_TOPIC
        ]
        assert len(publishes) == 1
        response = RestartResponse.model_validate_json(publishes[0].kwargs["payload"])
        assert response.request_id == "req-3"
        on_restart_requested.assert_called_once_with()

    def test_malformed_envelope_is_logged_and_dropped_without_triggering_restart(
        self, caplog
    ) -> None:
        import logging

        on_restart_requested = MagicMock()
        client = _make_client(on_restart_requested=on_restart_requested)
        msg = MagicMock()
        msg.topic = config_service.RESTART_REQUEST_TOPIC
        msg.payload = b"not json"

        with caplog.at_level(logging.ERROR, logger="mimirheim.mqtt"):
            client._on_message(client._client, None, msg)

        assert "Traceback" in caplog.text
        on_restart_requested.assert_not_called()
