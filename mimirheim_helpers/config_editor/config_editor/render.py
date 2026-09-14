"""Turns a Descriptor into the flat, template-ready structure server.py renders.

`build_groups` walks a Descriptor's FormSpec recursively (ticket 08; see
ADR-0006 and ADR-0007), producing a tree of `RenderedGroup`/`RenderedField`/
`RenderedEntry` objects: `RenderedGroup`'s `RenderedField`s cover a section's
Basic (always shown) and Expert (collapsed behind an Advanced disclosure)
fields, already Conditional-Visibility filtered, and a structural
`RenderedField` (nested object, optional object, Named Collection, or Ordered
Collection) carries its own recursively-built `RenderedGroup`/`RenderedEntry`
list rather than a leaf value. `server.py` and its Jinja2 templates only ever
iterate these plain objects, never a Descriptor or FormSpec directly. This
module does no I/O and never writes a Config Owner's configuration file; it
only prepares an already-received Descriptor, plus the current values
`server.py` fetched via `get_current_values`, for read-only display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mimirheim_shared.config_service import Descriptor
from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import evaluate_condition

# Label used for a field whose FieldSpec.group is unset, so every field still
# renders under some visible heading rather than being silently grouped under
# an empty string.
UNGROUPED_LABEL = "General"


def schema_default_values(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Extracts each top-level property's JSON Schema default, if any.

    Used as the base layer `build_groups` merges a Config Owner's actual
    current values on top of: a field genuinely absent from the on-disk
    configuration (never set, relying on its pydantic default) should still
    evaluate the same way a set field would for both display and Conditional
    Visibility. A property with no "default" key (a required field, or one
    built via a pydantic `default_factory`, which pydantic does not emit
    into JSON Schema) is simply absent from the returned dict; `dict.get`
    already treats a missing field as None, which matches the absence of a
    value correctly for those cases too.

    Args:
        json_schema: A Config Owner's `model_json_schema()` output, as carried
            by its Descriptor.

    Returns:
        Dict mapping top-level field name to its schema default, for every
        property that declares one.
    """
    properties = json_schema.get("properties", {})
    return {name: prop["default"] for name, prop in properties.items() if "default" in prop}


def _merge_defaults(schema: dict[str, Any], value: Any) -> dict[str, Any]:
    """Merges a nested object's or collection entry's own values on top of its own schema defaults.

    Used at every level `_build_field` recurses through a NESTED_OBJECT field
    or a NAMED_COLLECTION/ORDERED_COLLECTION entry, so a field with no
    on-disk value still prefills from its own Pydantic default at that
    nesting depth, the same way `build_groups` already does at the top
    level (see `schema_default_values`).

    Args:
        schema: The resolved JSON Schema fragment for this level (a nested
            object's own `field_schema`, or a collection's own resolved
            `item_schema`), whose own "properties" carry the defaults to
            merge in.
        value: The on-disk dict of values already set at this level, or
            anything else (e.g. `None`, when a collection entry has no
            explicit fields set) when nothing is set yet.

    Returns:
        `schema`'s own defaults merged with `value`'s entries, `value`'s
        entries winning on any key present in both.
    """
    return {**schema_default_values(schema), **(value if isinstance(value, dict) else {})}


@dataclass(frozen=True)
class RenderedEntry:
    """One existing entry of a Named Collection (keyed) or Ordered Collection (indexed) field."""

    key: str
    label: str
    groups: list["RenderedGroup"]


