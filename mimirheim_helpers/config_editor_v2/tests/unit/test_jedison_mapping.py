"""Unit tests for config_editor_v2.jedison_mapping.

Tests verify:
- The `x-mimir-label` hint becomes Jedison's native `title` key.
- The `x-mimir-group` hint becomes `x-category` on each field carrying it,
  and `x-format` on the parent object schema, set once.
- The `nullable-list` transform's advisory minimum-length hint surfaces in
  Jedison's native `description` key, appended to any existing description
  rather than overwriting it.
- An `x-mimir-` hint this step's mapping does not recognise is left in
  place, not dropped.
- A hint outside the `x-mimir-` namespace, assumed to already be in
  Jedison's own vocabulary, is left untouched.
- The vendored Jedison and Bootstrap license files are present on disk.
"""

from __future__ import annotations

from pathlib import Path

import config_editor_v2
from config_editor_v2.adapter import transform_schema
from config_editor_v2.jedison_mapping import to_jedison_object_schema, to_jedison_schema

from ..conftest import (
    GroupHintModel,
    IdentityAdapterModel,
    LabelHintModel,
    NullableListWithDescriptionModel,
    UnrecognisedMimirHintModel,
)


def _field_schema(model: type, field_name: str) -> dict:
    return model.model_json_schema()["properties"][field_name]


def test_label_hint_becomes_jedison_title() -> None:
    """A field with the label hint produces a `title` key equal to that value."""
    schema = _field_schema(LabelHintModel, "named")

    mapped = to_jedison_schema(schema)

    assert mapped["title"] == "Display Name"


def test_grouping_hint_sets_category_on_field_and_format_on_parent() -> None:
    """Fields sharing a grouping hint get matching `x-category`; the parent
    object schema gains `x-format` set to this step's chosen default."""
    model_schema = GroupHintModel.model_json_schema()

    mapped = to_jedison_object_schema(model_schema)

    assert mapped["properties"]["first"]["x-category"] == "Network"
    assert mapped["properties"]["second"]["x-category"] == "Network"
    assert mapped["x-format"] == "categories-vertical"


def test_ungrouped_field_has_no_category() -> None:
    """A field with no grouping hint gains no `x-category` key at all."""
    model_schema = GroupHintModel.model_json_schema()

    mapped = to_jedison_object_schema(model_schema)

    assert "x-category" not in mapped["properties"]["ungrouped"]


def test_nullable_list_advisory_hint_surfaces_as_description() -> None:
    """A field with the `nullable-list` advisory minimum hint produces a
    schema fragment whose `description` key contains that number."""
    schema = _field_schema(NullableListWithDescriptionModel, "entries")
    transformed = transform_schema(schema)
    assert transformed["x-mimir-min-length-hint"] == 2  # sanity check

    mapped = to_jedison_schema(transformed)

    assert "2" in mapped["description"]


def test_advisory_hint_appended_not_overwriting_existing_description() -> None:
    """A field with both a user-authored description and the advisory
    minimum hint retains the original text with the hint appended."""
    schema = _field_schema(NullableListWithDescriptionModel, "entries")
    transformed = transform_schema(schema)

    mapped = to_jedison_schema(transformed)

    assert "User-provided list of entries." in mapped["description"]
    assert "2" in mapped["description"]


def test_unrecognised_x_mimir_hint_is_left_in_place() -> None:
    """A field with an invented `x-mimir-` key not covered by this step's
    mapping keeps that key, unmodified, in the output."""
    schema = _field_schema(UnrecognisedMimirHintModel, "weird")

    mapped = to_jedison_schema(schema)

    assert mapped["x-mimir-totally-invented-hint"] == "unchanged"


def test_non_x_mimir_hint_untouched() -> None:
    """A non-namespaced hint intended for Jedison directly passes through
    unchanged."""
    schema = _field_schema(IdentityAdapterModel, "tagged")

    mapped = to_jedison_schema(schema)

    assert mapped["someLibraryOption"] is True


def test_vendored_jedison_license_file_present() -> None:
    """`static/vendor/jedison/` contains a license file."""
    vendor_dir = Path(config_editor_v2.__file__).parent / "static" / "vendor" / "jedison"

    assert (vendor_dir / "LICENSE").is_file()


def test_vendored_bootstrap_license_file_present() -> None:
    """`static/vendor/bootstrap/` contains a license file."""
    vendor_dir = Path(config_editor_v2.__file__).parent / "static" / "vendor" / "bootstrap"

    assert (vendor_dir / "LICENSE").is_file()
