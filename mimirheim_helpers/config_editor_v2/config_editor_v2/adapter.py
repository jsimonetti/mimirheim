"""Adapter transform dispatch for config-editor-v2.

This module is part of config-editor-v2's rendering-library-agnostic core.
It reads the `x-mimir-adapter` hint from a field's JSON Schema fragment and
routes that field's schema and data through the named transform, with an
unchanged passthrough for fields that carry no such hint.

This module does not know about any specific rendering library and does not
generate JSON Schema itself (a model's own `model_json_schema()` does that).
It implements no concrete transform beyond a trivial identity transform used
to prove the dispatch mechanism works end to end; the `nullable-list`
transform is added in step 71_2. See
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, sections
"The adapter" and "Namespace convention", for the full design.
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
            (model -> rendering library) direction.
        incoming_data: Rewrites a submitted value for the incoming
            (rendering library -> model validation) direction. Must be the
            exact inverse of `schema`'s structural change, per
            IMPLEMENTATION_DETAILS.md's "The adapter" section.
    """

    schema: Callable[[dict[str, Any]], dict[str, Any]]
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


def transform_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Applies the field's x-mimir-adapter transform to its schema fragment.

    A field with no `x-mimir-adapter` key in `json_schema_extra` is returned
    unchanged: all `x-mimir-` keys are left in place for a later stage to
    consume (see step 71_4) and all non-`x-mimir-` keys are passed through
    untouched, per the "Namespace convention" section of
    IMPLEMENTATION_DETAILS.md.

    Args:
        field_schema: The JSON Schema fragment for a single field, as it
            appears under the model's schema `properties`.

    Returns:
        The schema fragment to hand to the rendering library.

    Raises:
        KeyError: If `x-mimir-adapter` names a transform that was never
            registered via `register_transform`.
    """
    transform_name = field_schema.get(_ADAPTER_HINT)
    if transform_name is None:
        return field_schema
    return _TRANSFORMS[transform_name].schema(field_schema)


def transform_incoming_data(field_schema: dict[str, Any], value: Any) -> Any:
    """Applies the field's transform in the submit -> validate direction.

    Must be the exact inverse of `transform_schema`'s structural change, per
    IMPLEMENTATION_DETAILS.md's requirement that data run through the
    transform is acceptable input to the real Pydantic model's validation.

    Args:
        field_schema: The same JSON Schema fragment passed to
            `transform_schema` for this field, used only to read the
            `x-mimir-adapter` hint.
        value: The value submitted by the rendering library for this field.

    Returns:
        The value to hand to the real Pydantic model for validation.

    Raises:
        KeyError: If `x-mimir-adapter` names a transform that was never
            registered via `register_transform`.
    """
    transform_name = field_schema.get(_ADAPTER_HINT)
    if transform_name is None:
        return value
    return _TRANSFORMS[transform_name].incoming_data(value)


def _identity_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    return field_schema


def _identity_incoming_data(value: Any) -> Any:
    return value


# Registered only to prove the dispatch mechanism works end to end before
# step 71_2 builds the first transform with real behaviour ("nullable-list")
# against the same mechanism.
register_transform(
    "identity",
    Transform(schema=_identity_schema, incoming_data=_identity_incoming_data),
)
