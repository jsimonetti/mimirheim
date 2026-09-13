"""Turns a Descriptor into the flat, template-ready structure server.py renders.

`build_groups` groups a Descriptor's FormSpec fields by section, splits each
group's fields into Basic (always shown) and Expert (collapsed behind an
Advanced disclosure), and applies Conditional Visibility, so `server.py` and
its Jinja2 templates only ever iterate a plain list of `RenderedGroup`
objects. This module does no I/O and never writes a Config Owner's
configuration file; it only prepares an already-received Descriptor for
read-only display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mimirheim_shared.config_service import Descriptor
from mimirheim_shared.formspec import FieldSpec, Tier
from mimirheim_shared.visibility import evaluate_condition

# Label used for a field whose FieldSpec.group is unset, so every field still
# renders under some visible heading rather than being silently grouped under
# an empty string.
UNGROUPED_LABEL = "General"


def schema_default_values(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Extracts each top-level property's JSON Schema default, if any.

    Ticket 03 renders read-only: there is no per-owner "current on-disk
    values" fetch yet (a later ticket adds submitting edits), so a field's own
    JSON Schema default is the best available stand-in both for the read-only
    display itself and for evaluating Conditional Visibility. A property with
    no "default" key (a required field, or one built via a pydantic
    `default_factory`, which pydantic does not emit into JSON Schema) is
    simply absent from the returned dict; `evaluate_condition` already
    treats a missing field as None via `dict.get`.

    Args:
        json_schema: A Config Owner's `model_json_schema()` output, as carried
            by its Descriptor.

    Returns:
        Dict mapping top-level field name to its schema default, for every
        property that declares one.
    """
    properties = json_schema.get("properties", {})
    return {name: prop["default"] for name, prop in properties.items() if "default" in prop}


@dataclass(frozen=True)
class RenderedField:
    """One FormSpec field, resolved for template rendering."""

    name: str
    spec: FieldSpec
    value: Any


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


def build_groups(descriptor: Descriptor) -> list[RenderedGroup]:
    """Groups a Descriptor's FormSpec fields by section, tier, and visibility.

    Fields are grouped by `FieldSpec.group` (`UNGROUPED_LABEL` when unset), in
    `FormSpec.fields`'s own iteration order. Within a group, Basic fields are
    always shown; Expert fields are returned separately so the caller (the
    Jinja2 template) can render them behind an Advanced disclosure. A field
    marked `hidden` in its FieldSpec, or whose Conditional Visibility rule
    evaluates to False against the Descriptor's schema defaults (see
    `schema_default_values`), is left out of both lists entirely.

    Args:
        descriptor: The Config Owner's Descriptor to render.

    Returns:
        Groups in first-seen order, each with its filtered Basic and Expert
        field lists.
    """
    values = schema_default_values(descriptor.json_schema)
    groups: dict[str, RenderedGroup] = {}
    order: list[str] = []

    for name, spec in descriptor.form_spec.fields.items():
        if spec.hidden:
            continue
        if spec.visible_if is not None and not evaluate_condition(spec.visible_if, values):
            continue

        label = spec.group or UNGROUPED_LABEL
        if label not in groups:
            groups[label] = RenderedGroup(label=label)
            order.append(label)

        rendered = RenderedField(name=name, spec=spec, value=values.get(name))
        target = groups[label].expert_fields if spec.tier is Tier.EXPERT else groups[label].basic_fields
        target.append(rendered)

    return [groups[label] for label in order]