@dataclass(frozen=True)
class RenderedField:
    """One FormSpec field, resolved for template rendering.

    Exactly one of the following describes a field, depending on its
    effective Field Shape (`spec.shape`):

    - `value` (and, for ENUM_SELECT, `options`): a scalar or Literal[...]
      leaf field.
    - `nested_groups`: a nested object or optional object field's own
      recursively-built groups. Always built for an optional object, even
      when currently absent (`present` is False): `owner.html` renders it
      inside a `<fieldset disabled>` its Presence Toggle checkbox enables
      live via JS, so a currently-absent section can be populated without a
      page reload, while a disabled fieldset's controls are never submitted.
    - `entries`: a Named Collection or Ordered Collection field, one
      `RenderedEntry` per existing entry.

    Attributes:
        name: The field's full dotted/indexed path (e.g.
            `"batteries.battery_main.capacity_kwh"`), used directly as the
            rendered `<input>`/`<select>`/checkbox `name=` and `id=`
            attribute so a submission can be parsed back into the same
            nested structure (see `config_editor.submission`).
        spec: This field's FieldSpec, as resolved by
            `mimirheim_shared.formspec.resolve_field_shapes` when the Config
            Owner built its Descriptor.
    """

    name: str
    spec: FieldSpec
    value: Any = None
    options: list[Any] | None = None
    nested_groups: list["RenderedGroup"] | None = None
    entries: list[RenderedEntry] | None = None
    present: bool = False
    # Widget kind for a SCALAR leaf field ("text", "checkbox", or "number"),
    # derived from the field's own JSON Schema "type" (see _widget_attrs).
    # ENUM_SELECT and every structural shape ignore this; they render from
    # `options`/`nested_groups`/`entries` instead.
    input_type: str = "text"
    # HTML min/max for a "number" input_type, read from the Pydantic model's
    # own minimum/maximum (ge/le) or exclusiveMinimum/exclusiveMaximum
    # (gt/lt) JSON Schema keywords. None when the model declares no bound.
    min_value: Any = None
    max_value: Any = None
    # HTML step for a "number" input_type: "1" for an integer field, an
    # explicit multipleOf when the model declares one, otherwise "any".
    step: str | None = None


@dataclass(frozen=True)
class RenderedGroup:
    """One group's Basic and Expert fields, both already Conditional-Visibility filtered."""

    label: str
    basic_fields: list[RenderedField] = field(default_factory=list)
    expert_fields: list[RenderedField] = field(default_factory=list)

    @property
    def has_expert_fields(self) -> bool:
        """Whether this group has any Expert-tier field to collapse behind Advanced."""
        return bool(self.expert_fields)


def build_groups(descriptor: Descriptor, values: dict[str, Any] | None = None) -> list[RenderedGroup]:
    """Groups a Descriptor's FormSpec fields by section, tier, and visibility, recursively.

    Args:
        descriptor: The Config Owner's Descriptor to render.
        values: The Config Owner's current values (from `get_current_values`),
            or the Candidate Values a user just submitted when redisplaying a
            failed or successful `validate_and_write` so in-progress edits
            are not lost. Always merged on top of the Descriptor's own JSON
            Schema defaults (see `schema_default_values`) at the top level,
            so a field never explicitly set still evaluates as its pydantic
            default would. `_build_field` repeats this same merge (see
            `_merge_defaults`) at every nested-object and collection-entry
            recursion using that level's own JSON Schema fragment, so a
            field with no on-disk value prefills from its pydantic default
            at any nesting depth. `None` (nothing available yet, e.g. a
            timed-out `get_current_values`) renders from schema defaults
            alone.

    Returns:
        Groups in first-seen order, each with its filtered Basic and Expert
        field lists. A structural field's own nested groups/entries are
        built the same way, recursively, at every depth.
    """
    merged = {**schema_default_values(descriptor.json_schema), **(values or {})}
    defs = descriptor.json_schema.get("$defs", {})
    properties = descriptor.json_schema.get("properties", {})
    return _build_groups(descriptor.form_spec, merged, name_prefix="", properties=properties, defs=defs)


