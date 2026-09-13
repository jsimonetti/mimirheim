"""Parses a submitted form's dotted/indexed field names back into nested Candidate Values.

`render.py` names every rendered `<input>`/`<select>`/checkbox after its full
dotted/indexed path (e.g. `"batteries.battery_main.capacity_kwh"`,
`"charge_segments.0.power_max_kw"`) so a plain HTML form POST -- no JSON
payload, no client-side JSON-Schema-to-form library (ADR-0003) -- can express
arbitrarily nested structure. `parse_submission` is the inverse: given a
Descriptor's FormSpec and the flat `{name: value}` dict `server.py` parses a
POST body into, it rebuilds the nested dict/list a Config Owner's
`validate_and_write` expects as Candidate Values.

This module does no I/O and never talks to a Config Owner itself; it only
transforms an already-parsed form body.
"""

from __future__ import annotations

from typing import Any

from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FormSpec


def parse_submission(form_spec: FormSpec, raw: dict[str, str], name_prefix: str = "") -> dict[str, Any]:
    """Rebuild nested Candidate Values from a flat, dotted/indexed submitted form.

    A field absent from `raw` (and, for a structural field, with no
    dotted-deeper key under its own prefix present either) is left out of
    the returned dict entirely rather than set to `None` or `{}`: it was
    never rendered at all, whether because it is conditionally hidden, an
    Ordered/Named Collection with no existing entries, or an optional object
    currently absent, and the submission must not be read as an explicit
    instruction to clear it. This mirrors `overlay_values`
    (`mimirheim_shared.atomic_write`), which only touches the keys present
    in the values handed to it.

    Args:
        form_spec: The Descriptor's FormSpec describing the fields that were
            rendered.
        raw: The flat `{dotted_name: submitted_value}` dict parsed from the
            POST body (`server.py`'s `_parse_form_body`).
        name_prefix: The dotted path already consumed by the caller when
            recursing (e.g. `"batteries.battery_main"` while parsing that
            entry's own fields). Empty at the top level.

    Returns:
        The nested dict of Candidate Values this level of `form_spec`
        describes.
    """
    result: dict[str, Any] = {}

    for name, spec in form_spec.fields.items():
        if spec.hidden:
            continue

        field_name = f"{name_prefix}.{name}" if name_prefix else name
        shape = spec.shape or FieldShape.SCALAR

        if shape is FieldShape.NESTED_OBJECT and spec.nested_form_spec is not None:
            nested = parse_submission(spec.nested_form_spec, raw, field_name)
            if nested:
                result[name] = nested
            continue

        if shape is FieldShape.OPTIONAL_OBJECT and spec.nested_form_spec is not None:
            nested = parse_submission(spec.nested_form_spec, raw, field_name)
            if nested:
                result[name] = nested
            continue

        if shape is FieldShape.NAMED_COLLECTION and spec.nested_form_spec is not None:
            entries: dict[str, Any] = {}
            for key in sorted(_immediate_children(raw, field_name)):
                entries[key] = parse_submission(spec.nested_form_spec, raw, f"{field_name}.{key}")
            if entries:
                result[name] = entries
            continue

        if shape is FieldShape.ORDERED_COLLECTION and spec.nested_form_spec is not None:
            indices = sorted(int(child) for child in _immediate_children(raw, field_name))
            rows = [
                parse_submission(spec.nested_form_spec, raw, f"{field_name}.{index}")
                for index in indices
            ]
            if rows:
                result[name] = rows
            continue

        # SCALAR or ENUM_SELECT: a leaf field, submitted as a single value at
        # its own exact dotted path.
        if field_name in raw:
            result[name] = raw[field_name]

    return result


def _immediate_children(raw: dict[str, str], prefix: str) -> set[str]:
    """Return the set of immediate next path segments under a dotted `prefix`.

    E.g. for `prefix="batteries"` and `raw` containing
    `"batteries.battery_main.capacity_kwh"`, returns `{"battery_main"}` --
    one entry per existing Named Collection key or Ordered Collection index,
    regardless of how many of that entry's own fields were submitted.

    Args:
        raw: The flat submitted form dict.
        prefix: The dotted path whose immediate children to collect.

    Returns:
        The set of distinct next path segments found under `prefix`.
    """
    marker = f"{prefix}."
    children = set()
    for key in raw:
        if key.startswith(marker):
            children.add(key[len(marker) :].split(".", 1)[0])
    return children
