"""Unit tests for config_editor_v2.registry.

Tests verify:
- resolve_model imports and returns the exact class named by a registry
  entry.
- resolve_model re-raises ImportError for a bogus module path.
- resolve_model re-raises AttributeError for a valid module with a missing
  class name.
- RegistryEntry rejects unknown fields (extra="forbid").
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config_editor_v2.registry import RegistryEntry, resolve_model

from ..conftest import PlainFieldModel


def test_resolve_model_imports_class() -> None:
    """A RegistryEntry pointing at a fixture model resolves to that class."""
    entry = RegistryEntry(
        name="Plain",
        filename="plain.yaml",
        model_path=f"{PlainFieldModel.__module__}.{PlainFieldModel.__qualname__}",
    )
    assert resolve_model(entry) is PlainFieldModel


def test_resolve_model_raises_on_missing_module() -> None:
    """A bogus module path raises ImportError, not a generic exception."""
    entry = RegistryEntry(
        name="Bogus",
        filename="bogus.yaml",
        model_path="config_editor_v2_does_not_exist.module.Model",
    )
    with pytest.raises(ImportError):
        resolve_model(entry)


def test_resolve_model_raises_on_missing_attribute() -> None:
    """A valid module with a missing class name raises AttributeError."""
    entry = RegistryEntry(
        name="Missing attribute",
        filename="missing.yaml",
        model_path=f"{PlainFieldModel.__module__}.DoesNotExist",
    )
    with pytest.raises(AttributeError):
        resolve_model(entry)


def test_registry_entry_rejects_unknown_fields() -> None:
    """RegistryEntry.model_validate raises on an unexpected keyword."""
    with pytest.raises(ValidationError):
        RegistryEntry.model_validate(
            {
                "name": "Plain",
                "filename": "plain.yaml",
                "model_path": f"{PlainFieldModel.__module__}.{PlainFieldModel.__qualname__}",
                "unexpected": "field",
            }
        )
