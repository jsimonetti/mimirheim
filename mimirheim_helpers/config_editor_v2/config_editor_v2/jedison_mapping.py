"""Jedison-native schema derivation for config-editor-v2."""

from __future__ import annotations

from typing import Any


def _mark_add_property_enabled(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Sets `x-objectAdd: True` on a field schema naming an add-button label.

    Args:
        field_schema: A single field's JSON Schema fragment, exactly as it
            appears under some schema's `properties`.

    Returns:
        A copy of `field_schema`. If it carries `x-addPropertyContent`, the
        copy also carries `x-objectAdd: True`. A field with no
        `x-addPropertyContent` key is returned with only its top-level dict
        copied, otherwise unchanged.
    """
    rewritten = dict(field_schema)
    if "x-addPropertyContent" in rewritten:
        rewritten["x-objectAdd"] = True
    return rewritten


def _properties_marked(schema_with_properties: dict[str, Any]) -> dict[str, Any]:
    """Applies `_mark_add_property_enabled` to one schema's own `properties`.

    Used identically for a model's own top-level schema and for each of its
    `$defs` entries -- both are "a schema with a `properties` dict", the
    only shape this function needs to know about. Mirrors
    `adapter.py`'s `_document_properties_transformed`.

    Args:
        schema_with_properties: A schema dict that may or may not have its
            own `properties` key.

    Returns:
        A copy of `schema_with_properties`. If it has a `properties` key,
        every field schema in it is replaced with
        `_mark_add_property_enabled`'s result; every other key is
        unchanged.
    """
    if "properties" not in schema_with_properties:
        return schema_with_properties
    rewritten = dict(schema_with_properties)
    rewritten["properties"] = {
        field_name: _mark_add_property_enabled(field_schema)
        for field_name, field_schema in rewritten["properties"].items()
    }
    return rewritten


def to_jedison_object_schema(model_schema: dict[str, Any]) -> dict[str, Any]:
    """Sets `x-objectAdd: True` on every field naming an add-button label.

    Runs over the model's own top-level `properties` and, separately, over
    every entry in `$defs`, matching `adapter.transform_schema_document`'s
    own two-pass shape: Pydantic's flat `$defs` layout means no field is
    ever nested deeper than that.

    Args:
        model_schema: A JSON Schema document, typically already run through
            `adapter.transform_schema_document`.

    Returns:
        A new schema dict (the input and its `properties`/`$defs` sub-dicts
        are not mutated) with `x-objectAdd: True` added to every field
        schema, at every level, that carries `x-addPropertyContent`. A
        field with no `x-addPropertyContent` key is returned unchanged.
    """
    rewritten = _properties_marked(model_schema)
    if "$defs" in rewritten:
        rewritten["$defs"] = {
            def_name: _properties_marked(def_schema)
            for def_name, def_schema in rewritten["$defs"].items()
        }
    return rewritten
