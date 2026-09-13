"""Tests for Field Shape derivation and the one-directional Shape Override rule."""

from typing import Literal

import pytest
from pydantic import BaseModel, ConfigDict

from mimirheim_shared.field_shape import (
    FieldShape,
    derive_field_shape,
    effective_field_shape,
    nested_model_of,
)


class _Nested(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: float


class _Toy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scalar_field: float
    nested_field: _Nested
    optional_nested_field: _Nested | None
    named_collection_field: dict[str, _Nested]
    ordered_collection_field: list[_Nested]
    optional_ordered_collection_field: list[_Nested] | None
    enum_field: Literal["a", "b"]
    optional_scalar_field: float | None


def _annotation(name: str) -> object:
    return _Toy.model_fields[name].annotation


def test_scalar_shape() -> None:
    assert derive_field_shape(_annotation("scalar_field")) is FieldShape.SCALAR


def test_optional_scalar_is_still_scalar() -> None:
    assert derive_field_shape(_annotation("optional_scalar_field")) is FieldShape.SCALAR


def test_nested_object_shape() -> None:
    assert derive_field_shape(_annotation("nested_field")) is FieldShape.NESTED_OBJECT


def test_optional_object_shape() -> None:
    assert derive_field_shape(_annotation("optional_nested_field")) is FieldShape.OPTIONAL_OBJECT


def test_named_collection_shape() -> None:
    assert derive_field_shape(_annotation("named_collection_field")) is FieldShape.NAMED_COLLECTION


def test_ordered_collection_shape() -> None:
    assert derive_field_shape(_annotation("ordered_collection_field")) is FieldShape.ORDERED_COLLECTION


def test_optional_ordered_collection_is_still_ordered_collection() -> None:
    # An optional list keeps its Ordered Collection shape: "not provided" is
    # expressed by an empty/absent value, not by a separate Field Shape.
    assert (
        derive_field_shape(_annotation("optional_ordered_collection_field"))
        is FieldShape.ORDERED_COLLECTION
    )


def test_enum_select_shape() -> None:
    assert derive_field_shape(_annotation("enum_field")) is FieldShape.ENUM_SELECT


def test_nested_model_of_scalar_is_none() -> None:
    assert nested_model_of(_annotation("scalar_field")) is None


def test_nested_model_of_nested_object() -> None:
    assert nested_model_of(_annotation("nested_field")) is _Nested


def test_nested_model_of_optional_object() -> None:
    assert nested_model_of(_annotation("optional_nested_field")) is _Nested


def test_nested_model_of_named_collection() -> None:
    assert nested_model_of(_annotation("named_collection_field")) is _Nested


def test_nested_model_of_ordered_collection() -> None:
    assert nested_model_of(_annotation("ordered_collection_field")) is _Nested


def test_effective_shape_defaults_to_derived_when_no_override() -> None:
    assert effective_field_shape(FieldShape.NESTED_OBJECT, None) is FieldShape.NESTED_OBJECT


def test_effective_shape_uses_override_when_simpler() -> None:
    assert (
        effective_field_shape(FieldShape.NESTED_OBJECT, FieldShape.SCALAR)
        is FieldShape.SCALAR
    )


def test_effective_shape_rejects_override_that_adds_structure() -> None:
    with pytest.raises(ValueError, match="simpler"):
        effective_field_shape(FieldShape.SCALAR, FieldShape.NESTED_OBJECT)


def test_effective_shape_rejects_collection_override_to_single_nested_object() -> None:
    # Named/Ordered Collection are ranked above a single nested object: a
    # collection cannot be overridden down to "one object", only down to
    # something with no structure at all.
    with pytest.raises(ValueError, match="simpler"):
        effective_field_shape(FieldShape.NAMED_COLLECTION, FieldShape.NESTED_OBJECT)
