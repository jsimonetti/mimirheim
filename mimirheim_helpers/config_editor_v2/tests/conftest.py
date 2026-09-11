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
