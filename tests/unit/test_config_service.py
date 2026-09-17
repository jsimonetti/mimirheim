"""Unit tests for mimirheim.io.config_service.

This module only builds mimirheim core's Config Service Descriptor topic and
payload; it never touches an MQTT client. See tests/unit/test_mqtt_client.py
for coverage of MqttClient publishing/clearing the Descriptor on its one
connection, and tests/integration/test_config_service_roundtrip.py for the
real-broker round trip.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from ruamel.yaml import YAML

from mimirheim.config.formspec import MIMIRHEIM_CONFIG_FORM_SPEC
from mimirheim.config.schema import MimirheimConfig
from mimirheim.io.config_service import (
    DISPLAY_NAME,
    OWNER_ID,
    REQUEST_TOPIC,
    RESPONSE_TOPIC,
    RESTART_REQUEST_TOPIC,
    RESTART_RESPONSE_TOPIC,
    STATE_TOPIC,
    TOPIC,
    awaiting_configuration_state_payload,
    handle_restart_request,
    handle_validate_and_write,
    operational_state_payload_bytes,
    payload_bytes,
)
from mimirheim_shared.config_service import (
    Descriptor,
    OperationalState,
    RestartRequest,
    RestartResponse,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    descriptor_topic,
    restart_request_topic,
    restart_response_topic,
    state_topic,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
)
from mimirheim_shared.formspec import resolve_field_shapes

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_mimirheim_config.yaml"


def test_topic_is_the_well_known_descriptor_topic_for_this_owner() -> None:
    assert TOPIC == descriptor_topic(OWNER_ID)
    assert OWNER_ID == "mimirheim-core"


def test_payload_bytes_is_a_valid_descriptor_for_mimirheim_config() -> None:
    payload = payload_bytes()

    descriptor = Descriptor.model_validate_json(payload)
    assert descriptor.owner_id == OWNER_ID
    assert descriptor.display_name == DISPLAY_NAME
    assert descriptor.form_spec == resolve_field_shapes(MimirheimConfig, MIMIRHEIM_CONFIG_FORM_SPEC)
    assert descriptor.json_schema == MimirheimConfig.model_json_schema()


def test_request_and_response_topics_are_the_well_known_ones_for_this_owner() -> None:
    assert REQUEST_TOPIC == validate_and_write_request_topic(OWNER_ID)
    assert RESPONSE_TOPIC == validate_and_write_response_topic(OWNER_ID)


class TestHandleValidateAndWrite:
    def test_valid_candidate_values_are_written_and_success_is_published(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        request = ValidateAndWriteRequest(
            request_id="req-1",
            values={
                "mqtt": {"host": "localhost", "port": 1883},
                "grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0},
            },
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-1", success=True)

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["grid"]["import_limit_kw"] == 12.0
        assert written["grid"]["export_limit_kw"] == 5.0
        # Sibling values and comments untouched by this request survive.
        assert written["mqtt"]["host"] == "localhost"
        raw = config_path.read_text()
        assert "# broker address" in raw
        assert "# DNO connection agreement" in raw

    def test_partial_candidate_values_are_merged_with_on_disk_values_before_validation(
        self, tmp_path: Path
    ) -> None:
        """A submission touching only one section must validate against the
        merged result, not against the submission in isolation: the fixture's
        on-disk mqtt section (required by MimirheimConfig but absent here)
        must still count."""
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        request = ValidateAndWriteRequest(
            request_id="req-3",
            values={"grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0}},
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-3", success=True)

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["grid"]["import_limit_kw"] == 12.0
        assert written["mqtt"]["host"] == "localhost"

    def test_written_values_are_pydantic_coerced_not_the_raw_submission(
        self, tmp_path: Path
    ) -> None:
        """A Config Editor submits JSON-compatible values (e.g. a string from
        an HTML form field), not necessarily the type the model stores. The
        write-through must use MimirheimConfig's coerced value, not the raw
        string, or the on-disk YAML ends up with the wrong type."""
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        request = ValidateAndWriteRequest(
            request_id="req-4",
            values={"grid": {"import_limit_kw": "12.0", "export_limit_kw": 5.0}},
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-4", success=True)

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["grid"]["import_limit_kw"] == 12.0
        assert isinstance(written["grid"]["import_limit_kw"], float)

    def test_invalid_candidate_values_are_rejected_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        original = config_path.read_text()
        request = ValidateAndWriteRequest(
            request_id="req-2",
            values={
                "mqtt": {"host": "localhost", "port": 1883},
                # import_limit_kw must be >= 0.
                "grid": {"import_limit_kw": -5.0, "export_limit_kw": 5.0},
            },
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result.request_id == "req-2"
        assert result.success is False
        assert result.errors
        assert config_path.read_text() == original

    def test_malformed_request_envelope_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())

        with pytest.raises(ValidationError):
            handle_validate_and_write(b"not json", config_path)


class TestHandleValidateAndWriteWithMqttEnvOverrides:
    """Reproduces the reported bug: mimirheim core started with MQTT_HOST
    supplied by the Supervisor (no mqtt.host in the file) connects and runs
    fine, but saving an unrelated field via the Config Editor previously
    failed with "mqtt: Field required" because handle_validate_and_write
    merged Candidate Values onto the raw on-disk file, which never carries a
    value the environment supplies instead."""

    def test_env_supplied_host_satisfies_validation_without_being_in_the_submission(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker.local")
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text("grid:\n  import_limit_kw: 10.0\n  export_limit_kw: 5.0\n")
        request = ValidateAndWriteRequest(
            request_id="req-5",
            values={"grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0}},
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-5", success=True)

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["grid"]["import_limit_kw"] == 12.0
        # The env-supplied host must not leak into the file: a plain Docker
        # deployment without MQTT_HOST set must not silently inherit it.
        assert "mqtt" not in written

    def test_without_mqtt_env_the_same_submission_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MQTT_HOST", raising=False)
        config_path = tmp_path / "mimirheim.yaml"
        config_path.write_text("grid:\n  import_limit_kw: 10.0\n  export_limit_kw: 5.0\n")
        request = ValidateAndWriteRequest(
            request_id="req-6",
            values={"grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0}},
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result.success is False
        assert any("mqtt" in error for error in result.errors)


def test_state_topic_is_the_well_known_state_topic_for_this_owner() -> None:
    assert STATE_TOPIC == state_topic(OWNER_ID)


def test_restart_topics_are_the_well_known_ones_for_this_owner() -> None:
    assert RESTART_REQUEST_TOPIC == restart_request_topic(OWNER_ID)
    assert RESTART_RESPONSE_TOPIC == restart_response_topic(OWNER_ID)


def test_operational_state_payload_bytes_is_operational_with_no_detail() -> None:
    state = OperationalState.model_validate_json(operational_state_payload_bytes())
    assert state.state == "operational"
    assert state.detail is None


def test_awaiting_configuration_state_payload_carries_the_validation_detail() -> None:
    state = OperationalState.model_validate_json(
        awaiting_configuration_state_payload("grid.import_limit_kw: must be >= 0")
    )
    assert state.state == "awaiting_configuration"
    assert state.detail == "grid.import_limit_kw: must be >= 0"


class TestHandleRestartRequest:
    def test_valid_request_is_acknowledged_by_echoed_request_id(self) -> None:
        request = RestartRequest(request_id="req-5")

        response_payload = handle_restart_request(request.model_dump_json().encode("utf-8"))

        response = RestartResponse.model_validate_json(response_payload)
        assert response.request_id == "req-5"

    def test_malformed_request_envelope_raises(self) -> None:
        with pytest.raises(ValidationError):
            handle_restart_request(b"not json")
