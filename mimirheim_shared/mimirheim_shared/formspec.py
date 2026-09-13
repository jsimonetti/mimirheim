"""FormSpec: the presentation-only counterpart to a Config Owner's validation model.

A Config Owner's pydantic model stays pure (validation rules only). Every
field it wants shown in the Config Editor gets a matching ``FieldSpec`` entry
in a ``FormSpec``, carrying the label, description, help text, Tier, and
grouping the editor renders. A ``FormSpec`` carries no validation logic of
its own; see ``mimirheim_shared.alignment`` for the utility that checks a
``FormSpec`` and its model stay in sync.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict

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


class FormSpec(BaseModel):
    """The full set of FieldSpecs for one Config Owner's validation model."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, FieldSpec]
