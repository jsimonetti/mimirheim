"""Unit tests for ConfigOwnerSupport, the Config Service protocol wrapper.

Per the config-editor-v3 spec's Seam 1 testing decision, the MQTT client is
faked with a MagicMock and the actual file content is asserted with tmp_path,
using a real fixture YAML with comments to prove preservation.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, ConfigDict

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    Descriptor,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    descriptor_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec

from helper_common.config_owner import ConfigOwnerSupport

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_helper_config.yaml"


class _ToyHelperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: str


_TOY_FORM_SPEC = FormSpec(
    fields={"area": FieldSpec(label="Area", description="Price area code.")}
)


def _support(config_path: Path) -> ConfigOwnerSupport:
    return ConfigOwnerSupport(
        owner_id="toy-helper",
        display_name="Toy Helper",
        model=_ToyHelperConfig,
        form_spec=_TOY_FORM_SPEC,
        config_path=config_path,
    )


def _message(topic: str, payload: bytes) -> SimpleNamespace:
    return SimpleNamespace(topic=topic, payload=payload)


class TestRegisterLastWill:
    def test_registers_a_retained_clearing_last_will_on_the_descriptor_topic(
        self, tmp_path: Path
    ) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.register_last_will(client)

        client.will_set.assert_called_once_with(
            descriptor_topic("toy-helper"), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )


class TestOnConnect:
    def test_subscribes_to_the_request_topic_and_publishes_the_descriptor(
        self, tmp_path: Path
    ) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.on_connect(client)

        client.subscribe.assert_called_once_with(
            validate_and_write_request_topic("toy-helper"), qos=1
        )
        publish_call = client.publish.call_args
        assert publish_call.args[0] == descriptor_topic("toy-helper")
        assert publish_call.kwargs["retain"] is True
        descriptor = Descriptor.model_validate_json(publish_call.kwargs["payload"])
        assert descriptor.owner_id == "toy-helper"
        assert descriptor.display_name == "Toy Helper"
        assert descriptor.json_schema == _ToyHelperConfig.model_json_schema()
        assert descriptor.form_spec == _TOY_FORM_SPEC


class TestHandleMessage:
    def test_ignores_messages_on_other_topics(self, tmp_path: Path) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        handled = support.handle_message(client, _message("some/other/topic", b"x"))

        assert handled is False
        client.publish.assert_not_called()

    def test_valid_candidate_values_are_written_and_success_is_published(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        support = _support(config_path)
        client = MagicMock()
        request = ValidateAndWriteRequest(request_id="req-1", values={"area": "SE3"})

        handled = support.handle_message(
            client,
            _message(
                validate_and_write_request_topic("toy-helper"),
                request.model_dump_json().encode("utf-8"),
            ),
        )

        assert handled is True
        client.publish.assert_called_once()
        publish_call = client.publish.call_args
        assert publish_call.args[0] == validate_and_write_response_topic("toy-helper")
        assert publish_call.kwargs["retain"] is False
        result = ValidateAndWriteResult.model_validate_json(publish_call.kwargs["payload"])
        assert result == ValidateAndWriteResult(request_id="req-1", success=True)
        raw = config_path.read_text()
        assert "area: SE3" in raw
        assert "# price area code" in raw

    def test_invalid_candidate_values_are_rejected_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        original = config_path.read_text()
        support = _support(config_path)
        client = MagicMock()
        request = ValidateAndWriteRequest(request_id="req-2", values={"area": 5})

        support.handle_message(
            client,
            _message(
                validate_and_write_request_topic("toy-helper"),
                request.model_dump_json().encode("utf-8"),
            ),
        )

        result = ValidateAndWriteResult.model_validate_json(
            client.publish.call_args.kwargs["payload"]
        )
        assert result.success is False
        assert result.errors
        assert config_path.read_text() == original

    def test_a_malformed_request_envelope_is_logged_and_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        support = _support(config_path)
        client = MagicMock()

        handled = support.handle_message(
            client, _message(validate_and_write_request_topic("toy-helper"), b"not json")
        )

        assert handled is True
        client.publish.assert_not_called()
        assert "Failed to handle validate_and_write request" in caplog.text


class TestClearDescriptor:
    def test_publishes_a_retained_empty_payload_to_the_descriptor_topic(
        self, tmp_path: Path
    ) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.clear_descriptor(client)

        client.publish.assert_called_once_with(
            descriptor_topic("toy-helper"), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
