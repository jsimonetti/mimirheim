"""Shared fixture Pydantic models for config-editor-v2's test suite.

These models are deliberately small and unrelated to any real mimirheim or
helper configuration. They exist only to exercise the registry's model
resolution and the adapter's transform dispatch in isolation, and are reused
by later steps (71_2, 71_3) that build on the same mechanisms.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PlainFieldModel(BaseModel):
    """A fixture model with a field that carries no x-mimir- hints."""

    model_config = ConfigDict(extra="forbid")

    plain: str = "default"


class IdentityAdapterModel(BaseModel):
    """A fixture model with a field hinting the identity transform.

    The field also carries an unrelated, non-`x-mimir-` hint, to verify that
    the adapter leaves rendering-library-specific hints untouched.
    """

    model_config = ConfigDict(extra="forbid")

    tagged: str = Field(
        default="default",
        json_schema_extra={"x-mimir-adapter": "identity", "someLibraryOption": True},
    )


class UnregisteredAdapterModel(BaseModel):
    """A fixture model with a field naming a transform that is never registered."""

    model_config = ConfigDict(extra="forbid")

    broken: str = Field(
        default="default",
        json_schema_extra={"x-mimir-adapter": "does-not-exist"},
    )


class RequiredFieldModel(BaseModel):
    """A fixture model with a genuinely required field and no default.

    Exists specifically to exercise `save.validate_all`'s "untouched entry
    whose defaults don't validate is silently excluded" path: `name` has no
    default, so `RequiredFieldModel.model_validate({})` raises
    `ValidationError`, mirroring every real production model registered in
    config_editor_v2.registry (all of which require at least an `mqtt`
    block).
    """

    model_config = ConfigDict(extra="forbid")

    name: str


class ListFieldModel(BaseModel):
    """A fixture model with a plain (non-nullable) list-of-int field.

    Used by save.py's tests to exercise a validation error whose Pydantic
    `loc` tuple contains an integer list index (e.g. `("counts", 0)`), since
    `FieldError.loc` must stringify every element of that tuple.
    """

    model_config = ConfigDict(extra="forbid")

    counts: list[int] = Field(default_factory=list)


class NullableListModel(BaseModel):
    """A fixture model with a None-or-non-empty-list field.

    `entries` is typed `list[str] | None` with `min_length=2` on the array
    branch, which Pydantic renders as an `anyOf` between an array schema
    (with `minItems: 2`) and a null schema. This is the exact shape the
    `nullable-list` transform (step 71_2) is designed to rewrite.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[str] | None = Field(
        default=None,
        min_length=2,
        json_schema_extra={"x-mimir-adapter": "nullable-list"},
    )


class NullableListWithDescriptionModel(BaseModel):
    """A nullable-list field that also carries a user-authored description.

    Used by `test_jedison_mapping.py` to verify that
    `jedison_mapping.to_jedison_schema` appends the `nullable-list`
    transform's advisory minimum-length hint to an existing description
    rather than overwriting it.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[str] | None = Field(
        default=None,
        min_length=2,
        description="User-provided list of entries.",
        json_schema_extra={"x-mimir-adapter": "nullable-list"},
    )


class LabelHintModel(BaseModel):
    """A fixture model with a field carrying this editor's label hint.

    Used by `test_jedison_mapping.py` to verify `x-mimir-label` becomes
    Jedison's native `title` key.
    """

    model_config = ConfigDict(extra="forbid")

    named: str = Field(
        default="default",
        json_schema_extra={"x-mimir-label": "Display Name"},
    )


class GroupHintModel(BaseModel):
    """A fixture model with two fields sharing this editor's grouping hint.

    `ungrouped` carries no grouping hint at all, so tests can confirm it is
    left without an `x-category` key. Used by `test_jedison_mapping.py` to
    verify `x-mimir-group` becomes `x-category` on each field and
    `x-format` on the parent object schema.
    """

    model_config = ConfigDict(extra="forbid")

    first: str = Field(default="default", json_schema_extra={"x-mimir-group": "Network"})
    second: str = Field(default="default", json_schema_extra={"x-mimir-group": "Network"})
    ungrouped: str = "default"


class NestedClosedModel(BaseModel):
    """A small closed (extra="forbid") model, nested by `ObjectAddModel`.

    Used by `test_jedison_mapping.py` to verify that `to_jedison_object_schema`
    marks a `$defs` entry with `x-objectAdd: False` when it is closed, not
    just the root schema.
    """

    model_config = ConfigDict(extra="forbid")

    value: str = "default"


class ObjectAddModel(BaseModel):
    """A fixture model exercising Jedison's "Add property" button hint.

    `nested` is a closed sub-model (`additionalProperties: false` once
    rendered to `$defs`), matching every real production model in this
    project. `mapping` is a genuine open map (`dict[str, NestedClosedModel]`,
    matching real fields such as `MimirheimConfig.batteries`): its own
    schema has `additionalProperties` set to a *schema*, not `False`, since
    each dynamically-named entry must still validate as a `NestedClosedModel`.
    Used by `test_jedison_mapping.py` to verify `to_jedison_object_schema`
    sets `x-objectAdd: False` on closed objects (this model's own root
    schema and `$defs["NestedClosedModel"]`) while leaving `mapping`'s own
    field schema untouched, since removing its add button would remove the
    only way to add a new named entry.
    """

    model_config = ConfigDict(extra="forbid")

    nested: NestedClosedModel = Field(default_factory=NestedClosedModel)
    mapping: dict[str, NestedClosedModel] = Field(default_factory=dict)


class UnrecognisedMimirHintModel(BaseModel):
    """A fixture model with an invented, unmapped `x-mimir-` hint.

    Used by `test_jedison_mapping.py` to verify that an `x-mimir-` key this
    step's mapping does not recognise is left in place, not dropped.
    """

    model_config = ConfigDict(extra="forbid")

    weird: str = Field(
        default="default",
        json_schema_extra={"x-mimir-totally-invented-hint": "unchanged"},
    )
