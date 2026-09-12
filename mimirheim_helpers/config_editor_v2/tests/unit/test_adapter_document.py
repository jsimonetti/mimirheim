"""Unit tests for config_editor_v2.adapter's whole-document entry points.

`transform_schema` and `transform_incoming_data` only ever see one field's
schema fragment in isolation. `transform_schema_document` and
`transform_value_document` are what actually reach a field nested inside a
sub-model, a named-map (dict-typed) field's item model, or a list's items.
Tests verify each of those three nesting shapes is reached, using a
temporarily monkeypatched transform (as in test_adapter_dispatch.py) so no
production transform needs to exist for these tests to be meaningful.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from config_editor_v2.adapter import (
    Transform,
    _TRANSFORMS,
    transform_schema_document,
    transform_value_document,
)


class InnerModel(BaseModel):
    """A sub-model nested one level below a document's top-level fields."""

    model_config = ConfigDict(extra="forbid")

    tagged: str = Field(default="default", json_schema_extra={"x-mimir-adapter": "tag"})


class DocumentModel(BaseModel):
    """A fixture model exercising every nesting shape the document walk reaches.

    `nested` is a sub-model field (`$ref`), `named_map` is a dict-typed field
    whose item model is `InnerModel` (`additionalProperties`), and `items`
    is a list of `InnerModel` (`items`).
    """

    model_config = ConfigDict(extra="forbid")

    nested: InnerModel = Field(default_factory=InnerModel)
    named_map: dict[str, InnerModel] = Field(default_factory=dict)
    items: list[InnerModel] = Field(default_factory=list)


@pytest.fixture(autouse=True)
def _tag_transform(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registers a transform that tags whatever schema/value passes through it."""
    monkeypatch.setitem(
        _TRANSFORMS,
        "tag",
        Transform(
            schema=lambda field_schema, defs: {**field_schema, "x-mimir-test-tag": "dispatched"},
            incoming_data=lambda value: f"tagged:{value}",
        ),
    )


def test_transform_schema_document_reaches_a_ref_nested_field() -> None:
    """A field nested inside a $ref sub-model, in $defs, is transformed."""
    schema = DocumentModel.model_json_schema()

    transformed = transform_schema_document(schema)

    assert transformed["$defs"]["InnerModel"]["properties"]["tagged"]["x-mimir-test-tag"] == (
        "dispatched"
    )


def test_transform_value_document_reaches_a_ref_nested_field() -> None:
    """A submitted value nested inside a $ref sub-model is transformed."""
    schema = DocumentModel.model_json_schema()

    value = transform_value_document(
        schema["properties"]["nested"], {"tagged": "x"}, schema["$defs"]
    )

    assert value == {"tagged": "tagged:x"}


def test_transform_value_document_reaches_a_named_map_entry() -> None:
    """A submitted value inside a named-map (dict-typed) field's entry is transformed."""
    schema = DocumentModel.model_json_schema()

    value = transform_value_document(
        schema["properties"]["named_map"], {"entry_one": {"tagged": "x"}}, schema["$defs"]
    )

    assert value == {"entry_one": {"tagged": "tagged:x"}}


def test_transform_value_document_reaches_a_list_item() -> None:
    """A submitted value inside a list field's item is transformed."""
    schema = DocumentModel.model_json_schema()

    value = transform_value_document(
        schema["properties"]["items"], [{"tagged": "x"}], schema["$defs"]
    )

    assert value == [{"tagged": "tagged:x"}]


def test_transform_value_document_short_circuits_on_none() -> None:
    """A None value is never descended into or transformed against a child schema."""
    schema = DocumentModel.model_json_schema()

    value = transform_value_document(schema["properties"]["nested"], None, schema["$defs"])

    assert value is None
