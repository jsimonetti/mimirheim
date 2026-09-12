"""Unit tests for config_editor_v2.adapter's transform dispatch.

No transform is registered in production code; every test that needs one
registers it itself via `monkeypatch.setitem(_TRANSFORMS, ...)` and lets
pytest undo the registration on teardown.

Tests verify:
- A field without an x-mimir-adapter hint passes through unchanged, for both
  schema and data.
- A field with x-mimir-adapter: "identity" is actually routed through the
  registered transform, not merely passed through regardless of the hint.
- A field naming an unregistered transform raises KeyError, rather than
  silently passing through.
- Non-x-mimir hints on a field survive dispatch untouched.
"""

from __future__ import annotations

import pytest

from config_editor_v2.adapter import (
    Transform,
    _TRANSFORMS,
    transform_incoming_data,
    transform_schema,
)

from ..conftest import IdentityAdapterModel, PlainFieldModel, UnregisteredAdapterModel


def _field_schema(model: type, field_name: str) -> dict:
    return model.model_json_schema()["properties"][field_name]


def test_field_without_hint_passes_through_unchanged() -> None:
    """A hint-free field's schema and data are returned identical to input."""
    schema = _field_schema(PlainFieldModel, "plain")

    assert transform_schema(schema, {}) == schema
    assert transform_incoming_data(schema, "some value") == "some value"


def test_field_with_identity_transform_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    """A field naming "identity" is routed through the registered transform.

    The "identity" entry is temporarily replaced with a transform that tags
    its schema output, so the test can distinguish "the dispatch mechanism
    called the registered transform" from "the dispatch mechanism ignored
    the hint and passed the field through unchanged" — both would otherwise
    look identical for a true no-op transform.
    """
    tagging_transform = Transform(
        schema=lambda field_schema, defs: {**field_schema, "x-mimir-test-tag": "dispatched"},
        incoming_data=lambda value: value,
    )
    monkeypatch.setitem(_TRANSFORMS, "identity", tagging_transform)

    schema = _field_schema(IdentityAdapterModel, "tagged")
    transformed = transform_schema(schema, {})

    assert transformed["x-mimir-test-tag"] == "dispatched"


def test_unregistered_transform_name_raises_key_error() -> None:
    """A field naming a never-registered transform raises KeyError."""
    schema = _field_schema(UnregisteredAdapterModel, "broken")

    with pytest.raises(KeyError):
        transform_schema(schema, {})

    with pytest.raises(KeyError):
        transform_incoming_data(schema, "some value")


def test_non_mimir_hints_pass_through_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrelated rendering-library hint survives dispatch untouched."""
    monkeypatch.setitem(
        _TRANSFORMS,
        "identity",
        Transform(schema=lambda field_schema, defs: field_schema, incoming_data=lambda value: value),
    )
    schema = _field_schema(IdentityAdapterModel, "tagged")

    transformed = transform_schema(schema, {})

    assert transformed["someLibraryOption"] is True
