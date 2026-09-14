"""Field Shape: a FormSpec field's structural kind, derived from its validation model.

A field's Field Shape -- scalar, nested object, Named Collection, Ordered
Collection, optional object, or enum select -- is inferred from the
corresponding pydantic field's own type annotation rather than hand-declared
on every ``FieldSpec`` (see ADR-0006, ``mimirheim_shared/docs/adr``). This
module provides that derivation, the lookup of the nested pydantic model a
non-scalar shape recurses into, and the one-directional rule a ``FieldSpec``'s
Shape Override must obey: it may only simplify, never add structure the
validation model does not have.
"""

from __future__ import annotations

import types
from enum import Enum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel


class FieldShape(str, Enum):
    """A FormSpec field's structural kind, derived from its validation model."""

    SCALAR = "scalar"
    NESTED_OBJECT = "nested_object"
    NAMED_COLLECTION = "named_collection"
    ORDERED_COLLECTION = "ordered_collection"
    OPTIONAL_OBJECT = "optional_object"
    ENUM_SELECT = "enum_select"
    # A list of scalars (e.g. list[float]), as distinct from ORDERED_COLLECTION's
    # list of a nested model: there is no nested model to recurse into, but it is
    # still rendered/submitted as one entry per item, not one opaque text field.
    SCALAR_LIST = "scalar_list"


def _is_model(candidate: Any) -> bool:
    return isinstance(candidate, type) and issubclass(candidate, BaseModel)


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    """Strip a ``X | None`` wrapper, reporting whether it was present.

    Handles both ``X | None`` (``types.UnionType``) and ``Optional[X]``
    (``typing.Union``) spellings, since a field's annotation may be written
    either way.
    """
    origin = get_origin(annotation)
    if origin is types.UnionType or str(origin) == "typing.Union":
        args = get_args(annotation)
        non_none = [arg for arg in args if arg is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            return non_none[0], True
    return annotation, False


def derive_field_shape(annotation: Any) -> FieldShape:
    """Derive a field's Field Shape from its pydantic type annotation.

    Args:
        annotation: A pydantic model field's resolved annotation, e.g.
            ``model.model_fields[name].annotation``.

    Returns:
        The Field Shape implied by the annotation's own structure. Any
        annotation this module does not recognise as nested structure (plain
        scalars, but also unions of multiple non-model types) is SCALAR: it is
        rendered as an opaque value, never silently mistaken for structure.
    """
    origin = get_origin(annotation)
    if origin is Literal:
        return FieldShape.ENUM_SELECT

    inner, was_optional = _unwrap_optional(annotation)
    inner_origin = get_origin(inner)

    if inner_origin is dict:
        args = get_args(inner)
        if len(args) == 2 and _is_model(args[1]):
            return FieldShape.NAMED_COLLECTION
        return FieldShape.SCALAR

    if inner_origin is list:
        args = get_args(inner)
        if len(args) == 1 and _is_model(args[0]):
            return FieldShape.ORDERED_COLLECTION
        if len(args) == 1:
            return FieldShape.SCALAR_LIST
        return FieldShape.SCALAR

    if _is_model(inner):
        return FieldShape.OPTIONAL_OBJECT if was_optional else FieldShape.NESTED_OBJECT

    return FieldShape.SCALAR


def nested_model_of(annotation: Any) -> type[BaseModel] | None:
    """Return the nested pydantic model class a non-scalar annotation recurses into.

    Args:
        annotation: A pydantic model field's resolved annotation.

    Returns:
        The nested model class for a nested object, optional object, Named
        Collection, or Ordered Collection annotation. None for a scalar or
        enum-select annotation, which have no nested model to recurse into.
    """
    inner, _ = _unwrap_optional(annotation)
    inner_origin = get_origin(inner)

    if inner_origin is dict:
        args = get_args(inner)
        candidate = args[1] if len(args) == 2 else None
        return candidate if candidate is not None and _is_model(candidate) else None

    if inner_origin is list:
        args = get_args(inner)
        candidate = args[0] if len(args) == 1 else None
        return candidate if candidate is not None and _is_model(candidate) else None

    return inner if _is_model(inner) else None


def effective_field_shape(
    derived: FieldShape, override: FieldShape | None
) -> FieldShape:
    """Resolve a field's effective Field Shape, enforcing the one-directional Shape Override rule.

    Args:
        derived: The Field Shape derived from the field's validation model.
        override: The field's explicit Shape Override, if any.

    A Shape Override may only simplify a field down to an opaque scalar (e.g.
    a nested object rendered as raw text instead of recursed into); it can
    never retarget a field to a different structured shape, since that would
    invent structure the validation model does not have.

    Args:
        derived: The Field Shape derived from the field's validation model.
        override: The field's explicit Shape Override, if any.

    Returns:
        ``derived`` when no override is set, otherwise ``override``.

    Raises:
        ValueError: If ``override`` is set to anything other than ``derived``
            itself or ``FieldShape.SCALAR``.
    """
    if override is None or override is derived:
        return derived
    if override is not FieldShape.SCALAR:
        raise ValueError(
            f"Shape Override {override.value!r} is not simpler than the derived "
            f"shape {derived.value!r}. A Shape Override may only simplify a field "
            "down to an opaque scalar, never to another structured shape."
        )
    return override
