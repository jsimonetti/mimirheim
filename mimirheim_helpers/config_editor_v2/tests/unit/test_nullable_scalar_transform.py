"""Unit tests for config_editor_v2.transforms's `nullable-scalar` transform.

Tests verify:
- Schema-side: the `anyOf` between a scalar branch and null is replaced by
  the non-null branch's own schema, with a constrained branch's own keys
  (e.g. `minimum`) surviving the collapse, and a `default: null` dropped
  entirely.
- Data-side: an empty submitted string is converted to `None` before
  validation; `None` and a non-empty value pass through unchanged.
"""

from __future__ import annotations

from config_editor_v2.adapter import transform_incoming_data, transform_schema

from ..conftest import NullableScalarModel, NullableScalarNumberModel


def _field_schema(model: type, field_name: str) -> dict:
    return model.model_json_schema()["properties"][field_name]


def test_schema_anyof_replaced_with_plain_string() -> None:
    """The rewritten schema has no `anyOf` key and `type == "string"`."""
    schema = _field_schema(NullableScalarModel, "note")

    transformed = transform_schema(schema, {})

    assert "anyOf" not in transformed
    assert transformed["type"] == "string"


def test_schema_default_none_is_dropped() -> None:
    """A `default: null` key is removed, not carried forward as `None`."""
    schema = _field_schema(NullableScalarModel, "note")
    assert schema["default"] is None  # sanity check on the fixture

    transformed = transform_schema(schema, {})

    assert "default" not in transformed


def test_schema_description_is_preserved() -> None:
    """A key already on the field (not part of the anyOf branches) survives."""
    schema = _field_schema(NullableScalarModel, "note")

    transformed = transform_schema(schema, {})

    assert transformed["description"] == "A note."


def test_schema_constrained_number_branch_keys_survive() -> None:
    """A `ge`-constrained number branch's own `minimum` key survives the collapse."""
    schema = _field_schema(NullableScalarNumberModel, "amount")

    transformed = transform_schema(schema, {})

    assert transformed["type"] == "number"
    assert transformed["minimum"] == 0.0


def test_data_empty_string_converted_to_none() -> None:
    """Submitting an empty string produces `None`."""
    schema = _field_schema(NullableScalarModel, "note")

    assert transform_incoming_data(schema, "") is None


def test_data_nonempty_string_passed_through() -> None:
    """Submitting a non-empty string produces the same string, unchanged."""
    schema = _field_schema(NullableScalarModel, "note")

    assert transform_incoming_data(schema, "hello") == "hello"


def test_data_none_passed_through() -> None:
    """Submitting `None` produces `None`."""
    schema = _field_schema(NullableScalarModel, "note")

    assert transform_incoming_data(schema, None) is None
