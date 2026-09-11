"""Maps config-editor-v2's `x-mimir-` hints to Jedison's schema vocabulary.

This module is the Jedison-specific half of the adapter described in
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md, section "The
adapter". Where `adapter.py` and `transforms.py` are rendering-library
agnostic, this module is the one place in config-editor-v2 that knows what
concrete schema attributes Jedison itself reads. It runs after
`adapter.transform_schema` (see `adapter.py`) and any field-specific
transform such as `nullable-list` (see `transforms.py`) have already been
applied to a field's schema fragment.

The plan for this step specifies a single function operating on one field's
schema in isolation, `to_jedison_schema`. That is preserved below for the
purely per-field concerns: copying this editor's label hint into Jedison's
native `title` key, and folding the `nullable-list` transform's advisory
minimum-length hint into Jedison's native `description` key. Grouping fields
into sections cannot be decided from a single field's schema alone -- Jedison
expects `x-format` set once on the *parent* object schema and `x-category`
set on each grouped *field* schema, which requires seeing every field in the
model at once. `to_jedison_object_schema` is added for that concern: it
takes a full model schema (as produced by a Pydantic model's
`model_json_schema()`), applies `to_jedison_schema` to every field under
`properties`, and additionally resolves grouping across all of them.

Hint key names used by this module (chosen here, following this project's
existing `x-mimir-` naming precedent set by `x-mimir-adapter` and
`x-mimir-min-length-hint`; IMPLEMENTATION_DETAILS.md does not spell out
literal names for these two source hints):

- `x-mimir-label`: this field's display label. Copied into Jedison's native
  `title` key.
- `x-mimir-group`: the name of the section this field belongs to. Translated
  into Jedison's `x-category` (on the field) and `x-format` (on the parent
  object schema, set once, to `"categories-vertical"` -- see
  `_DEFAULT_CATEGORIES_FORMAT` below for why this default was chosen).

A field with none of the `x-mimir-` keys this module recognises (`x-mimir-label`,
`x-mimir-group`, and the `nullable-list` transform's `x-mimir-min-length-hint`)
is passed through unchanged. An `x-mimir-` key this module does not recognise
-- including `x-mimir-adapter`, which belongs to `adapter.py`, not to this
module -- is left in place rather than dropped, so a future mapping addition
is additive rather than a breaking rename. A hint outside the `x-mimir-`
namespace is assumed to already be in Jedison's own vocabulary and is left
untouched, per IMPLEMENTATION_DETAILS.md's "Namespace convention".

This module does not perform a data-direction transform. Unlike the
transforms in `transforms.py`, none of this module's concerns (label,
grouping, advisory help text) change the shape of a value submitted by the
form, so there is nothing for a submit-time inverse to undo. It also does
not invoke `adapter.transform_schema` itself -- callers are responsible for
running a field's schema through the adapter's dispatch first -- and it does
not know about the registry, the rendering library's runtime behaviour, or
HTTP serving; the HTTP service is built in step 71_5.
"""

from __future__ import annotations

from typing import Any

# This editor's own hint for a field's display label. Copied verbatim into
# Jedison's native `title` key, which Jedison reads with no `x-` prefix.
_LABEL_HINT_KEY = "x-mimir-label"

# This editor's own hint for which section a field belongs to. Translated
# into Jedison's two-part category construct: `x-category` on the field and
# `x-format` on the parent object schema (see `to_jedison_object_schema`).
_GROUP_HINT_KEY = "x-mimir-group"

# Must match the advisory hint key the `nullable-list` transform writes in
# transforms.py (`_MIN_LENGTH_HINT_KEY` there). Kept as a separate literal
# here, rather than imported, because this module maps *any* field carrying
# this hint into Jedison's `description`, regardless of which transform
# produced it -- the coupling is to the hint's name, not to transforms.py's
# implementation.
_MIN_LENGTH_HINT_KEY = "x-mimir-min-length-hint"

