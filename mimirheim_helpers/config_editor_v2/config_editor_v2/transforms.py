"""Adapter transforms for config-editor-v2.

This module implements three concrete adapter transforms: `nullable-list` (a
field typed as either `None` or a non-empty list), `nullable-scalar` (a
field typed as either `None` or a plain string/number/integer), and
`dict-key-as-title` (a named-map field whose per-entry tab or section label
should be the entry's own key, not its item model's class-derived title).

The two nullable transforms collapse a JSON Schema `anyOf` between a
concrete type and a null type into a plain, non-`anyOf` schema, because
Jedison has no built-in notion of "optional single-typed value" -- left
alone, it renders any two-branch `anyOf` as a generic type-switcher control
that is genuinely unusable for this shape: both branches end up labelled
identically (from this field's own title, which Jedison merges into every
branch that lacks its own), so the switcher shows two indistinguishable
options with no indication of what either one does.

`dict-key-as-title` addresses a different problem: Jedison's map-editor
widgets (including the `nav-horizontal` tab strip) label each entry with its
item schema's own `title` if one is present, falling back to the entry's
dict key only when the item schema has no title at all. Every `$defs` entry
`model_json_schema()` produces carries a `title` by default (the Pydantic
class name, e.g. `"BatteryConfig"`), so every entry in every named-map field
ends up labelled with that fixed class name instead of the name the user
actually gave it (`"garage_battery"`, and so on) -- the one piece of
information that actually distinguishes one entry from another. This
transform never fires on a whole model's schema; it only ever fires on a
field that names it, so it does not remove `title` from a `$defs` entry that
some other field might reasonably want to keep.

None of these transforms fire on their own. A field opts in explicitly by
setting `x-mimir-adapter: "nullable-list"`, `"nullable-scalar"`, or
`"dict-key-as-title"` in its own `json_schema_extra`; see `adapter.py`'s
module docstring for why this project does not auto-detect a transform by
shape. Registering a transform here makes it available to any field that
names it -- it does not apply it to every field of a matching shape.

Importing this module registers all three transforms via
`config_editor_v2.adapter.register_transform` as a side effect of import. It
exports no other public symbol. This module does not know about the
registry (`registry.py`) or any rendering library; it operates purely on
JSON Schema fragments and plain Python values, per the adapter's dispatch
contract in `adapter.py`.
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
# "The adapter".
_MIN_LENGTH_HINT_KEY = "x-mimir-min-length-hint"


def _nullable_non_null_branch(field_schema: dict[str, Any]) -> dict[str, Any] | None:
    """Returns the non-null branch of a two-member `anyOf: [X, null]` pair.

    Args:
        field_schema: A field's JSON Schema fragment, as it appears before
            any transform has run.

    Returns:
        The other branch's schema fragment if `field_schema["anyOf"]` is
        exactly a two-member list with one member equal to
        `{"type": "null"}`; `None` if `field_schema` carries no `anyOf` at
        all, or an `anyOf` of any other shape.
    """
    any_of = field_schema.get("anyOf")
    if not (isinstance(any_of, list) and len(any_of) == 2):
        return None
    null_branch = {"type": "null"}
    null_branches = [branch for branch in any_of if branch == null_branch]
    if len(null_branches) != 1:
        return None
    return next(branch for branch in any_of if branch != null_branch)


def _nullable_list_schema(field_schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Rewrites a None-or-non-empty-list `anyOf` into a plain array schema.

    Args:
        field_schema: The field's JSON Schema fragment, containing an
            `anyOf` between an array branch (optionally carrying
            `minItems`) and a null branch.
        defs: The full document's `$defs` map. Unused: this transform never
            resolves a `$ref`, it only rewrites its own field's fragment.

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
    to that check.

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


def _nullable_scalar_schema(field_schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Rewrites a None-or-plain-scalar `anyOf` into a plain scalar schema.

    Args:
        field_schema: The field's JSON Schema fragment, containing an
            `anyOf` between a string/number/integer branch and a null
            branch.
        defs: The full document's `$defs` map. Unused: this transform never
            resolves a `$ref`, it only rewrites its own field's fragment.

    Returns:
        A copy of `field_schema` with `anyOf` replaced by the non-null
        branch's own keys (`type`, and anything else Pydantic placed on that
        branch specifically, such as `minimum` or `format`). All other keys
        already on `field_schema` (`title`, `description`, and so on) are
        preserved. A `default` key equal to `None` is removed entirely
        rather than carried forward: once the rewritten schema is no longer
        nullable, `None` is not a value Jedison's own scalar editors accept
        as a default -- the rewritten schema's absence of a default is what
        makes the field render blank, which is this transform's equivalent
        of "unset".
    """
    non_null_branch = _nullable_non_null_branch(field_schema)
    if non_null_branch is None:
        raise ValueError("nullable-scalar requires a two-member anyOf: [X, null]")
    rewritten = {key: value for key, value in field_schema.items() if key != "anyOf"}
    rewritten.update(non_null_branch)
    if rewritten.get("default", "_MIMIR_NO_DEFAULT") is None:
        del rewritten["default"]
    return rewritten


