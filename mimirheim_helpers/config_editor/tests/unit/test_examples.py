"""Tests for the worked example schemas under ``examples/schemas/`` (plan 70 decision 6).

These are the schemas the drop-in authoring wiki page
(``wiki/Developer/Custom-Config-Schemas.md``) walks a third-party author
through. They must stay valid (or, for the broken example, invalid in the
exact documented way) so the wiki page never rots silently.
"""
from __future__ import annotations

import json
from pathlib import Path

from config_editor import registry

_EXAMPLES_DIR = Path(__file__).parents[2] / "examples" / "schemas"
_SOLAREDGE_PATH = _EXAMPLES_DIR / "solaredge.schema.json"
_BROKEN_EXAMPLE_PATH = _EXAMPLES_DIR / "broken-example.schema.json.txt"


def test_solaredge_example_passes_validate_schemas(tmp_path: Path) -> None:
    """solaredge.schema.json, placed as a drop-in, loads with zero rejections."""
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "solaredge.schema.json").write_text(_SOLAREDGE_PATH.read_text())

    reg = registry.build_registry(bundled_dir=registry.BUNDLED_SCHEMA_DIR, dropin_dir=schemas_dir)

    assert reg.problems == ()
    assert "solaredge" in reg.entries


def test_solaredge_example_exercises_the_full_blessed_vocabulary() -> None:
    """The example must actually use every vocabulary item plan 70 requires of it."""
    doc = json.loads(_SOLAREDGE_PATH.read_text())

    inverter = doc["$defs"]["InverterConfig"]["properties"]
    assert doc["properties"]["inverters"]["additionalProperties"] == {"$ref": "#/$defs/InverterConfig"}
    assert inverter["mode"]["enum"] == ["single-phase", "three-phase"]
    assert inverter["poll_interval_s"]["x-category"] == "Advanced"
    assert inverter["pv_array"]["x-enumSource"] == "#/context/pv_arrays"
    assert inverter["output_topic"]["x-format"] == "mimir-topic-placeholder"


def test_broken_example_produces_the_exact_documented_rejection_reason(tmp_path: Path) -> None:
    """The .txt-suffixed broken example, copied in as a live schema, is rejected exactly as documented."""
    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "broken-example.schema.json").write_text(_BROKEN_EXAMPLE_PATH.read_text())

    reg = registry.build_registry(bundled_dir=registry.BUNDLED_SCHEMA_DIR, dropin_dir=schemas_dir)

    assert "broken-example" not in reg.entries
    assert len(reg.problems) == 1
    assert reg.problems[0].source == "broken-example.schema.json"
    assert reg.problems[0].reason == (
        "x-mimirheim is missing; every mimirheim helper schema must declare "
        "an x-mimirheim envelope (SPEC.md §3)."
    )


def test_broken_example_is_ignored_by_real_discovery_as_shipped() -> None:
    """The committed .txt file itself is never picked up by the *.schema.json glob."""
    assert _BROKEN_EXAMPLE_PATH.exists()
    assert not _BROKEN_EXAMPLE_PATH.name.endswith(".schema.json")
