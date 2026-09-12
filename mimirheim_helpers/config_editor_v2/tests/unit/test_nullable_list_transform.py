"""Unit tests for config_editor_v2.transforms's `nullable-list` transform.

Tests verify:
- Schema-side: the `anyOf` between an array-with-minimum and null is
  replaced with a plain, unenforced array schema, with the original minimum
  (if any) preserved only as a non-enforcing advisory hint.
- Data-side: an empty submitted list is converted to `None` before
  validation, a non-empty list and `None` pass through unchanged, and a
  too-short non-empty list is not rejected by this transform.
- The specific alternative-pair validation failure IMPLEMENTATION_DETAILS.md
  describes, which the empty-list-to-None conversion exists to prevent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from config_editor_v2.adapter import transform_incoming_data, transform_schema

from ..conftest import NullableListModel


def _field_schema(model: type, field_name: str) -> dict:
    return model.model_json_schema()["properties"][field_name]


def test_schema_anyof_replaced_with_plain_array() -> None:
    """The rewritten schema has no `anyOf` key and `type == "array"`."""
    schema = _field_schema(NullableListModel, "entries")

    transformed = transform_schema(schema, {})

    assert "anyOf" not in transformed
    assert transformed["type"] == "array"


def test_schema_min_items_not_enforced_after_transform() -> None:
    """The rewritten schema has no `minItems`, even though the source did."""
    schema = _field_schema(NullableListModel, "entries")
    assert schema["anyOf"][0]["minItems"] == 2  # sanity check on the fixture

    transformed = transform_schema(schema, {})

    assert "minItems" not in transformed


def test_schema_min_length_hint_preserved_as_advisory() -> None:
    """The rewritten schema carries the original minimum as an advisory hint."""
    schema = _field_schema(NullableListModel, "entries")

    transformed = transform_schema(schema, {})

    assert transformed["x-mimir-min-length-hint"] == 2


def test_schema_no_hint_when_source_has_no_minimum() -> None:
    """A source field with no minimum produces no advisory hint key at all."""

    class NoMinimumModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        entries: list[str] | None = Field(
            default=None,
            json_schema_extra={"x-mimir-adapter": "nullable-list"},
        )

    schema = _field_schema(NoMinimumModel, "entries")
    assert "minItems" not in schema["anyOf"][0]  # sanity check on the fixture

    transformed = transform_schema(schema, {})

    assert "x-mimir-min-length-hint" not in transformed


def test_data_empty_list_converted_to_none() -> None:
    """Submitting an empty list produces `None`."""
    schema = _field_schema(NullableListModel, "entries")

    assert transform_incoming_data(schema, []) is None


def test_data_nonempty_list_passed_through() -> None:
    """Submitting a non-empty list produces the same list, unchanged."""
    schema = _field_schema(NullableListModel, "entries")

    assert transform_incoming_data(schema, ["a", "b"]) == ["a", "b"]


def test_data_none_passed_through() -> None:
    """Submitting `None` produces `None`."""
    schema = _field_schema(NullableListModel, "entries")

    assert transform_incoming_data(schema, None) is None


def test_short_nonempty_list_not_rejected_by_transform() -> None:
    """A list shorter than the real minimum passes through unchanged.

    This transform does not enforce the field's true minimum length; the
    real Pydantic model's own validation is what catches this, out of scope
    for this transform's tests, per IMPLEMENTATION_DETAILS.md's accepted
    trade-off.
    """
    schema = _field_schema(NullableListModel, "entries")

    assert transform_incoming_data(schema, ["only-one"]) == ["only-one"]


def test_alternative_pair_empty_untouched_field_validates() -> None:
    """Leaving one of a nullable-list alternative pair empty still validates.

    Reproduces the scenario IMPLEMENTATION_DETAILS.md describes: a model
    with two nullable-list fields validated as an exactly-one-of pair using
    `is not None`. A user who fills in one field and leaves the other's list
    editor empty submits `[]` for the untouched field. Without the
    empty-list-to-None conversion, that `[]` reads as "provided" to the
    `is not None` check, and validation incorrectly rejects data where
    exactly one alternative was, in fact, provided.
    """

    class AlternativePairModel(BaseModel):
        model_config = ConfigDict(extra="forbid")

        first: list[str] | None = Field(
            default=None,
            min_length=2,
            json_schema_extra={"x-mimir-adapter": "nullable-list"},
        )
        second: list[str] | None = Field(
            default=None,
            min_length=2,
            json_schema_extra={"x-mimir-adapter": "nullable-list"},
        )

        @model_validator(mode="after")
        def _exactly_one_provided(self) -> "AlternativePairModel":
            if (self.first is not None) == (self.second is not None):
                raise ValueError("exactly one of first, second must be provided")
            return self

    first_schema = _field_schema(AlternativePairModel, "first")
    second_schema = _field_schema(AlternativePairModel, "second")

    submitted = {
        "first": transform_incoming_data(first_schema, ["a", "b"]),
        "second": transform_incoming_data(second_schema, []),
    }

    AlternativePairModel.model_validate(submitted)
