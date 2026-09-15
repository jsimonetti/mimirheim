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
    GetCurrentValuesRequest,
    GetCurrentValuesResult,
    OperationalState,
    RestartRequest,
    RestartResponse,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    descriptor_topic,
    get_current_values_request_topic,
    get_current_values_response_topic,
    restart_request_topic,
    restart_response_topic,
    state_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec, resolve_field_shapes

from helper_common.config_owner import ConfigOwnerSupport

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_helper_config.yaml"


class _ToyHelperConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    area: str


_TOY_FORM_SPEC = FormSpec(
    fields={"area": FieldSpec(label="Area", description="Price area code.")}
)


def _support(
    config_path: Path, *, awaiting_configuration_detail: str | None = None
) -> ConfigOwnerSupport:
    return ConfigOwnerSupport(
        owner_id="toy-helper",
        display_name="Toy Helper",
        model=_ToyHelperConfig,
        form_spec=_TOY_FORM_SPEC,
        config_path=config_path,
        awaiting_configuration_detail=awaiting_configuration_detail,
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

        client.subscribe.assert_any_call(validate_and_write_request_topic("toy-helper"), qos=1)
        client.subscribe.assert_any_call(get_current_values_request_topic("toy-helper"), qos=1)
        client.subscribe.assert_any_call(restart_request_topic("toy-helper"), qos=1)
        publish_calls = {c.args[0]: c for c in client.publish.call_args_list}
        descriptor_call = publish_calls[descriptor_topic("toy-helper")]
        assert descriptor_call.kwargs["retain"] is True
        descriptor = Descriptor.model_validate_json(descriptor_call.kwargs["payload"])
        assert descriptor.owner_id == "toy-helper"
        assert descriptor.display_name == "Toy Helper"
        assert descriptor.json_schema == _ToyHelperConfig.model_json_schema()
        assert descriptor.form_spec == resolve_field_shapes(_ToyHelperConfig, _TOY_FORM_SPEC)

    def test_publishes_operational_state_by_default(self, tmp_path: Path) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.on_connect(client)

        publish_calls = {c.args[0]: c for c in client.publish.call_args_list}
        state_call = publish_calls[state_topic("toy-helper")]
        assert state_call.kwargs["retain"] is True
        state = OperationalState.model_validate_json(state_call.kwargs["payload"])
        assert state == OperationalState(state="operational")

    def test_publishes_awaiting_configuration_state_with_detail_when_constructed_that_way(
        self, tmp_path: Path
    ) -> None:
        support = _support(tmp_path / "config.yaml", awaiting_configuration_detail="bad field")
        client = MagicMock()

        support.on_connect(client)

        publish_calls = {c.args[0]: c for c in client.publish.call_args_list}
        state = OperationalState.model_validate_json(
            publish_calls[state_topic("toy-helper")].kwargs["payload"]
        )
        assert state == OperationalState(state="awaiting_configuration", detail="bad field")


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

    def test_get_current_values_request_is_answered_with_current_on_disk_values(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        support = _support(config_path)
        client = MagicMock()
        request = GetCurrentValuesRequest(request_id="req-3")

        handled = support.handle_message(
            client,
            _message(
                get_current_values_request_topic("toy-helper"),
                request.model_dump_json().encode("utf-8"),
            ),
        )

        assert handled is True
        client.publish.assert_called_once()
        publish_call = client.publish.call_args
        assert publish_call.args[0] == get_current_values_response_topic("toy-helper")
        assert publish_call.kwargs["retain"] is False
        result = GetCurrentValuesResult.model_validate_json(publish_call.kwargs["payload"])
        assert result.request_id == "req-3"
        assert result.values["area"] == "NL"

    def test_a_malformed_get_current_values_envelope_is_logged_and_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        support = _support(config_path)
        client = MagicMock()

        handled = support.handle_message(
            client, _message(get_current_values_request_topic("toy-helper"), b"not json")
        )

        assert handled is True
        client.publish.assert_not_called()
        assert "Failed to handle get_current_values request" in caplog.text


class TestRestartRequest:
    def test_acknowledges_and_sets_restart_requested(self, tmp_path: Path) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()
        request = RestartRequest(request_id="req-9")

        handled = support.handle_message(
            client,
            _message(restart_request_topic("toy-helper"), request.model_dump_json().encode("utf-8")),
        )

        assert handled is True
        assert support.restart_requested.is_set()
        client.publish.assert_called_once()
        publish_call = client.publish.call_args
        assert publish_call.args[0] == restart_response_topic("toy-helper")
        assert publish_call.kwargs["retain"] is False
        response = RestartResponse.model_validate_json(publish_call.kwargs["payload"])
        assert response == RestartResponse(request_id="req-9")

    def test_a_malformed_request_envelope_is_logged_and_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        handled = support.handle_message(
            client, _message(restart_request_topic("toy-helper"), b"not json")
        )

        assert handled is True
        assert not support.restart_requested.is_set()
        client.publish.assert_not_called()
        assert "Failed to handle restart_request" in caplog.text

    def test_restart_requested_starts_unset(self, tmp_path: Path) -> None:
        support = _support(tmp_path / "config.yaml")

        assert not support.restart_requested.is_set()


class TestClearDescriptor:
    def test_publishes_a_retained_empty_payload_to_the_descriptor_topic(
        self, tmp_path: Path
    ) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.clear_descriptor(client)

        client.publish.assert_any_call(
            descriptor_topic("toy-helper"), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )

    def test_also_clears_the_operational_state_topic(self, tmp_path: Path) -> None:
        support = _support(tmp_path / "config.yaml")
        client = MagicMock()

        support.clear_descriptor(client)

        client.publish.assert_any_call(
            state_topic("toy-helper"), payload=CLEARING_PAYLOAD, qos=1, retain=True
        )
