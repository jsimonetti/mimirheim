"""Shared fixture Pydantic models for config-editor-v2's test suite.

These models are deliberately small and unrelated to any real mimirheim or
helper configuration. They exist only to exercise the registry's model
resolution and the adapter's transform dispatch in isolation. Every field
that exercises a transform opts in explicitly via `x-mimir-adapter`; this
project has no shape-based auto-detection (see `adapter.py`).
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
    `nullable-list` transform is designed to rewrite; the field opts in
    explicitly via `x-mimir-adapter`, since this project has no shape-based
    auto-detection.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[str] | None = Field(
        default=None,
        min_length=2,
        json_schema_extra={"x-mimir-adapter": "nullable-list"},
    )


class NullableScalarModel(BaseModel):
    """A fixture model with a None-or-plain-string field.

    `note` is typed `str | None`, which Pydantic renders as an `anyOf`
    between a string schema and a null schema. This is the exact shape the
    `nullable-scalar` transform is designed to rewrite; the field opts in
    explicitly via `x-mimir-adapter`.
    """

    model_config = ConfigDict(extra="forbid")

    note: str | None = Field(
        default=None,
        description="A note.",
        json_schema_extra={"x-mimir-adapter": "nullable-scalar"},
    )


class NullableScalarNumberModel(BaseModel):
    """A fixture model with a None-or-plain-number field, ge-constrained.

    Used to verify a constrained number branch's own keys (e.g. `minimum`)
    survive the `nullable-scalar` collapse.
    """

    model_config = ConfigDict(extra="forbid")

    amount: float | None = Field(
        default=None,
        ge=0.0,
        json_schema_extra={"x-mimir-adapter": "nullable-scalar"},
    )


class NamedMapItemModel(BaseModel):
    """A fixture item model referenced only via a named-map field's `$ref`.

    `model_json_schema()` gives this class's `$defs` entry a `title` of
    `"NamedMapItemModel"` by default (the class name), the exact shape the
    `dict-key-as-title` transform is designed to strip.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = "default"


class DictKeyAsTitleModel(BaseModel):
    """A fixture model with a named-map field opting into `dict-key-as-title`."""

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, NamedMapItemModel] = Field(
        default_factory=dict,
        json_schema_extra={"x-mimir-adapter": "dict-key-as-title"},
    )


class AddPropertyContentNestedModel(BaseModel):
    """A fixture item model referenced only via a named-map field's `$ref`.

    Carries its own `x-addPropertyContent`-bearing field, so a test can
    confirm `jedison_mapping.to_jedison_object_schema` reaches a field
    nested inside a `$defs` entry, not only a top-level field.
    """

    model_config = ConfigDict(extra="forbid")

    tags: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra={"x-addPropertyContent": "Add tag"},
    )


class AddPropertyContentModel(BaseModel):
    """A fixture model exercising `x-addPropertyContent` -> `x-objectAdd`.

    `labelled` carries Jedison's native `x-addPropertyContent` hint (not an
    `x-mimir-adapter` hint -- this is Jedison's own vocabulary, per the
    "Namespace convention" section, and the one case where this editor
    derives a Jedison-native key automatically rather than requiring an
    explicit hint; see `jedison_mapping.py`'s module docstring for why).
    `unlabelled` carries neither hint, to verify a field that does not ask
    for an add-button label is left alone. `nested` exercises the same
    derivation on a field inside a `$defs` entry.
    """

    model_config = ConfigDict(extra="forbid")

    labelled: dict[str, str] = Field(
        default_factory=dict,
        json_schema_extra={"x-addPropertyContent": "Add labelled entry"},
    )
    unlabelled: dict[str, str] = Field(default_factory=dict)
    nested: dict[str, AddPropertyContentNestedModel] = Field(default_factory=dict)
