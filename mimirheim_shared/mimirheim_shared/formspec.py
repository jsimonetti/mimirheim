"""FormSpec: the presentation-only counterpart to a Config Owner's validation model.

A Config Owner's pydantic model stays pure (validation rules only). Every
field it wants shown in the Config Editor gets a matching ``FieldSpec`` entry
in a ``FormSpec``, carrying the label, description, help text, Tier, and
grouping the editor renders. A ``FormSpec`` carries no validation logic of
its own; see ``mimirheim_shared.alignment`` for the utility that checks a
``FormSpec`` and its model stay in sync.

A field's Field Shape (scalar, nested object, Named Collection, Ordered
Collection, optional object, or enum select) is derived from the model, not
carried on ``FieldSpec`` itself; see ``mimirheim_shared.field_shape``
(ADR-0006). ``FieldSpec.nested_form_spec`` is how a non-scalar field's own
FormSpec is attached for recursion, and ``FieldSpec.shape_override`` is how a
field is deliberately rendered simpler than its derived shape.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict

from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.visibility import Condition


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
    # The nested model's own FormSpec, for a field whose derived (or
    # overridden) Field Shape is NESTED_OBJECT, OPTIONAL_OBJECT,
    # NAMED_COLLECTION, or ORDERED_COLLECTION. Authored once by the package
    # that defines the nested model and referenced here, never duplicated
    # (ADR-0007).
    nested_form_spec: "FormSpec | None" = None


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
