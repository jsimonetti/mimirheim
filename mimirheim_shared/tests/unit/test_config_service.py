"""Tests for the Config Service Descriptor building blocks.

mimirheim_shared never touches an MQTT client (see config_service.py's module
docstring): these tests only exercise the pure topic-naming, Descriptor
construction, payload serialisation, and the generic ``handle_validate_and_write``
validate-then-write sequence. Each Config Owner's own test suite covers wiring
its Descriptor onto an actual (faked) MQTT client.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    Descriptor,
    ValidateAndWriteRequest,
    ValidateAndWriteResult,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
    handle_validate_and_write,
    validate_and_write_request_topic,
    validate_and_write_response_topic,
    validate_and_write_result_payload,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_config.yaml"


class _ToyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float


_TOY_FORM_SPEC = FormSpec(
    fields={"capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh.")}
)


class _ToyMqtt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 1883


class _ToyBattery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float


class _ToyConfig(BaseModel):
    """Shaped to match tests/unit/fixtures/sample_config.yaml."""

    model_config = ConfigDict(extra="forbid")

    mqtt: _ToyMqtt
    battery: _ToyBattery


def test_descriptor_topic_is_well_known_and_owner_scoped() -> None:
    assert descriptor_topic("mimirheim-core") == "mimirheim/config-service/mimirheim-core/descriptor"
    assert descriptor_topic("nordpool") == "mimirheim/config-service/nordpool/descriptor"


def test_clearing_payload_is_empty() -> None:
    assert CLEARING_PAYLOAD == b""


def test_build_descriptor_carries_schema_and_form_spec() -> None:
    descriptor = build_descriptor("toy-owner", "Toy Owner", _ToyModel, _TOY_FORM_SPEC)

    assert isinstance(descriptor, Descriptor)
    assert descriptor.owner_id == "toy-owner"
    assert descriptor.display_name == "Toy Owner"
    assert descriptor.json_schema == _ToyModel.model_json_schema()
    assert descriptor.form_spec == _TOY_FORM_SPEC


def test_descriptor_payload_round_trips_as_json() -> None:
    descriptor = build_descriptor("toy-owner", "Toy Owner", _ToyModel, _TOY_FORM_SPEC)

    payload = descriptor_payload(descriptor)

    assert isinstance(payload, bytes)
    assert Descriptor.model_validate_json(payload) == descriptor


def test_validate_and_write_request_topic_is_well_known_and_owner_scoped() -> None:
    assert (
        validate_and_write_request_topic("mimirheim-core")
        == "mimirheim/config-service/mimirheim-core/validate_and_write/request"
    )
    assert (
        validate_and_write_request_topic("nordpool")
        == "mimirheim/config-service/nordpool/validate_and_write/request"
    )


def test_validate_and_write_response_topic_is_well_known_and_owner_scoped() -> None:
    assert (
        validate_and_write_response_topic("mimirheim-core")
        == "mimirheim/config-service/mimirheim-core/validate_and_write/response"
    )
    assert (
        validate_and_write_response_topic("nordpool")
        == "mimirheim/config-service/nordpool/validate_and_write/response"
    )


def test_validate_and_write_request_round_trips_as_json() -> None:
    request = ValidateAndWriteRequest(request_id="req-1", values={"capacity_kwh": 12.0})

    payload = request.model_dump_json().encode("utf-8")

    assert ValidateAndWriteRequest.model_validate_json(payload) == request


def test_validate_and_write_result_payload_round_trips_as_json_on_success() -> None:
    result = ValidateAndWriteResult(request_id="req-1", success=True)

    payload = validate_and_write_result_payload(result)

    assert isinstance(payload, bytes)
    assert ValidateAndWriteResult.model_validate_json(payload) == result
    assert ValidateAndWriteResult.model_validate_json(payload).errors == []


def test_validate_and_write_result_payload_round_trips_as_json_on_failure() -> None:
    result = ValidateAndWriteResult(
        request_id="req-1", success=False, errors=["capacity_kwh: field required"]
    )

    payload = validate_and_write_result_payload(result)

    assert ValidateAndWriteResult.model_validate_json(payload) == result


class TestHandleValidateAndWrite:
    def test_valid_candidate_values_are_written_and_success_is_published(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        request = ValidateAndWriteRequest(
            request_id="req-1", values={"battery": {"capacity_kwh": 15.0}}
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path, _ToyConfig
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-1", success=True)

        yaml = YAML()
        with config_path.open() as fh:
            written = yaml.load(fh)
        assert written["battery"]["capacity_kwh"] == 15.0
        # Sibling values and comments untouched by this request survive.
        assert written["mqtt"]["host"] == "localhost"
        raw = config_path.read_text()
        assert "# Top-level system configuration" in raw
        assert "# broker address" in raw

    def test_partial_candidate_values_are_merged_with_on_disk_values_before_validation(
        self, tmp_path: Path
    ) -> None:
        """A submission touching only one section must validate against the
        merged result, not against the submission in isolation: the fixture's
        on-disk mqtt section (required by _ToyConfig but absent here) must
        still count."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        request = ValidateAndWriteRequest(
            request_id="req-3", values={"battery": {"capacity_kwh": 15.0}}
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path, _ToyConfig
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result == ValidateAndWriteResult(request_id="req-3", success=True)

    def test_invalid_candidate_values_are_rejected_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())
        original = config_path.read_text()
        request = ValidateAndWriteRequest(
            request_id="req-2", values={"battery": {"capacity_kwh": "not-a-number"}}
        )

        response = handle_validate_and_write(
            request.model_dump_json().encode("utf-8"), config_path, _ToyConfig
        )

        result = ValidateAndWriteResult.model_validate_json(response)
        assert result.request_id == "req-2"
        assert result.success is False
        assert result.errors
        assert config_path.read_text() == original

    def test_malformed_request_envelope_raises(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.yaml"
        config_path.write_text(FIXTURE_PATH.read_text())

        with pytest.raises(ValidationError):
            handle_validate_and_write(b"not json", config_path, _ToyConfig)
