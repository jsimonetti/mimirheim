"""Tests for the Config Service Descriptor building blocks.

mimirheim_shared never touches an MQTT client (see config_service.py's module
docstring): these tests only exercise the pure topic-naming, Descriptor
construction, and payload serialisation. Each Config Owner's own test suite
covers wiring its Descriptor onto an actual (faked) MQTT client.
"""

from pydantic import BaseModel, ConfigDict

from mimirheim_shared.config_service import (
    CLEARING_PAYLOAD,
    Descriptor,
    build_descriptor,
    descriptor_payload,
    descriptor_topic,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec


class _ToyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float


_TOY_FORM_SPEC = FormSpec(
    fields={"capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh.")}
)


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