# Jedison supports two layouts for a categorized object schema:
# "categories-horizontal" (tabs across the top) and "categories-vertical"
# (a side list of section names). This step picks "categories-vertical" as
# the default because it degrades better on narrow viewports -- a horizontal
# tab strip has to scroll or wrap once there are more than a handful of
# sections, while a vertical list simply grows. Nothing in this step's
# scope requires the other layout; if a specific registry entry ever needs
# it, that is a future per-entry override, not a change to this default.
_DEFAULT_CATEGORIES_FORMAT = "categories-vertical"


def to_jedison_schema(field_schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrites one field's `x-mimir-` hints into Jedison's own vocabulary.

    This function runs after `adapter.transform_schema` (see `adapter.py`)
    and any field-specific transform such as `nullable-list` (see
    `transforms.py`) have already applied. It performs no domain-specific
    logic of its own -- it only renames or restructures already-produced
    `x-mimir-` keys into Jedison's vocabulary, for the concerns that can be
    decided from a single field's schema in isolation. Grouping
    (`x-mimir-group`) is not handled here; see `to_jedison_object_schema`.

    Args:
        field_schema: The JSON Schema fragment for a single field, after any
            adapter transform has already been applied to it.

    Returns:
        A new schema fragment (the input is not mutated). This editor's
        label hint, if present, becomes the `title` key. The `nullable-list`
        transform's advisory minimum-length hint, if present, is appended to
        the `description` key rather than overwriting any existing
        description. A field with neither hint is returned equal to the
        input. Any other key, `x-mimir-` namespaced or not, is left in
        place unmodified.
    """
    rewritten = dict(field_schema)

    label = rewritten.pop(_LABEL_HINT_KEY, None)
    if label is not None:
        rewritten["title"] = label

    min_length_hint = rewritten.pop(_MIN_LENGTH_HINT_KEY, None)
    if min_length_hint is not None:
        advisory = f"At least {min_length_hint} entries are required if any are provided."
        existing_description = rewritten.get("description")
        rewritten["description"] = (
            f"{existing_description} {advisory}" if existing_description else advisory
        )

    return rewritten


def to_jedison_object_schema(model_schema: dict[str, Any]) -> dict[str, Any]:
    """Rewrites a full model schema for Jedison, including field grouping.

    Applies `to_jedison_schema` to every field schema under `properties`,
    then resolves two concerns that cannot be decided from a single field
    in isolation:

    - This editor's grouping hint (`x-mimir-group`). Each field carrying
      that hint gets Jedison's `x-category` key set to the hint's value,
      and if any field in the schema carries the hint, this returned
      top-level object schema gets `x-format` set once, to
      `_DEFAULT_CATEGORIES_FORMAT`, so Jedison renders a categorized layout
      instead of a single flat list of fields.
    Args:
        model_schema: The full JSON Schema produced by a Pydantic model's
            `model_json_schema()`, with fields not yet run through
            `to_jedison_schema`.

    Returns:
        A new schema dict (the input and its `properties` sub-dicts are not
        mutated). `properties` is replaced with the per-field mapped
        versions. `x-format` is added only when at least one field carries
        the grouping hint; a model with no grouped fields is returned with
        no `x-format` key at all, matching Jedison's own fallback
        behaviour for an ungrouped object schema.
    """
    rewritten = dict(model_schema)
    properties = {
        name: to_jedison_schema(field_schema)
        for name, field_schema in rewritten.get("properties", {}).items()
    }

    has_grouped_field = False
    for field_schema in properties.values():
        group = field_schema.pop(_GROUP_HINT_KEY, None)
        if group is not None:
            field_schema["x-category"] = group
            has_grouped_field = True

    rewritten["properties"] = properties
    if has_grouped_field:
        rewritten["x-format"] = _DEFAULT_CATEGORIES_FORMAT

    return rewritten
