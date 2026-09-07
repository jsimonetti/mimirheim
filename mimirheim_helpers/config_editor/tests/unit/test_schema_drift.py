"""Drift test for the committed bundled config-editor schemas.

Mirrors ``tests/unit/test_schema_ui_annotations.py::test_schema_json_is_up_to_date``:
every ``mimirheim_helpers/config_editor/config_editor/schemas/bundled/*.schema.json``
file must match what ``scripts/generate_schema_json.py`` would currently
produce for it. If this fails, regenerate:

    uv run python scripts/generate_schema_json.py

This file is deliberately separate from ``test_schema_ui_annotations.py``
(deleted once the old field vocabulary it tested for was migrated away, per
plan 68 Decision 10) so that bundled-schema drift coverage survives that
deletion.

What this module does not do:
- It does not validate the bundled schemas against the meta-schema or the
  x-mimirheim envelope rules -- that is test_registry.py's job, exercised
  against fixtures. This module only checks for staleness against the live
  Pydantic models.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_editor import registry
from scripts.generate_schema_json import BUNDLED_ENTRIES, build_bundled_schema, _category_orders


def _bundled_schema_path(entry_id: str) -> Path:
    return registry.BUNDLED_SCHEMA_DIR / f"{entry_id}.schema.json"


@pytest.mark.parametrize("entry", BUNDLED_ENTRIES, ids=[e["id"] for e in BUNDLED_ENTRIES])
def test_bundled_schema_is_up_to_date(entry: dict) -> None:
    """Each committed bundled schema matches the live Pydantic model's output."""
    path = _bundled_schema_path(entry["id"])
    assert path.exists(), (
        f"{path} not found. Run: uv run python scripts/generate_schema_json.py"
    )
    order = _category_orders(BUNDLED_ENTRIES)[entry["id"]]
    live = build_bundled_schema(entry, order)
    committed = json.loads(path.read_text())
    assert live == committed, (
        f"{path} is out of date. Run: uv run python scripts/generate_schema_json.py"
    )


def test_no_stray_bundled_schema_files() -> None:
    """schemas/bundled/ contains exactly the files BUNDLED_ENTRIES expects, nothing else."""
    expected = {f"{entry['id']}.schema.json" for entry in BUNDLED_ENTRIES}
    actual = {p.name for p in registry.BUNDLED_SCHEMA_DIR.glob("*.schema.json")}
    assert actual == expected


def test_bundled_schemas_load_cleanly_through_the_registry() -> None:
    """Every real bundled schema passes discovery with zero rejections (canary for §10/§13)."""
    reg = registry.build_registry(registry.BUNDLED_SCHEMA_DIR)
    assert reg.problems == ()
    assert set(reg.entries) == {entry["id"] for entry in BUNDLED_ENTRIES}
