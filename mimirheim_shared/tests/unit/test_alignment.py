"""Tests for the spec/model alignment assertion utility."""

import pytest
from pydantic import BaseModel, ConfigDict

from mimirheim_shared.alignment import assert_form_spec_complete
from mimirheim_shared.field_shape import FieldShape
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


class _Inner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    name: str


class _InnerSpecMissingName(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float
    name: str


_INNER_SPEC = FormSpec(
    fields={
        "value": FieldSpec(label="Value", description="A value."),
        "name": FieldSpec(label="Name", description="A name."),
    }
)

_INNER_SPEC_MISSING_NAME = FormSpec(
    fields={
        "value": FieldSpec(label="Value", description="A value."),
    }
)


class _WithNestedObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nested: _Inner


class _WithOptionalObject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    nested: _Inner | None


class _WithNamedCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: dict[str, _Inner]


class _WithOrderedCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[_Inner]


def test_recurses_into_a_nested_object_field() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(
                label="Nested", description="A nested object.", nested_form_spec=_INNER_SPEC
            ),
        }
    )

    assert_form_spec_complete(_WithNestedObject, spec)


def test_recurses_into_an_optional_object_field() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(
                label="Nested", description="An optional nested object.", nested_form_spec=_INNER_SPEC
            ),
        }
    )

    assert_form_spec_complete(_WithOptionalObject, spec)


def test_recurses_into_a_named_collection_field() -> None:
    spec = FormSpec(
        fields={
            "entries": FieldSpec(
                label="Entries", description="Named entries.", nested_form_spec=_INNER_SPEC
            ),
        }
    )

    assert_form_spec_complete(_WithNamedCollection, spec)


def test_recurses_into_an_ordered_collection_field() -> None:
    spec = FormSpec(
        fields={
            "entries": FieldSpec(
                label="Entries", description="Ordered entries.", nested_form_spec=_INNER_SPEC
            ),
        }
    )

    assert_form_spec_complete(_WithOrderedCollection, spec)


def test_recursion_catches_a_missing_field_at_any_depth() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(
                label="Nested",
                description="A nested object.",
                nested_form_spec=_INNER_SPEC_MISSING_NAME,
            ),
        }
    )

    with pytest.raises(ValueError, match="name"):
        assert_form_spec_complete(_WithNestedObject, spec)


def test_recursion_catches_a_missing_field_inside_a_collection() -> None:
    spec = FormSpec(
        fields={
            "entries": FieldSpec(
                label="Entries",
                description="Named entries.",
                nested_form_spec=_INNER_SPEC_MISSING_NAME,
            ),
        }
    )

    with pytest.raises(ValueError, match="name"):
        assert_form_spec_complete(_WithNamedCollection, spec)


def test_structural_field_without_nested_spec_or_override_raises() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(label="Nested", description="A nested object."),
        }
    )

    with pytest.raises(ValueError, match="nested"):
        assert_form_spec_complete(_WithNestedObject, spec)


def test_hidden_structural_field_does_not_need_a_nested_spec() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(label="Nested", description="A nested object.", hidden=True),
        }
    )

    assert_form_spec_complete(_WithNestedObject, spec)


class _Outer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inner: _WithNestedObject


def test_recursion_catches_a_missing_field_two_levels_deep() -> None:
    spec = FormSpec(
        fields={
            "inner": FieldSpec(
                label="Inner",
                description="A doubly-nested object.",
                nested_form_spec=FormSpec(
                    fields={
                        "nested": FieldSpec(
                            label="Nested",
                            description="A nested object.",
                            nested_form_spec=_INNER_SPEC_MISSING_NAME,
                        ),
                    }
                ),
            ),
        }
    )

    with pytest.raises(ValueError, match="name"):
        assert_form_spec_complete(_Outer, spec)


def test_shape_override_to_scalar_skips_recursion() -> None:
    spec = FormSpec(
        fields={
            "nested": FieldSpec(
                label="Nested",
                description="A nested object.",
                shape_override=FieldShape.SCALAR,
            ),
        }
    )

    assert_form_spec_complete(_WithNestedObject, spec)
