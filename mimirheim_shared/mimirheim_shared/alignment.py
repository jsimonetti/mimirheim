"""Verifies a FormSpec fully describes its paired pydantic validation model.

Every configurable pydantic model gets a separate, colocated FormSpec (see
``mimirheim_shared.formspec``). Nothing at the type level keeps the two from
drifting apart as fields are added or removed. Each Config Owner's own test
suite calls ``assert_form_spec_complete`` against its own model and FormSpec
to catch that drift; this is not run centrally by the Config Editor.
"""

from __future__ import annotations

from pydantic import BaseModel

from mimirheim_shared.formspec import FormSpec


def assert_form_spec_complete(model: type[BaseModel], spec: FormSpec) -> None:
    """Assert every field of ``model`` has a matching entry in ``spec``.

    A model field is "represented" if ``spec.fields`` has an entry for it,
    whether or not that entry is marked ``hidden``; ``hidden=True`` is how a
    field is explicitly excluded from the rendered form while still
    documenting that the omission was intentional rather than an oversight.

    Args:
        model: The pydantic model defining the on-disk configuration shape.
        spec: The FormSpec meant to describe every field of ``model``.

    Raises:
        ValueError: If a model field has no FormSpec entry, or a FormSpec
            entry names a field that does not exist on the model.
    """
    model_field_names = set(model.model_fields)
    spec_field_names = set(spec.fields)

    missing = sorted(model_field_names - spec_field_names)
    if missing:
        raise ValueError(
            f"FormSpec for {model.__name__} is missing entries for fields: {missing}"
        )

    extra = sorted(spec_field_names - model_field_names)
    if extra:
        raise ValueError(
            f"FormSpec for {model.__name__} declares fields not present on the model: {extra}"
        )
