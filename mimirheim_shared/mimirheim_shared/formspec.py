"""FormSpec: the presentation-only counterpart to a Config Owner's validation model.

A Config Owner's pydantic model stays pure (validation rules only). Every
field it wants shown in the Config Editor gets a matching ``FieldSpec`` entry
in a ``FormSpec``, carrying the label, description, help text, Tier, and
grouping the editor renders. A ``FormSpec`` carries no validation logic of
its own; see ``mimirheim_shared.alignment`` for the utility that checks a
``FormSpec`` and its model stay in sync.

A field's Field Shape (scalar, nested object, Named Collection, Ordered
Collection, optional object, or enum select) is derived from the model, not
hand-authored on ``FieldSpec``; see ``mimirheim_shared.field_shape``
(ADR-0006). ``FieldSpec.nested_form_spec`` is how a non-scalar field's own
FormSpec is attached for recursion, and ``FieldSpec.shape_override`` is how a
field is deliberately rendered simpler than its derived shape.
``resolve_field_shapes`` (below) is how a Config Owner's ``build_descriptor``
step attaches each field's already-derived, effective Field Shape onto
``FieldSpec.shape`` before the FormSpec crosses the MQTT boundary: the Config
Editor never imports a Config Owner's validation model (ADR-0001), so it has
no other way to know a field's shape.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from mimirheim_shared.field_shape import (
    FieldShape,
    derive_field_shape,
    effective_field_shape,
    nested_model_of,
)
from mimirheim_shared.visibility import Condition

# Field Shapes with a nested model to recurse into. A Shape Override can only
# simplify a field down to SCALAR (see field_shape.effective_field_shape), so
# an effective shape outside this set never has structure to resolve further.
_STRUCTURAL_SHAPES = frozenset(
    {
        FieldShape.NESTED_OBJECT,
        FieldShape.OPTIONAL_OBJECT,
        FieldShape.NAMED_COLLECTION,
        FieldShape.ORDERED_COLLECTION,
    }
)


class Tier(str, Enum):
    """A field's default visibility: shown immediately, or behind Advanced.

    BASIC fields are shown by default. EXPERT fields are hidden behind a
    per-section Advanced disclosure until the user opts to expand it.
    """

    BASIC = "basic"
    EXPERT = "expert"


class FieldSpec(BaseModel):
    """Presentation metadata for a single field of a Config Owner's model."""

    model_config = ConfigDict(extra="forbid")

    label: str
    description: str
    help_text: str | None = None
    doc_link: str | None = None
    tier: Tier = Tier.BASIC
    group: str | None = None
    visible_if: Condition | None = None
    # Marks a field as intentionally left out of the rendered form (e.g. an
    # internal-only field) while still counting as "represented" for the
    # spec/model alignment assertion. See mimirheim_shared.alignment.
    hidden: bool = False
    # Renders the field with less structure than its derived Field Shape has
    # (e.g. a nested object as opaque raw text). One-directional: enforced by
    # mimirheim_shared.field_shape.effective_field_shape, never checked here,
    # since a FieldSpec alone does not know its field's derived shape.
    shape_override: FieldShape | None = None
    # Per-value display labels for an ENUM_SELECT (Literal[...]) field, keyed
    # by the literal's own string form. A value with no entry here falls back
    # to its raw literal value; see option_label().
    option_labels: dict[str, str] | None = None
    # A hint value shown as static help text next to a scalar field (str,
    # int, float, bool, or ENUM_SELECT), distinct from the field's own
    # schema default: it never pre-fills the input and never overwrites an
    # on-disk value already there. Meaningless on a structural Field Shape
    # (NESTED_OBJECT, OPTIONAL_OBJECT, NAMED_COLLECTION, ORDERED_COLLECTION),
    # so the Config Editor only renders it for a scalar leaf field.
    suggested_value: Any | None = None
    # The top-level navigable pane this field belongs to. Unset falls back to
    # the default "General" tab (mimirheim_shared.CONTEXT.md's Tab entry),
    # orthogonal to `group`: `group` still renders flat headings within
    # whichever Tab/Subtab pane a field lands in.
    tab: str | None = None
    # A second, nested navigation level scoped within this field's Tab. Same
    # "General" fallback as `tab` when unset. The Tab/Subtab hierarchy is
    # exactly two levels deep; there is no third level.
    subtab: str | None = None
    # The nested model's own FormSpec, for a field whose derived (or
    # overridden) Field Shape is NESTED_OBJECT, OPTIONAL_OBJECT,
    # NAMED_COLLECTION, or ORDERED_COLLECTION. Authored once by the package
    # that defines the nested model and referenced here, never duplicated
    # (ADR-0007).
    nested_form_spec: "FormSpec | None" = None
    # The field's effective Field Shape (derived from the model, resolved
    # through any Shape Override). Never hand-authored: left None on every
    # FieldSpec an author writes, and filled in by resolve_field_shapes when
    # a Config Owner's build_descriptor step assembles the Descriptor it
    # publishes, since that is the one place both the model and the FormSpec
    # are available together.
    shape: FieldShape | None = None


