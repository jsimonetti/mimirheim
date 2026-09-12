"""Unit tests for config_editor_v2.jedison_mapping.

Tests verify:
- A field carrying Jedison's native `x-addPropertyContent` hint gains
  `x-objectAdd: True`.
- A field with neither hint is left unchanged.
- A field nested inside a `$defs` entry is reached, not only a top-level
  field of the model itself.
- The input schema document is not mutated.
"""

from __future__ import annotations

from config_editor_v2.jedison_mapping import to_jedison_object_schema

from ..conftest import AddPropertyContentModel


def test_field_with_add_property_content_gains_object_add() -> None:
    """A field naming an add-button label gets `x-objectAdd: True`."""
    model_schema = AddPropertyContentModel.model_json_schema()

    mapped = to_jedison_object_schema(model_schema)

    assert mapped["properties"]["labelled"]["x-objectAdd"] is True


def test_field_without_add_property_content_is_untouched() -> None:
    """A field naming no add-button label gains no `x-objectAdd` key."""
    model_schema = AddPropertyContentModel.model_json_schema()

    mapped = to_jedison_object_schema(model_schema)

    assert "x-objectAdd" not in mapped["properties"]["unlabelled"]


def test_field_nested_in_defs_entry_is_reached() -> None:
    """A field inside a `$defs` entry also gets `x-objectAdd: True`."""
    model_schema = AddPropertyContentModel.model_json_schema()

    mapped = to_jedison_object_schema(model_schema)

    nested_def = mapped["$defs"]["AddPropertyContentNestedModel"]
    assert nested_def["properties"]["tags"]["x-objectAdd"] is True


def test_input_document_not_mutated() -> None:
    """The original schema document's field fragments are left unmodified."""
    model_schema = AddPropertyContentModel.model_json_schema()

    to_jedison_object_schema(model_schema)

    assert "x-objectAdd" not in model_schema["properties"]["labelled"]
