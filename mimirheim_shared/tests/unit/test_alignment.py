"""Tests for the spec/model alignment assertion utility."""

import pytest
from pydantic import BaseModel, ConfigDict

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.formspec import FieldSpec, FormSpec


class _ToyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capacity_kwh: float
    max_power_kw: float


def test_matching_spec_passes() -> None:
    spec = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh."),
            "max_power_kw": FieldSpec(label="Max power", description="Maximum charge power in kW."),
        }
    )

    assert_form_spec_complete(_ToyModel, spec)


def test_spec_missing_field_raises() -> None:
    spec = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh."),
        }
    )

    with pytest.raises(ValueError, match="max_power_kw"):
        assert_form_spec_complete(_ToyModel, spec)


def test_spec_with_extra_field_raises() -> None:
    spec = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh."),
            "max_power_kw": FieldSpec(label="Max power", description="Maximum charge power in kW."),
            "not_a_real_field": FieldSpec(label="Ghost", description="Does not exist on the model."),
        }
    )

    with pytest.raises(ValueError, match="not_a_real_field"):
        assert_form_spec_complete(_ToyModel, spec)


def test_explicitly_hidden_field_still_counts_as_represented() -> None:
    spec = FormSpec(
        fields={
            "capacity_kwh": FieldSpec(label="Capacity", description="Usable capacity in kWh."),
            "max_power_kw": FieldSpec(
                label="Max power",
                description="Maximum charge power in kW.",
                hidden=True,
            ),
        }
    )

    assert_form_spec_complete(_ToyModel, spec)
    assert spec.fields["max_power_kw"].hidden is True