class FormSpec(BaseModel):
    """The full set of FieldSpecs for one Config Owner's validation model."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, FieldSpec]


FieldSpec.model_rebuild()


def option_label(field_spec: FieldSpec, value: Any) -> str:
    """Return an ENUM_SELECT field's display label for one of its literal values.

    Args:
        field_spec: The FieldSpec for the Literal[...] field.
        value: One of the field's literal values.

    Returns:
        The configured label from ``field_spec.option_labels``, or ``str(value)``
        when no label was supplied for it.
    """
    if field_spec.option_labels is not None:
        label = field_spec.option_labels.get(str(value))
        if label is not None:
            return label
    return str(value)


def resolve_field_shapes(model: type[BaseModel], form_spec: FormSpec) -> FormSpec:
    """Return a copy of ``form_spec`` with every field's effective Field Shape attached.

    Walks ``form_spec`` alongside ``model``, recursing into a nested
    FormSpec (and its own nested model) wherever a field's effective shape
    is structural (nested object, optional object, Named Collection, or
    Ordered Collection). Called once by ``build_descriptor`` when a Config
    Owner assembles the Descriptor it publishes: the Config Owner is the
    only place that has both the validation model and the FormSpec at hand,
    since a Config Editor never imports a Config Owner's model (ADR-0001).

    Args:
        model: The pydantic model ``form_spec`` describes.
        form_spec: The hand-authored FormSpec to resolve shapes onto. Not
            mutated; a new FormSpec is returned.

    Returns:
        A FormSpec identical to ``form_spec`` except that every FieldSpec's
        ``shape`` is set to its effective Field Shape, and every nested
        FormSpec reachable through ``nested_form_spec`` has been resolved
        the same way, at every depth.
    """
    resolved_fields: dict[str, FieldSpec] = {}

    for name, field_spec in form_spec.fields.items():
        model_field = model.model_fields.get(name)
        if model_field is None:
            # Not this function's job to enforce alignment (see
            # mimirheim_shared.alignment); a field the model does not have
            # is passed through unresolved rather than raised on here.
            resolved_fields[name] = field_spec
            continue

        derived = derive_field_shape(model_field.annotation)
        effective = effective_field_shape(derived, field_spec.shape_override)

        nested_form_spec = field_spec.nested_form_spec
        if effective in _STRUCTURAL_SHAPES and nested_form_spec is not None:
            nested_model = nested_model_of(model_field.annotation)
            if nested_model is not None:
                nested_form_spec = resolve_field_shapes(nested_model, nested_form_spec)

        resolved_fields[name] = field_spec.model_copy(
            update={"shape": effective, "nested_form_spec": nested_form_spec}
        )

    return FormSpec(fields=resolved_fields)
