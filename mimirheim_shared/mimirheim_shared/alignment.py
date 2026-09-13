"""Verifies a FormSpec fully describes its paired pydantic validation model.

Every configurable pydantic model gets a separate, colocated FormSpec (see
``mimirheim_shared.formspec``). Nothing at the type level keeps the two from
drifting apart as fields are added or removed. Each Config Owner's own test
suite calls ``assert_form_spec_complete`` against its own model and FormSpec
to catch that drift; this is not run centrally by the Config Editor.

Per ADR-0006, a FormSpec's structure now mirrors its validation model
recursively rather than only at the top level: a field whose Field Shape is
a nested object, Named Collection, Ordered Collection, or optional object
must carry a ``nested_form_spec`` (or an explicit Shape Override down to a
plain scalar), and that nested FormSpec is checked for completeness against
its own model in turn, at every depth.
"""

from __future__ import annotations

from pydantic import BaseModel

from mimirheim_shared.field_shape import (
    FieldShape,
    derive_field_shape,
    effective_field_shape,
    nested_model_of,
)
from mimirheim_shared.formspec import FormSpec

# Shapes with no nested FormSpec to recurse into: a plain value and a
# Literal[...] select both render as a single field, never a sub-form.
_LEAF_SHAPES = frozenset({FieldShape.SCALAR, FieldShape.ENUM_SELECT})


def assert_form_spec_complete(
    model: type[BaseModel], spec: FormSpec, *, _path: str = ""
) -> None:
    """Assert every field of ``model`` has a matching entry in ``spec``, recursively.

    A model field is "represented" if ``spec.fields`` has an entry for it,
    whether or not that entry is marked ``hidden``; ``hidden=True`` is how a
    field is explicitly excluded from the rendered form while still
    documenting that the omission was intentional rather than an oversight.

    For a field whose effective Field Shape (derived from the model,
    optionally simplified by a Shape Override; see
    ``mimirheim_shared.field_shape``) is a nested object, Named Collection,
    Ordered Collection, or optional object, its ``FieldSpec.nested_form_spec``
    is in turn checked for completeness against the nested model it recurses
    into -- unless the field is ``hidden``, in which case its contents are
    not rendered at all and need no nested FormSpec.

    Args:
        model: The pydantic model defining the on-disk configuration shape.
        spec: The FormSpec meant to describe every field of ``model``.

    Raises:
        ValueError: If a model field has no FormSpec entry, a FormSpec entry
            names a field that does not exist on the model, a structural
            field has no ``nested_form_spec`` and no Shape Override, or a
            nested FormSpec is itself incomplete for its own model, at any
            depth.
    """
    label = model.__name__ if not _path else f"{model.__name__} (at {_path})"

    model_field_names = set(model.model_fields)
    spec_field_names = set(spec.fields)

    missing = sorted(model_field_names - spec_field_names)
    if missing:
        raise ValueError(f"FormSpec for {label} is missing entries for fields: {missing}")

    extra = sorted(spec_field_names - model_field_names)
    if extra:
        raise ValueError(
            f"FormSpec for {label} declares fields not present on the model: {extra}"
        )

    for field_name, field_spec in spec.fields.items():
        if field_spec.hidden:
            continue

        annotation = model.model_fields[field_name].annotation
        derived = derive_field_shape(annotation)
        effective = effective_field_shape(derived, field_spec.shape_override)
        if effective in _LEAF_SHAPES:
            continue

        nested_model = nested_model_of(annotation)
        field_path = f"{_path}.{field_name}" if _path else field_name
        if nested_model is None or field_spec.nested_form_spec is None:
            raise ValueError(
                f"FormSpec for {label} field {field_name!r} has shape "
                f"{effective.value!r} but no nested_form_spec to describe it."
            )
        assert_form_spec_complete(
            nested_model, field_spec.nested_form_spec, _path=field_path
        )
