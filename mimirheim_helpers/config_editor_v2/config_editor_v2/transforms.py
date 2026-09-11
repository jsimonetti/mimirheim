"""The `nullable-list` adapter transform for config-editor-v2.

This module implements the one concrete adapter transform documented in
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, under "The
`nullable-list` transform": a field typed as either `None` or a non-empty
list, which Pydantic renders as a JSON Schema `anyOf` between an array with
a minimum item count and a null type.

Importing this module registers the transform under the name
"nullable-list" via `config_editor_v2.adapter.register_transform` as a side
effect of import. It exports no other public symbol. This module does not
know about the registry (`registry.py`) or any rendering library; it
operates purely on JSON Schema fragments and plain Python values, per the
adapter's dispatch contract in `adapter.py`.
"""

from __future__ import annotations

from typing import Any

from .adapter import Transform, register_transform

# Advisory-only key carrying the original array minimum length forward for
# optional in-form messaging (e.g. "at least two entries required if any are
# provided"). Never enforced by this transform or written back as the
# rewritten schema's own minItems -- doing so would reintroduce the
# enforcement this transform exists to remove at this layer. See
# mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, section
# "The `nullable-list` transform".
_MIN_LENGTH_HINT_KEY = "x-mimir-min-length-hint"


def _nullable_list_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrites a None-or-non-empty-list `anyOf` into a plain array schema.

    Args:
        field_schema: The field's JSON Schema fragment, containing an
            `anyOf` between an array branch (optionally carrying
            `minItems`) and a null branch.

    Returns:
        A copy of `field_schema` with `anyOf` replaced by a plain
        `{"type": "array", "items": ...}` schema and no `minItems` key. All
        other keys (`title`, `default`, `x-mimir-adapter`, and so on) are
        preserved unchanged. If the array branch declared a `minItems`
        greater than zero, that value is carried forward under the advisory
        `x-mimir-min-length-hint` key instead of being dropped.
    """
    array_branch = next(
        branch for branch in field_schema["anyOf"] if branch.get("type") == "array"
    )
    rewritten = {key: value for key, value in field_schema.items() if key != "anyOf"}
    rewritten["type"] = "array"
    rewritten["items"] = array_branch.get("items", {})

    min_items = array_branch.get("minItems")
    if min_items:
        rewritten[_MIN_LENGTH_HINT_KEY] = min_items

    return rewritten


def _nullable_list_incoming_data(value: Any) -> Any:
    """Converts an empty submitted list back to `None` before validation.

    An untouched list editor submits an empty list, which is indistinguishable
    from "the user explicitly cleared this field" at the data layer. Some
    models pair two nullable-list fields as alternatives validated with
    `is not None`; without this conversion, an empty list reads as "provided"
    to that check. See
    mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, section
    "The `nullable-list` transform".

    Args:
        value: The value submitted by the rendering library for this field:
            `None`, an empty list, or a non-empty list.

    Returns:
        `None` if `value` is an empty list; `value` unchanged otherwise,
        including when it is already `None` or a non-empty list shorter
        than the field's real minimum length (not enforced here).
    """
    if value == []:
        return None
    return value


register_transform(
    "nullable-list",
    Transform(schema=_nullable_list_schema, incoming_data=_nullable_list_incoming_data),
)
