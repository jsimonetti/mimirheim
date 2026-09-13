"""Tests for the FormSpec presentation types."""

import pytest
from pydantic import ValidationError

from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier


def test_tier_has_basic_and_expert() -> None:
    assert Tier.BASIC != Tier.EXPERT


def test_field_spec_minimal() -> None:
    spec = FieldSpec(label="Battery capacity", description="Usable capacity in kWh.")

    assert spec.label == "Battery capacity"
    assert spec.description == "Usable capacity in kWh."
    assert spec.tier is Tier.BASIC
    assert spec.help_text is None
    assert spec.doc_link is None
    assert spec.group is None
    assert spec.hidden is False


def test_field_spec_full() -> None:
    spec = FieldSpec(
        label="Autodiscovery prefix",
        description="MQTT prefix used for Home Assistant autodiscovery.",
        help_text="Only used when autodiscovery is enabled.",
        doc_link="https://example.invalid/wiki/autodiscovery",
        tier=Tier.EXPERT,
        group="Home Assistant",
    )

    assert spec.tier is Tier.EXPERT
    assert spec.help_text == "Only used when autodiscovery is enabled."
    assert spec.doc_link == "https://example.invalid/wiki/autodiscovery"
    assert spec.group == "Home Assistant"


def test_field_spec_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        FieldSpec(label="x", description="y", nonsense="z")


def test_form_spec_holds_fields_by_name() -> None:
    form = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh."),
        }
    )

    assert "capacity_kwh" in form.fields
    assert form.fields["capacity_kwh"].label == "Capacity"


def test_form_spec_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        FormSpec(fields={}, nonsense="z")
