"""Unit tests for config_editor_v3.mqtt_client.ConfigEditorMqttClient.

Fakes the paho message per the pattern in `tests/unit/test_mqtt_client.py`
(mimirheim core): constructs `MagicMock` messages directly and asserts on the
resulting `ConfigOwnerRegistry` state, with no real broker connection.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from helper_common.config import MqttConfig
from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    GetCurrentValuesRequest,
    GetCurrentValuesResult,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    build_descriptor,
    descriptor_payload,
    get_current_values_response_topic,
    get_current_values_result_payload,
    validate_and_write_response_topic,
    validate_and_write_result_payload,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec
from pydantic import BaseModel, ConfigDict

from config_editor_v3.mqtt_client import ConfigEditorMqttClient
from config_editor_v3.registry import ConfigOwnerRegistry


class _FakeOwnerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


_FAKE_FORM_SPEC = FormSpec(fields={"enabled": FieldSpec(label="Enabled", description="Enabled.")})


def _make_config() -> object:
    class _Config:
        mqtt = MqttConfig(host="localhost", client_id="test-config-editor-v3")

    return _Config()


def _message(topic: str, payload: bytes) -> MagicMock:
    message = MagicMock()
    message.topic = topic
    message.payload = payload
    return message


def test_on_message_registers_a_valid_descriptor() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    descriptor = build_descriptor("nordpool", "Nordpool prices", _FakeOwnerConfig, _FAKE_FORM_SPEC)
    payload = descriptor_payload(descriptor)

    client._on_message(MagicMock(), None, _message("mimirheim/config-service/nordpool/descriptor", payload))

    assert registry.get("nordpool") == descriptor


def test_on_message_removes_owner_on_clearing_payload() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    descriptor = build_descriptor("nordpool", "Nordpool prices", _FakeOwnerConfig, _FAKE_FORM_SPEC)
    registry.update(descriptor)

    client._on_message(
        MagicMock(), None, _message("mimirheim/config-service/nordpool/descriptor", CLEARING_PAYLOAD)
    )

    assert registry.get("nordpool") is None


def test_on_message_discards_malformed_payload_without_raising() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)

    client._on_message(
        MagicMock(), None, _message("mimirheim/config-service/nordpool/descriptor", b"not json")
    )

    assert registry.get("nordpool") is None


def _fake_publish_replying_with(
    client: ConfigEditorMqttClient, success: bool, errors: list[str] | None = None
) -> Callable[[str, bytes, int], None]:
    """Builds a publish side_effect that immediately delivers a canned response.

    Registering the pending request happens before `submit_validate_and_write`
    calls `publish` (see its implementation), so by the time this side_effect
    runs, `client._on_message` can already find and set it — no real thread or
    broker required to exercise the full request/response round trip.
    """

    def _publish(topic: str, payload: bytes, qos: int = 0) -> None:
        request = ValidateAndWriteRequest.model_validate_json(payload)
        response = ValidateAndWriteResult(request_id=request.request_id, success=success, errors=errors or [])
        client._on_message(
            MagicMock(),
            None,
            _message(validate_and_write_response_topic("mimirheim-core"), validate_and_write_result_payload(response)),
        )

    return _publish


def test_submit_validate_and_write_returns_the_owners_result() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    client._client.publish = MagicMock(side_effect=_fake_publish_replying_with(client, success=True))

    result = client.submit_validate_and_write("mimirheim-core", {"grid": {"import_limit_kw": 17}})

    assert result.success is True
    assert client._pending == {}


def test_submit_validate_and_write_surfaces_validation_errors() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    client._client.publish = MagicMock(
        side_effect=_fake_publish_replying_with(client, success=False, errors=["grid: field required"])
    )

    result = client.submit_validate_and_write("mimirheim-core", {})

    assert result.success is False
    assert result.errors == ["grid: field required"]


def test_submit_validate_and_write_times_out_when_no_response_arrives() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    client._client.publish = MagicMock()

    with pytest.raises(TimeoutError):
        client.submit_validate_and_write("mimirheim-core", {}, timeout=0.05)

    assert client._pending == {}


def test_on_message_discards_malformed_validate_and_write_response_without_raising() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)

    client._on_message(
        MagicMock(),
        None,
        _message(validate_and_write_response_topic("mimirheim-core"), b"not json"),
    )

    assert client._pending == {}


def test_on_message_ignores_response_for_unknown_request_id() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    response = ValidateAndWriteResult(request_id="unknown-request-id", success=True)

    client._on_message(
        MagicMock(),
        None,
        _message(validate_and_write_response_topic("mimirheim-core"), validate_and_write_result_payload(response)),
    )

    assert client._pending == {}


def _fake_publish_replying_with_current_values(
    client: ConfigEditorMqttClient, values: dict
) -> Callable[[str, bytes, int], None]:
    def _publish(topic: str, payload: bytes, qos: int = 0) -> None:
        request = GetCurrentValuesRequest.model_validate_json(payload)
        response = GetCurrentValuesResult(request_id=request.request_id, values=values)
        client._on_message(
            MagicMock(),
            None,
            _message(
                get_current_values_response_topic("mimirheim-core"),
                get_current_values_result_payload(response),
            ),
        )

    return _publish


def test_get_current_values_returns_the_owners_current_values() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    client._client.publish = MagicMock(
        side_effect=_fake_publish_replying_with_current_values(client, {"grid": {"import_limit_kw": 17}})
    )

    values = client.get_current_values("mimirheim-core")

    assert values == {"grid": {"import_limit_kw": 17}}
    assert client._pending == {}


def test_get_current_values_times_out_when_no_response_arrives() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    client._client.publish = MagicMock()

    with pytest.raises(TimeoutError):
        client.get_current_values("mimirheim-core", timeout=0.05)

    assert client._pending == {}


def test_on_message_discards_malformed_get_current_values_response_without_raising() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)

    client._on_message(
        MagicMock(),
        None,
        _message(get_current_values_response_topic("mimirheim-core"), b"not json"),
    )

    assert client._pending == {}


def test_on_message_ignores_get_current_values_response_for_unknown_request_id() -> None:
    registry = ConfigOwnerRegistry()
    client = ConfigEditorMqttClient(_make_config(), registry)
    response = GetCurrentValuesResult(request_id="unknown-request-id", values={})

    client._on_message(
        MagicMock(),
        None,
        _message(
            get_current_values_response_topic("mimirheim-core"),
            get_current_values_result_payload(response),
        ),
    )

    assert client._pending == {}