def _nullable_scalar_incoming_data(value: Any) -> Any:
    """Converts an empty submitted string back to `None` before validation.

    Jedison's default string editor always coerces its input to a string
    (`String(value)`), so a cleared text input submits `""`, not `None`.
    Jedison's default number editor already converts a cleared input to
    `None` on its own, so a number field never needs this conversion in
    practice -- this function still passes `None` and any non-empty value
    through unchanged, so it is correct for both cases without needing to
    know which scalar type it is handling.

    Args:
        value: The value submitted by the rendering library for this field:
            `None`, `""`, or a non-empty/non-blank value of the field's real
            type.

    Returns:
        `None` if `value` is `""`; `value` unchanged otherwise.
    """
    if value == "":
        return None
    return value


def _dict_key_as_title_schema(field_schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Inlines the item schema of a named-map field with its `title` removed.

    Args:
        field_schema: The named-map field's own JSON Schema fragment,
            containing an `additionalProperties` that is a `$ref` to a
            `$defs` entry.
        defs: The full document's `$defs` map, used to resolve that `$ref`.

    Returns:
        A copy of `field_schema` whose `additionalProperties` is the
        referenced `$defs` entry, inlined and copied, with its `title` key
        removed and every other key unchanged. The original `$defs` entry
        itself is not modified, so any other field that also references it
        keeps its title. All other keys already on `field_schema` (the
        field's own `title`, `x-format`, and so on) are preserved unchanged.
    """
    ref = field_schema["additionalProperties"]["$ref"]
    item_schema = defs[ref.rsplit("/", 1)[-1]]
    inlined_item_schema = {key: value for key, value in item_schema.items() if key != "title"}
    rewritten = dict(field_schema)
    rewritten["additionalProperties"] = inlined_item_schema
    return rewritten


def _dict_key_as_title_incoming_data(value: Any) -> Any:
    """Passes a named-map field's submitted value through unchanged.

    `dict-key-as-title` only changes how the map's entries are labelled in
    the rendering library; it does not change the shape of the submitted
    data, which still validates against the original item model unchanged.

    Args:
        value: The value submitted by the rendering library for this field.

    Returns:
        `value`, unchanged.
    """
    return value


register_transform(
    "nullable-list",
    Transform(schema=_nullable_list_schema, incoming_data=_nullable_list_incoming_data),
)

register_transform(
    "nullable-scalar",
    Transform(schema=_nullable_scalar_schema, incoming_data=_nullable_scalar_incoming_data),
)

register_transform(
    "dict-key-as-title",
    Transform(
        schema=_dict_key_as_title_schema, incoming_data=_dict_key_as_title_incoming_data
    ),
)