def _build_groups(
    form_spec: FormSpec,
    values: dict[str, Any],
    name_prefix: str,
    properties: dict[str, Any],
    defs: dict[str, Any],
) -> list[RenderedGroup]:
    groups: dict[str, RenderedGroup] = {}
    order: list[str] = []

    for name, spec in form_spec.fields.items():
        if spec.hidden:
            continue
        if spec.visible_if is not None and not evaluate_condition(spec.visible_if, values):
            continue

        label = spec.group or UNGROUPED_LABEL
        if label not in groups:
            groups[label] = RenderedGroup(label=label)
            order.append(label)

        field_name = f"{name_prefix}.{name}" if name_prefix else name
        field_schema = _resolve_schema(properties.get(name, {}), defs)
        rendered = _build_field(field_name, spec, values.get(name), field_schema, defs)
        target = groups[label].expert_fields if spec.tier is Tier.EXPERT else groups[label].basic_fields
        target.append(rendered)

    return [groups[label] for label in order]


def _build_field(
    field_name: str,
    spec: FieldSpec,
    value: Any,
    field_schema: dict[str, Any],
    defs: dict[str, Any],
) -> RenderedField:
    shape = spec.shape or FieldShape.SCALAR

    if shape is FieldShape.NESTED_OBJECT and spec.nested_form_spec is not None:
        nested_properties = field_schema.get("properties", {})
        nested_groups = _build_groups(
            spec.nested_form_spec, _merge_defaults(field_schema, value), field_name, nested_properties, defs
        )
        return RenderedField(name=field_name, spec=spec, nested_groups=nested_groups)

    if shape is FieldShape.OPTIONAL_OBJECT and spec.nested_form_spec is not None:
        present = isinstance(value, dict)
        nested_properties = field_schema.get("properties", {})
        # Always built, even when currently absent: owner.html renders this
        # inside a <fieldset disabled> the Presence Toggle checkbox enables
        # live via JS, so a user can populate a currently-absent optional
        # section without a page reload. A disabled fieldset's descendant
        # controls are never submitted (native HTML behaviour), so an
        # unchecked toggle still keeps nested fields out of the submission.
        nested_groups = _build_groups(
            spec.nested_form_spec, value if present else {}, field_name, nested_properties, defs
        )
        return RenderedField(name=field_name, spec=spec, present=present, nested_groups=nested_groups)

    if shape is FieldShape.NAMED_COLLECTION and spec.nested_form_spec is not None:
        item_schema = _resolve_schema(field_schema.get("additionalProperties", {}), defs)
        item_properties = item_schema.get("properties", {})
        entries = []
        if isinstance(value, dict):
            for key, entry_value in value.items():
                entry_prefix = f"{field_name}.{key}"
                entry_groups = _build_groups(
                    spec.nested_form_spec,
                    _merge_defaults(item_schema, entry_value),
                    entry_prefix,
                    item_properties,
                    defs,
                )
                entries.append(RenderedEntry(key=key, label=key, groups=entry_groups))
        return RenderedField(name=field_name, spec=spec, entries=entries)

    if shape is FieldShape.ORDERED_COLLECTION and spec.nested_form_spec is not None:
        item_schema = _resolve_schema(field_schema.get("items", {}), defs)
        item_properties = item_schema.get("properties", {})
        entries = []
        if isinstance(value, list):
            for index, entry_value in enumerate(value):
                entry_prefix = f"{field_name}.{index}"
                entry_groups = _build_groups(
                    spec.nested_form_spec,
                    _merge_defaults(item_schema, entry_value),
                    entry_prefix,
                    item_properties,
                    defs,
                )
                entries.append(RenderedEntry(key=str(index), label=f"#{index + 1}", groups=entry_groups))
        return RenderedField(name=field_name, spec=spec, entries=entries)

    if shape is FieldShape.ENUM_SELECT:
        options = field_schema.get("enum", [])
        return RenderedField(name=field_name, spec=spec, value=value, options=options)

    # SCALAR, or a Shape Override simplifying a structural field down to it.
    input_type, min_value, max_value, step = _widget_attrs(field_schema)
    return RenderedField(
        name=field_name,
        spec=spec,
        value=value,
        input_type=input_type,
        min_value=min_value,
        max_value=max_value,
        step=step,
    )


