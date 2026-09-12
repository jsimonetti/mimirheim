"""Adapter transform dispatch for config-editor-v2.

This module is part of config-editor-v2's rendering-library-agnostic core.
It reads the `x-mimir-adapter` hint from a field's JSON Schema fragment and
routes that field's schema and data through the named transform, with an
unchanged passthrough for fields that carry no such hint. Selection is
always explicit: a field opts into a transform by setting `x-mimir-adapter`
itself. There is no shape-based auto-detection -- nothing here ever fires
without a field naming it directly, per this project's "no automatic
transforms" rule.

This module does not know about any specific rendering library and does not
generate JSON Schema itself (a model's own `model_json_schema()` does that).
It implements no concrete transform of its own; a transform is registered by
whatever code needs one, via `register_transform`, and only takes effect for
a field that names it explicitly. See
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, sections "The
adapter" and "Namespace convention", for the full design.

Beyond per-field dispatch, this module also provides the whole-document
entry points `transform_schema_document` and `transform_value_document`.
Every registered model's schema places every sub-model it uses -- however
deeply nested -- as a flat entry in that schema's own `$defs` (this is
`model_json_schema()`'s own behaviour, not something this project
constructs); the single-field functions above only ever see one field
fragment at a time and have no way to reach a field nested inside a
sub-model on their own. The whole-document functions exist to actually
reach those fields: `transform_schema_document` runs `transform_schema`
over a model's own top-level properties and, separately, over every
`$defs` entry's own properties -- exactly two flat passes, not an
open-ended recursive walk, because Pydantic's flat `$defs` layout means no
field is ever nested deeper than that. `transform_value_document` walks
actual submitted data, which carries no `$ref` pointers of its own, so it
resolves `$ref`/`anyOf` against the schema as it descends into
`properties`, `additionalProperties`, and `items`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_ADAPTER_HINT = "x-mimir-adapter"


@dataclass(frozen=True)
class Transform:
    """A named pair of schema and data transform functions.

    Attributes:
        schema: Rewrites a field's JSON Schema fragment for the outgoing
            (model -> rendering library) direction. Receives the full
            document's `$defs` map alongside the field's own fragment, so a
            transform that needs to resolve a `$ref` (for example, one that
            inlines another field's or `$defs` entry's schema) can do so
            without a separate, document-scoped dispatch mechanism. A
            transform that only ever rewrites its own field's fragment (such
            as `nullable-list`) is free to ignore this second argument.
        incoming_data: Rewrites a submitted value for the incoming
            (rendering library -> model validation) direction. Must be the
            exact inverse of `schema`'s structural change, per
            IMPLEMENTATION_DETAILS.md's "The adapter" section.
    """

    schema: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    incoming_data: Callable[[Any], Any]


_TRANSFORMS: dict[str, Transform] = {}


def register_transform(name: str, transform: Transform) -> None:
    """Adds a transform to the dispatch table under a chosen name.

    Args:
        name: The value a field's `x-mimir-adapter` hint must carry to be
            routed to this transform.
        transform: The schema and data transform functions to register.
    """
    _TRANSFORMS[name] = transform


def _resolve_transform_name(field_schema: dict[str, Any]) -> str | None:
    """Finds which registered transform, if any, applies to this field.

    A field is only ever routed to a transform by its own explicit
    `x-mimir-adapter` hint; there is no shape-based fallback.

    Args:
        field_schema: The field's raw JSON Schema fragment.

    Returns:
        The transform name to dispatch to, or `None` if the field carries no
        `x-mimir-adapter` hint.
    """
    return field_schema.get(_ADAPTER_HINT)


def transform_schema(field_schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Applies the field's resolved transform to its schema fragment.

    A field is routed to a transform only by an explicit `x-mimir-adapter`
    hint. A field carrying no such hint is returned unchanged: all
    `x-mimir-` keys are left in place for a later stage to consume and all
    non-`x-mimir-` keys are passed through untouched, per the "Namespace
    convention" section of IMPLEMENTATION_DETAILS.md.

    Args:
        field_schema: The JSON Schema fragment for a single field, as it
            appears under the model's schema `properties`.
        defs: The full document's `$defs` map, passed to the resolved
            transform's `schema` function so it can resolve a `$ref` if it
            needs to. Ignored for a field with no `x-mimir-adapter` hint.

    Returns:
        The schema fragment to hand to the rendering library.

    Raises:
        KeyError: If `x-mimir-adapter` names a transform that was never
            registered via `register_transform`.
    """
    transform_name = _resolve_transform_name(field_schema)
    if transform_name is None:
        return field_schema
    return _TRANSFORMS[transform_name].schema(field_schema, defs)


def transform_incoming_data(field_schema: dict[str, Any], value: Any) -> Any:
    """Applies the field's transform in the submit -> validate direction.

    Must be the exact inverse of `transform_schema`'s structural change, per
    IMPLEMENTATION_DETAILS.md's requirement that data run through the
    transform is acceptable input to the real Pydantic model's validation.

    Args:
        field_schema: The same JSON Schema fragment passed to
            `transform_schema` for this field, used only to resolve which
            transform (if any) applies.
        value: The value submitted by the rendering library for this field.

    Returns:
        The value to hand to the real Pydantic model for validation.

    Raises:
        KeyError: If `x-mimir-adapter` names a transform that was never
            registered via `register_transform`.
    """
    transform_name = _resolve_transform_name(field_schema)
    if transform_name is None:
        return value
    return _TRANSFORMS[transform_name].incoming_data(value)


