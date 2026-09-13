"""Unit tests for config_editor_v3.mqtt_client.ConfigEditorMqttClient.

Fakes the paho message per the pattern in `tests/unit/test_mqtt_client.py`
(mimirheim core): constructs `MagicMock` messages directly and asserts on the
resulting `ConfigOwnerRegistry` state, with no real broker connection.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from helper_common.config import MqttConfig
from mimirheim_shared.config_service import CLEARING_PAYLOAD, build_descriptor, descriptor_payload
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