def _widget_attrs(field_schema: dict[str, Any]) -> tuple[str, Any, Any, str | None]:
    """Derive a SCALAR field's HTML widget kind and numeric bounds from its JSON Schema.

    Reads the field's own JSON Schema type and, for a numeric type, its
    minimum/maximum (from the Pydantic model's `ge`/`le`) or
    exclusiveMinimum/exclusiveMaximum (from `gt`/`lt`) directly off the
    already-resolved schema fragment, per ADR-0002: a numeric bound is not
    duplicated onto FieldSpec, it is read once from the model's own JSON
    Schema.

    Args:
        field_schema: The field's own resolved JSON Schema fragment (see
            `_resolve_schema`).

    Returns:
        A `(input_type, min_value, max_value, step)` tuple. `input_type` is
        "checkbox" for a boolean field, "number" for an integer or float
        field, otherwise "text" (string, or a shape-overridden field with no
        recognised scalar JSON type). `min_value`/`max_value`/`step` are only
        meaningful when `input_type` is "number".
    """
    json_type = field_schema.get("type")

    if json_type == "boolean":
        return "checkbox", None, None, None

    if json_type == "integer":
        return "number", *_numeric_bounds(field_schema), "1"

    if json_type == "number":
        multiple_of = field_schema.get("multipleOf")
        step = str(multiple_of) if multiple_of is not None else "any"
        return "number", *_numeric_bounds(field_schema), step

    return "text", None, None, None


def _numeric_bounds(field_schema: dict[str, Any]) -> tuple[Any, Any]:
    """Read a numeric field's min/max, preferring an inclusive bound over an exclusive one.

    Args:
        field_schema: The field's own resolved JSON Schema fragment.

    Returns:
        A `(min_value, max_value)` tuple: each is the JSON Schema `minimum`/
        `maximum` keyword (from the Pydantic model's `ge`/`le`) if declared,
        otherwise `exclusiveMinimum`/`exclusiveMaximum` (from `gt`/`lt`) if
        that is declared instead, otherwise `None`. HTML's `min`/`max`
        attributes have no exclusive form, so an exclusive bound is rendered
        as if it were inclusive -- a best-effort hint, not a guarantee; the
        model's own validation remains the source of truth on submit.
    """
    min_value = field_schema.get("minimum", field_schema.get("exclusiveMinimum"))
    max_value = field_schema.get("maximum", field_schema.get("exclusiveMaximum"))
    return min_value, max_value


def _resolve_schema(schema_fragment: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """Follow a `$ref`/`allOf`/nullable-`anyOf` wrapper down to the concrete schema.

    A Config Owner's `model_json_schema()` output wraps a nested-model field
    in one of these forms depending on the pydantic version and whether the
    field is optional; this resolves whichever form is present down to the
    schema that actually carries `"properties"`, `"additionalProperties"`,
    `"items"`, or `"enum"`, so `_build_field` never needs to special-case
    them itself.

    Args:
        schema_fragment: The field's own JSON Schema fragment, e.g.
            `properties[name]` from the enclosing object schema.
        defs: The root schema's `$defs`, which every `$ref` here points into
            regardless of nesting depth.

    Returns:
        The concrete schema fragment, or `{}` if `schema_fragment` was
        already empty (e.g. a field name unknown to the schema).
    """
    if "$ref" in schema_fragment:
        ref_name = schema_fragment["$ref"].rsplit("/", 1)[-1]
        return _resolve_schema(defs.get(ref_name, {}), defs)
    if "allOf" in schema_fragment and len(schema_fragment["allOf"]) == 1:
        return _resolve_schema(schema_fragment["allOf"][0], defs)
    if "anyOf" in schema_fragment:
        for branch in schema_fragment["anyOf"]:
            if branch.get("type") != "null":
                return _resolve_schema(branch, defs)
    return schema_fragment
