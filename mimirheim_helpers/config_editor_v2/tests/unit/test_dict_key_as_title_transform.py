"""Unit tests for config_editor_v2.transforms's `dict-key-as-title` transform.

Tests verify:
- Schema-side: the named-map field's `additionalProperties` `$ref` is
  replaced by the referenced `$defs` entry, inlined, with its `title` key
  removed and every other key preserved.
- The original `$defs` entry itself is left untouched, so another field
  referencing the same item model would still see its title.
- The field's own keys (other than `additionalProperties`) survive
  unchanged.
- Data-side: a submitted value passes through unchanged.
"""

from __future__ import annotations

from config_editor_v2.adapter import transform_incoming_data, transform_schema

from ..conftest import DictKeyAsTitleModel


def _field_schema_and_defs(model: type, field_name: str) -> tuple[dict, dict]:
    schema = model.model_json_schema()
    return schema["properties"][field_name], schema["$defs"]


def test_additional_properties_ref_replaced_with_inlined_item_schema() -> None:
    """The rewritten field's `additionalProperties` has no `$ref` key."""
    field_schema, defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")

    transformed = transform_schema(field_schema, defs)

    assert "$ref" not in transformed["additionalProperties"]
    assert transformed["additionalProperties"]["type"] == "object"


def test_inlined_item_schema_has_no_title() -> None:
    """The inlined item schema's `title` key is removed."""
    field_schema, defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")
    assert defs["NamedMapItemModel"]["title"] == "NamedMapItemModel"  # sanity check

    transformed = transform_schema(field_schema, defs)

    assert "title" not in transformed["additionalProperties"]


def test_inlined_item_schema_preserves_other_keys() -> None:
    """Keys other than `title` on the item schema survive the inlining."""
    field_schema, defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")

    transformed = transform_schema(field_schema, defs)

    assert transformed["additionalProperties"]["properties"] == (
        defs["NamedMapItemModel"]["properties"]
    )


def test_original_defs_entry_is_not_mutated() -> None:
    """The source `$defs` entry keeps its `title`, for any other referrer."""
    field_schema, defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")

    transform_schema(field_schema, defs)

    assert defs["NamedMapItemModel"]["title"] == "NamedMapItemModel"


def test_field_own_keys_are_preserved() -> None:
    """The field's own `x-mimir-adapter` hint survives the rewrite."""
    field_schema, defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")

    transformed = transform_schema(field_schema, defs)

    assert transformed["x-mimir-adapter"] == "dict-key-as-title"


def test_data_passed_through_unchanged() -> None:
    """A submitted map value is not altered by this transform."""
    field_schema, _defs = _field_schema_and_defs(DictKeyAsTitleModel, "entries")

    submitted = {"garage_battery": {"label": "Garage"}}

    assert transform_incoming_data(field_schema, submitted) == submitted
