"""Unit tests for config_editor.registry.ConfigOwnerRegistry."""

from __future__ import annotations

from mimirheim_shared.config_service import Descriptor
from mimirheim_shared.formspec import FieldSpec, FormSpec

from config_editor.registry import ConfigOwnerRegistry


def _descriptor(owner_id: str, display_name: str) -> Descriptor:
    return Descriptor(
        owner_id=owner_id,
        display_name=display_name,
        json_schema={"properties": {}},
        form_spec=FormSpec(fields={"enabled": FieldSpec(label="Enabled", description="Enabled.")}),
    )


def test_unknown_owner_returns_none() -> None:
    registry = ConfigOwnerRegistry()

    assert registry.get("mimirheim-core") is None
    assert registry.all() == []


def test_update_registers_a_new_owner() -> None:
    registry = ConfigOwnerRegistry()
    descriptor = _descriptor("mimirheim-core", "Mimirheim")

    registry.update(descriptor)

    assert registry.get("mimirheim-core") == descriptor
    assert registry.all() == [descriptor]


def test_update_replaces_an_existing_owner() -> None:
    registry = ConfigOwnerRegistry()
    registry.update(_descriptor("mimirheim-core", "Mimirheim"))
    replacement = _descriptor("mimirheim-core", "Mimirheim (renamed)")

    registry.update(replacement)

    assert registry.get("mimirheim-core") == replacement
    assert registry.all() == [replacement]


def test_remove_drops_a_registered_owner() -> None:
    registry = ConfigOwnerRegistry()
    registry.update(_descriptor("mimirheim-core", "Mimirheim"))

    registry.remove("mimirheim-core")

    assert registry.get("mimirheim-core") is None
    assert registry.all() == []


def test_remove_of_unknown_owner_is_a_no_op() -> None:
    registry = ConfigOwnerRegistry()

    registry.remove("does-not-exist")

    assert registry.all() == []


def test_all_is_sorted_by_display_name() -> None:
    registry = ConfigOwnerRegistry()
    registry.update(_descriptor("nordpool", "Nordpool prices"))
    registry.update(_descriptor("mimirheim-core", "Mimirheim"))

    assert [d.owner_id for d in registry.all()] == ["mimirheim-core", "nordpool"]