def _document_properties_transformed(
    schema_with_properties: dict[str, Any], defs: dict[str, Any]
) -> dict[str, Any]:
    """Runs `transform_schema` over one schema's own `properties`, if any.

    Used identically for a registered model's own top-level schema and for
    each of its `$defs` entries -- both are "a schema with a `properties`
    dict", the only shape this function needs to know about.

    Args:
        schema_with_properties: A schema dict that may or may not have its
            own `properties` key (a `$defs` entry for a model with no
            fields of its own, though none exist in this project today,
            would have none).
        defs: The full document's `$defs` map, threaded through to
            `transform_schema` for every field in `schema_with_properties`.

    Returns:
        A copy of `schema_with_properties`. If it has a `properties` key,
        every field schema in it is replaced with `transform_schema`'s
        result; every other key is unchanged.
    """
    if "properties" not in schema_with_properties:
        return schema_with_properties
    rewritten = dict(schema_with_properties)
    rewritten["properties"] = {
        field_name: transform_schema(field_schema, defs)
        for field_name, field_schema in rewritten["properties"].items()
    }
    return rewritten


def transform_schema_document(model_schema: dict[str, Any]) -> dict[str, Any]:
    """Applies `transform_schema` to every field anywhere in a model's schema.

    Runs over the model's own top-level `properties` and, separately, over
    every entry in `$defs` -- every sub-model this model uses, however
    deeply it is nested in the original Python type graph. No further
    recursion is needed: `model_json_schema()` always hoists every
    referenced sub-model into this one flat `$defs` map, so "top-level
    properties, plus each `$defs` entry's own properties" already reaches
    every field in the document.

    Every field, wherever it appears, is transformed against the same,
    whole document's `$defs` map -- a field nested inside a `$defs` entry is
    not limited to resolving references relative to that entry alone, since
    `model_json_schema()`'s flat layout means every `$ref` anywhere in the
    document already resolves against this one shared map.

    Args:
        model_schema: The full JSON Schema produced by a Pydantic model's
            `model_json_schema()`.

    Returns:
        A new schema dict (the input and its `properties`/`$defs`
        sub-dicts are not mutated) with every field, at every level,
        transformed.
    """
    defs = model_schema.get("$defs", {})
    rewritten = _document_properties_transformed(model_schema, defs)
    if "$defs" in rewritten:
        rewritten["$defs"] = {
            def_name: _document_properties_transformed(def_schema, defs)
            for def_name, def_schema in rewritten["$defs"].items()
        }
    return rewritten


def _resolve_for_recursion(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follows `$ref` and a null-pair `anyOf` down to a concrete schema.

    Used only to decide how to recurse into a value's children (via
    `properties`, `additionalProperties`, or `items`); the field's own
    transform is always applied afterwards against its original,
    unresolved schema fragment (see `transform_value_document`), not
    against this function's result.

    Every `$ref` in this project's registered models points directly at a
    `$defs` entry (per `model_json_schema()`'s flat layout -- see
    `transform_schema_document`), and every `anyOf` is exactly a two-member
    `[X, {"type": "null"}]` pair (confirmed against every currently
    registered model; see IMPLEMENTATION_DETAILS.md's "Editor and option
    selection reference"). Repeatedly resolving one, then the other, until
    neither applies, therefore always terminates.

    Args:
        schema: The schema fragment to resolve.
        defs: The full document's `$defs` map, used to resolve `$ref`.

    Returns:
        A concrete schema fragment: one with no `$ref` and no `anyOf` of the
        shape described above. May still have neither `properties`,
        `additionalProperties`, nor `items` (a scalar leaf).
    """
    current = schema
    while True:
        ref = current.get("$ref")
        if ref is not None:
            current = defs.get(ref.rsplit("/", 1)[-1], {})
            continue
        any_of = current.get("anyOf")
        if isinstance(any_of, list) and len(any_of) == 2:
            null_branches = [branch for branch in any_of if branch == {"type": "null"}]
            if len(null_branches) == 1:
                current = next(branch for branch in any_of if branch != {"type": "null"})
                continue
        return current


def transform_value_document(
    field_schema: dict[str, Any], value: Any, defs: dict[str, Any]
) -> Any:
    """Applies `transform_incoming_data` to a value and everything nested in it.

    A submitted value carries no schema pointers of its own, so this
    function walks the schema in step with the data: for a dict matching an
    object schema's `properties`, each key's value is transformed against
    that key's own field schema; for a dict matching a schema whose
    `additionalProperties` is itself a schema (a named-map / dict-typed
    field such as `batteries`), each entry's value is transformed against
    that shared item schema; for a list matching an array schema, each
    element is transformed against `items`. Every level's *own* transform
    (whichever explicit `x-mimir-adapter` hint it carries, if any) is
    applied last, against that level's original, unresolved schema
    fragment -- children are always transformed bottom-up before their
    parent's own hint is considered.

    Args:
        field_schema: This value's own JSON Schema fragment, exactly as it
            appears in the model's schema (before `$ref`/`anyOf` resolution).
        value: The submitted value for this field: a plain scalar, `None`,
            a dict, or a list.
        defs: The full document's `$defs` map, used to resolve `$ref` while
            descending into children.

    Returns:
        `value` with every applicable transform applied, at every level.
    """
    if value is not None:
        concrete = _resolve_for_recursion(field_schema, defs)
        properties = concrete.get("properties")
        additional_properties = concrete.get("additionalProperties")
        items_schema = concrete.get("items")
        if isinstance(value, dict) and isinstance(properties, dict):
            value = {
                name: transform_value_document(properties.get(name, {}), child, defs)
                for name, child in value.items()
            }
        elif isinstance(value, dict) and isinstance(additional_properties, dict):
            value = {
                name: transform_value_document(additional_properties, child, defs)
                for name, child in value.items()
            }
        elif isinstance(value, list) and isinstance(items_schema, dict):
            value = [transform_value_document(items_schema, item, defs) for item in value]
    return transform_incoming_data(field_schema, value)
