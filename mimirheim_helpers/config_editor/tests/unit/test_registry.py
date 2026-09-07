"""Unit tests for config_editor.registry.

Covers, against fixture schema files built at test time (not the real
bundled ones -- see test_schema_drift.py for those):

- Every SPEC.md §10 rejection rule (13 rules), one fixture and one test each.
- Drop-in discovery and collision handling (SPEC.md §2).
- Document composition -- the ``context`` subtree (SPEC.md §5).
- The two-pass validation model (SPEC.md §7).
- Save semantics -- validate-all-then-write-all, exclusive groups, comment
  preservation (SPEC.md §8).

What this module does not do:
- It does not exercise the real bundled schemas under schemas/bundled/ --
  that is test_schema_drift.py's job.
- It does not exercise any HTTP endpoint -- registry.py has no HTTP
  dependencies, and neither do these tests.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from config_editor import registry


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _valid_schema(**overrides: Any) -> dict[str, Any]:
    """Return a minimal schema document that passes every rejection rule.

    Tests mutate exactly one aspect of this to trigger exactly one rule.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"enabled": {"type": "boolean"}},
        "x-mimirheim": {
            "file": "widget.yaml",
            "category": "other",
            "python_package": "widget",
        },
    }
    schema.update(overrides)
    return schema


def _write_schema(directory: Path, filename: str, content: str | dict[str, Any]) -> Path:
    """Write ``content`` (a dict, JSON-encoded, or a raw string) to ``directory/filename``."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    text = content if isinstance(content, str) else json.dumps(content)
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------
# Step 1/2 -- one fixture and one test per SPEC.md §10 rejection rule
# ---------------------------------------------------------------------------


def test_rule1_oversized_file_is_rejected(tmp_path: Path) -> None:
    """Rule 1: a schema file over the 256 KiB limit is rejected."""
    huge = _valid_schema()
    huge["description"] = "x" * (registry.MAX_SCHEMA_FILE_BYTES + 1024)
    _write_schema(tmp_path, "widget.schema.json", huge)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "byte limit" in problems[0].reason
    assert problems[0].source == "widget.schema.json"


def test_rule2_invalid_json_is_rejected(tmp_path: Path) -> None:
    """Rule 2: a file that is not valid JSON is rejected."""
    _write_schema(tmp_path, "widget.schema.json", "{not valid json")

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "not valid JSON" in problems[0].reason


def test_rule3_meta_schema_failure_is_rejected(tmp_path: Path) -> None:
    """Rule 3: a document that fails the meta-schema (wrong JSON type for a known key) is rejected."""
    bad = _valid_schema(properties="not-an-object")
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "meta-schema validation" in problems[0].reason


def test_rule4_root_type_not_object_is_rejected(tmp_path: Path) -> None:
    """Rule 4: a schema whose declared root "type" is not "object" is rejected."""
    bad = _valid_schema(type="array")
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert '"type"' in problems[0].reason
    assert "object" in problems[0].reason


def test_rule5_missing_x_mimirheim_is_rejected(tmp_path: Path) -> None:
    """Rule 5: a schema with no x-mimirheim envelope at all is rejected."""
    bad = _valid_schema()
    del bad["x-mimirheim"]
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "x-mimirheim is missing" in problems[0].reason


def test_rule6_bad_file_pattern_is_rejected(tmp_path: Path) -> None:
    """Rule 6: x-mimirheim.file failing the required pattern is rejected."""
    bad = _valid_schema(**{"x-mimirheim": {"file": "Not Valid!!.yaml", "category": "other", "python_package": "w"}})
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "does not match the required pattern" in problems[0].reason


def test_rule7_file_with_path_separator_is_rejected(tmp_path: Path) -> None:
    """Rule 7: x-mimirheim.file containing a path separator is rejected with a specific message."""
    bad = _valid_schema(**{"x-mimirheim": {"file": "sub/dir.yaml", "category": "other", "python_package": "w"}})
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "path separator" in problems[0].reason


def test_rule8_file_equal_to_config_editor_yaml_is_rejected(tmp_path: Path) -> None:
    """Rule 8: x-mimirheim.file equal to "config-editor.yaml" is rejected."""
    bad = _valid_schema(
        **{"x-mimirheim": {"file": "config-editor.yaml", "category": "other", "python_package": "w"}}
    )
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "config-editor.yaml" in problems[0].reason


def test_rule9_unknown_category_is_rejected(tmp_path: Path) -> None:
    """Rule 9: x-mimirheim.category outside the closed set is rejected."""
    bad = _valid_schema(**{"x-mimirheim": {"file": "widget.yaml", "category": "bogus", "python_package": "w"}})
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "category" in problems[0].reason
    assert "bogus" in problems[0].reason


def test_rule10_bundled_collision_via_duplicate_id_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 10: discover_bundled raises when two files resolve to the same id.

    Two schema files cannot share a filename on one filesystem (the id is
    the filename stem), so this reproduces the packaging-bug scenario the
    same way a real duplicate would: by making the loader report the same
    id for two distinct files, then asserting discover_bundled treats that
    as the startup-time hard error SPEC.md §2 requires -- never a soft
    rejection.
    """
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())
    real_loader = registry._load_single_schema

    def _fake_load(path: Path, *, is_bundled: bool) -> registry.RegistryEntry:
        # Every file in this directory resolves to id "widget", forcing the
        # collision discover_bundled must detect regardless of which real
        # filename produced it.
        result = real_loader(path, is_bundled=is_bundled)
        assert isinstance(result, registry.RegistryEntry)
        return registry.RegistryEntry(
            id="widget",
            envelope=result.envelope,
            schema=result.schema,
            source_filename=path.name,
            is_bundled=is_bundled,
        )

    _write_schema(
        tmp_path,
        "widget-second.schema.json",
        _valid_schema(**{"x-mimirheim": {"file": "widget2.yaml", "category": "other", "python_package": "w2"}}),
    )
    monkeypatch.setattr(registry, "_load_single_schema", _fake_load)

    with pytest.raises(registry.BundledSchemaCollisionError) as exc_info:
        registry.discover_bundled(tmp_path)

    assert "widget" in str(exc_info.value)


def test_rule11_non_local_ref_is_rejected(tmp_path: Path) -> None:
    """Rule 11: any $ref that is not document-local ("#/...") is rejected."""
    bad = _valid_schema()
    bad["properties"]["evil"] = {"$ref": "https://example.com/other.json"}
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "non-local $ref" in problems[0].reason
    assert "https://example.com/other.json" in problems[0].reason


def test_rule12_excessive_nesting_depth_is_rejected(tmp_path: Path) -> None:
    """Rule 12: schema nesting depth beyond the limit is rejected."""
    bad = _valid_schema()
    node: dict[str, Any] = {"type": "object", "properties": {}}
    cursor = node
    for _ in range(registry.MAX_SCHEMA_NESTING_DEPTH + 5):
        child: dict[str, Any] = {"type": "object", "properties": {}}
        cursor["properties"]["nested"] = child
        cursor = child
    bad["properties"]["deep"] = node
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "nesting depth" in problems[0].reason


def test_rule13_excessive_property_count_is_rejected(tmp_path: Path) -> None:
    """Rule 13: total property count (root + $defs) beyond the limit is rejected."""
    bad = _valid_schema()
    bad["properties"].update({f"field_{i}": {"type": "string"} for i in range(registry.MAX_SCHEMA_PROPERTY_COUNT + 1)})
    _write_schema(tmp_path, "widget.schema.json", bad)

    entries, problems = registry.discover_bundled(tmp_path)

    assert entries == {}
    assert len(problems) == 1
    assert "properties" in problems[0].reason


# ---------------------------------------------------------------------------
# Step 1/2 -- a valid bundled schema loads successfully
# ---------------------------------------------------------------------------


def test_valid_bundled_schema_loads(tmp_path: Path) -> None:
    """A schema satisfying every rule loads into a RegistryEntry."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())

    entries, problems = registry.discover_bundled(tmp_path)

    assert problems == []
    assert "widget" in entries
    entry = entries["widget"]
    assert entry.id == "widget"
    assert entry.envelope.file == "widget.yaml"
    assert entry.envelope.category == "other"
    assert entry.envelope.python_package == "widget"
    assert entry.is_bundled is True
    assert entry.source_filename == "widget.schema.json"


def test_malformed_bundled_schema_never_blocks_a_valid_sibling(tmp_path: Path) -> None:
    """A malformed bundled file never prevents a valid sibling from loading."""
    _write_schema(tmp_path, "broken.schema.json", "{not valid json")
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())

    entries, problems = registry.discover_bundled(tmp_path)

    assert "widget" in entries
    assert len(problems) == 1
    assert problems[0].source == "broken.schema.json"


# ---------------------------------------------------------------------------
# Step 3 -- drop-in discovery and collision handling
# ---------------------------------------------------------------------------


def test_dropin_with_fresh_id_loads(tmp_path: Path) -> None:
    """A drop-in with an id not claimed by any bundled schema loads normally."""
    _write_schema(tmp_path, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "gadget.yaml", "category": "other", "python_package": "gadget"},
    }))

    entries, problems = registry.discover_dropins(tmp_path, bundled_ids=frozenset({"widget"}))

    assert problems == []
    assert "gadget" in entries
    assert entries["gadget"].is_bundled is False


def test_dropin_colliding_with_bundled_id_is_rejected_bundled_wins(tmp_path: Path) -> None:
    """A drop-in id colliding with a bundled id is rejected; the bundled schema wins."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())

    entries, problems = registry.discover_dropins(tmp_path, bundled_ids=frozenset({"widget"}))

    assert entries == {}
    assert len(problems) == 1
    assert "collides with a bundled schema" in problems[0].reason


def test_two_dropins_sharing_an_id_first_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two drop-in files whose ids collide: sorted-first wins, second is rejected naming it."""
    _write_schema(tmp_path, "aaa.schema.json", _valid_schema())
    _write_schema(tmp_path, "zzz.schema.json", _valid_schema(
        **{"x-mimirheim": {"file": "widget2.yaml", "category": "other", "python_package": "w2"}}
    ))

    real_loader = registry._load_single_schema

    def _same_id_loader(path: Path, *, is_bundled: bool) -> registry.RegistryEntry | registry.RejectedSchema:
        result = real_loader(path, is_bundled=is_bundled)
        if isinstance(result, registry.RegistryEntry):
            return registry.RegistryEntry(
                id="widget",
                envelope=result.envelope,
                schema=result.schema,
                source_filename=path.name,
                is_bundled=is_bundled,
            )
        return result

    monkeypatch.setattr(registry, "_load_single_schema", _same_id_loader)

    entries, problems = registry.discover_dropins(tmp_path, bundled_ids=frozenset())

    assert set(entries) == {"widget"}
    assert entries["widget"].source_filename == "aaa.schema.json"
    assert len(problems) == 1
    assert problems[0].source == "zzz.schema.json"
    assert "aaa.schema.json" in problems[0].reason


def test_malformed_dropin_never_blocks_a_valid_sibling(tmp_path: Path) -> None:
    """A malformed drop-in never prevents a valid sibling from loading."""
    _write_schema(tmp_path, "broken.schema.json", "{not valid json")
    _write_schema(tmp_path, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "gadget.yaml", "category": "other", "python_package": "gadget"},
    }))

    entries, problems = registry.discover_dropins(tmp_path, bundled_ids=frozenset())

    assert "gadget" in entries
    assert any(p.source == "broken.schema.json" for p in problems)


def test_build_registry_merges_bundled_and_dropin(tmp_path: Path) -> None:
    """build_registry combines bundled and drop-in directories into one Registry."""
    bundled_dir = tmp_path / "bundled"
    dropin_dir = tmp_path / "dropin"
    _write_schema(bundled_dir, "widget.schema.json", _valid_schema())
    _write_schema(dropin_dir, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "gadget.yaml", "category": "other", "python_package": "gadget"},
    }))

    reg = registry.build_registry(bundled_dir, dropin_dir)

    assert set(reg.entries) == {"widget", "gadget"}
    assert reg.problems == ()


def test_build_registry_without_dropin_dir_is_fine(tmp_path: Path) -> None:
    """build_registry with dropin_dir=None (or a non-existent path) does not error."""
    bundled_dir = tmp_path / "bundled"
    _write_schema(bundled_dir, "widget.schema.json", _valid_schema())

    reg = registry.build_registry(bundled_dir, tmp_path / "does-not-exist")

    assert set(reg.entries) == {"widget"}
    assert reg.problems == ()


# ---------------------------------------------------------------------------
# Step 4 -- context composition (SPEC.md §5)
# ---------------------------------------------------------------------------


def test_build_context_from_full_mimirheim_config() -> None:
    """context is built with mqtt_topic_prefix, pv_arrays, static_loads from mimirheim.yaml's data."""
    mimirheim_config = {
        "mqtt": {"topic_prefix": "custom_prefix", "host": "localhost"},
        "pv_arrays": {"roof": {"max_power_kw": 8.0}},
        "static_loads": {"base_load": {}},
    }

    context = registry.build_context(mimirheim_config)

    assert context == {
        "mqtt_topic_prefix": "custom_prefix",
        "pv_arrays": {"roof": {"max_power_kw": 8.0}},
        "static_loads": {"base_load": {}},
    }


def test_build_context_defaults_topic_prefix_when_mqtt_absent() -> None:
    """mqtt_topic_prefix falls back to the same default MimirheimConfig itself uses."""
    context = registry.build_context({})

    assert context["mqtt_topic_prefix"] == "mimir"


def test_build_context_empty_pv_arrays_and_static_loads_are_empty_objects_not_absent() -> None:
    """Empty pv_arrays/static_loads produce an empty object, never an absent key."""
    context = registry.build_context({"mqtt": {"topic_prefix": "mimir"}})

    assert context["pv_arrays"] == {}
    assert context["static_loads"] == {}
    assert "pv_arrays" in context
    assert "static_loads" in context


def test_compose_entry_schema_injects_context_for_non_mimirheim_entry(tmp_path: Path) -> None:
    """A non-mimirheim.yaml entry's composed schema gets a context property."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["widget"]

    composed = registry.compose_entry_schema(entry)

    assert "context" in composed["properties"]
    assert composed["properties"]["context"]["readOnly"] is True
    assert set(composed["properties"]["context"]["properties"]) == {
        "mqtt_topic_prefix",
        "pv_arrays",
        "static_loads",
    }


def test_compose_entry_schema_mimirheim_yaml_entry_gets_no_context_key(tmp_path: Path) -> None:
    """mimirheim.yaml's own entry gets no context key at all."""
    _write_schema(tmp_path, "mimirheim.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "mimirheim.yaml", "category": "core", "python_package": "mimirheim", "required": True},
    }))
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["mimirheim"]

    composed = registry.compose_entry_schema(entry)

    assert "context" not in composed.get("properties", {})


def test_compose_entry_schema_does_not_mutate_original(tmp_path: Path) -> None:
    """compose_entry_schema returns a copy; the entry's own schema is untouched."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["widget"]

    registry.compose_entry_schema(entry)

    assert "context" not in entry.schema.get("properties", {})


# ---------------------------------------------------------------------------
# Step 5 -- validation model (SPEC.md §7)
# ---------------------------------------------------------------------------


def test_jsonschema_pass_rejects_invalid_data(tmp_path: Path) -> None:
    """A jsonschema failure produces a non-empty error list with loc/msg."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["widget"]

    errors = registry.validate_entry(entry, {"enabled": "not-a-bool"})

    assert len(errors) == 1
    assert errors[0]["loc"] == ["enabled"]
    assert "msg" in errors[0]


def test_jsonschema_pass_accepts_valid_data(tmp_path: Path) -> None:
    """Valid data against a schema with no python_model produces no errors."""
    _write_schema(tmp_path, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["widget"]

    errors = registry.validate_entry(entry, {"enabled": True})

    assert errors == []


def test_dropin_python_model_is_ignored_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drop-in declaring python_model gets jsonschema-only validation; a warning is logged, no import attempted."""
    _write_schema(tmp_path, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {
            "file": "gadget.yaml",
            "category": "other",
            "python_package": "gadget",
            "python_model": "nonexistent_module_xyz.config:NonexistentModel",
        },
    }))
    entries, problems = registry.discover_dropins(tmp_path, bundled_ids=frozenset())
    assert problems == []
    entry = entries["gadget"]
    assert entry.is_bundled is False

    import_calls: list[str] = []
    monkeypatch.setattr(
        registry.importlib,
        "import_module",
        lambda name: import_calls.append(name) or pytest.fail("import_module must not be called for a drop-in"),
    )

    with caplog.at_level(logging.WARNING, logger="config_editor.registry"):
        errors = registry.validate_entry(entry, {"enabled": True})

    assert errors == []
    assert import_calls == []
    assert any("python_model" in record.message for record in caplog.records)
    assert any("gadget" in record.message for record in caplog.records)


def _cfgeditor_schema() -> dict[str, Any]:
    """A bundled-style fixture backed by the real ConfigEditorConfig Pydantic model.

    ``port`` is declared as a plain ``integer`` here -- deliberately without
    the model's own ``ge=1024, le=65535`` constraint -- so a submitted
    ``port: 80`` passes the jsonschema pass but fails the second, Pydantic
    pass. That is exactly the "jsonschema passes, Pydantic fails" case the
    plan calls for: it proves the second pass adds real coverage, not just
    duplicate checking.
    """
    schema = _valid_schema(**{
        "x-mimirheim": {
            "file": "cfgeditor.yaml",
            "category": "other",
            "python_package": "config_editor",
            "python_model": "config_editor.config:ConfigEditorConfig",
        },
    })
    schema["properties"]["port"] = {"type": "integer"}
    return schema


def test_bundled_python_model_second_pass_runs(tmp_path: Path) -> None:
    """A bundled entry with python_model set runs the Pydantic pass after jsonschema passes."""
    _write_schema(tmp_path, "cfgeditor.schema.json", _cfgeditor_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["cfgeditor"]
    assert entry.is_bundled is True

    errors = registry.validate_entry(entry, {"enabled": True, "port": 80})

    assert errors != []
    assert any("port" in str(e["loc"]) for e in errors)


def test_error_shape_identical_between_passes(tmp_path: Path) -> None:
    """Both the jsonschema pass and the Pydantic pass produce {"loc": [...], "msg": str} dicts."""
    _write_schema(tmp_path, "cfgeditor.schema.json", _cfgeditor_schema())
    entries, _ = registry.discover_bundled(tmp_path)
    entry = entries["cfgeditor"]

    jsonschema_errors = registry.validate_entry(entry, {"enabled": "not-a-bool"})
    pydantic_errors = registry.validate_entry(entry, {"enabled": True, "port": 80})

    for error_list in (jsonschema_errors, pydantic_errors):
        assert error_list != []
        for error in error_list:
            assert set(error) == {"loc", "msg"}
            assert isinstance(error["loc"], list)
            assert isinstance(error["msg"], str)


# ---------------------------------------------------------------------------
# Step 6 -- save semantics (SPEC.md §8)
# ---------------------------------------------------------------------------


def _registry_with_baseload_group(tmp_path: Path) -> registry.Registry:
    """Build a Registry with three exclusive-group "baseload" entries."""
    for stem, filename, pkg in (
        ("baseload-static", "baseload-static.yaml", "baseload_static"),
        ("baseload-ha", "baseload-ha.yaml", "baseload_ha"),
        ("baseload-ha-db", "baseload-ha-db.yaml", "baseload_ha_db"),
    ):
        _write_schema(tmp_path, f"{stem}.schema.json", _valid_schema(**{
            "x-mimirheim": {
                "file": filename,
                "category": "baseload",
                "python_package": pkg,
                "exclusive_group": "baseload",
            },
        }))
    entries, problems = registry.discover_bundled(tmp_path)
    assert problems == []
    return registry.Registry(entries=entries, problems=())


def test_save_rejects_whole_batch_when_one_dirty_entry_is_invalid(tmp_path: Path) -> None:
    """validate-all-then-write-all: one invalid entry blocks writing any file."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_schema(schema_dir, "widget.schema.json", _valid_schema())
    _write_schema(schema_dir, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "gadget.yaml", "category": "other", "python_package": "gadget"},
    }))
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())

    result = registry.save_entries(
        reg,
        config_dir,
        {
            "widget": {"enabled": True, "config": {"enabled": True}},
            "gadget": {"enabled": True, "config": {"enabled": "not-a-bool"}},
        },
    )

    assert result.ok is False
    assert "gadget" in result.errors
    assert not (config_dir / "widget.yaml").exists()
    assert not (config_dir / "gadget.yaml").exists()


def test_save_writes_all_when_every_dirty_entry_is_valid(tmp_path: Path) -> None:
    """When every dirty entry is valid, every file is written."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_schema(schema_dir, "widget.schema.json", _valid_schema())
    _write_schema(schema_dir, "gadget.schema.json", _valid_schema(**{
        "x-mimirheim": {"file": "gadget.yaml", "category": "other", "python_package": "gadget"},
    }))
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())

    result = registry.save_entries(
        reg,
        config_dir,
        {
            "widget": {"enabled": True, "config": {"enabled": True}},
            "gadget": {"enabled": True, "config": {"enabled": False}},
        },
    )

    assert result.ok is True
    assert set(result.written) == {"widget.yaml", "gadget.yaml"}
    assert (config_dir / "widget.yaml").exists()
    assert (config_dir / "gadget.yaml").exists()


def test_save_enabling_one_exclusive_group_member_deletes_the_others(tmp_path: Path) -> None:
    """Saving baseload-ha.yaml's entry deletes baseload-static.yaml/baseload-ha-db.yaml if present."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    reg = _registry_with_baseload_group(schema_dir)
    (config_dir / "baseload-static.yaml").write_text("some: value\n")
    (config_dir / "baseload-ha-db.yaml").write_text("some: value\n")

    result = registry.save_entries(
        reg,
        config_dir,
        {"baseload-ha": {"enabled": True, "config": {"enabled": True}}},
    )

    assert result.ok is True
    assert (config_dir / "baseload-ha.yaml").exists()
    assert not (config_dir / "baseload-static.yaml").exists()
    assert not (config_dir / "baseload-ha-db.yaml").exists()
    assert set(result.deleted) == {"baseload-static.yaml", "baseload-ha-db.yaml"}


def test_save_exclusive_group_is_read_from_registry_not_hardcoded(tmp_path: Path) -> None:
    """Exclusive-group deletion works for an arbitrary group name unrelated to baseload."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for stem, filename in (("alpha", "alpha.yaml"), ("beta", "beta.yaml")):
        _write_schema(schema_dir, f"{stem}.schema.json", _valid_schema(**{
            "x-mimirheim": {
                "file": filename,
                "category": "other",
                "python_package": stem,
                "exclusive_group": "custom-group",
            },
        }))
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())
    (config_dir / "beta.yaml").write_text("some: value\n")

    result = registry.save_entries(
        reg, config_dir, {"alpha": {"enabled": True, "config": {"enabled": True}}}
    )

    assert result.ok is True
    assert not (config_dir / "beta.yaml").exists()


def test_save_comment_preservation_survives_a_single_field_save(tmp_path: Path) -> None:
    """Saving an entry that only touches one field preserves existing comments in that file."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    schema = _valid_schema()
    schema["properties"]["name"] = {"type": "string"}
    _write_schema(schema_dir, "widget.schema.json", schema)
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())

    existing_yaml = (
        "# a hand-written comment that must survive\n"
        "enabled: true\n"
        "name: original\n"
    )
    (config_dir / "widget.yaml").write_text(existing_yaml)

    result = registry.save_entries(
        reg,
        config_dir,
        {"widget": {"enabled": True, "config": {"enabled": True, "name": "changed"}}},
    )

    assert result.ok is True
    written_text = (config_dir / "widget.yaml").read_text()
    assert "# a hand-written comment that must survive" in written_text
    assert "changed" in written_text


def test_save_unknown_entry_id_raises(tmp_path: Path) -> None:
    """A save request referencing an id the registry does not know raises UnknownEntryError."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_schema(schema_dir, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())

    with pytest.raises(registry.UnknownEntryError):
        registry.save_entries(
            reg, config_dir, {"does-not-exist": {"enabled": True, "config": {}}}
        )


def test_save_deletes_disabled_entry_file(tmp_path: Path) -> None:
    """An entry submitted with enabled: false has its file deleted, no validation required."""
    schema_dir = tmp_path / "schemas"
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    _write_schema(schema_dir, "widget.schema.json", _valid_schema())
    entries, _ = registry.discover_bundled(schema_dir)
    reg = registry.Registry(entries=entries, problems=())
    (config_dir / "widget.yaml").write_text("enabled: true\n")

    result = registry.save_entries(reg, config_dir, {"widget": {"enabled": False}})

    assert result.ok is True
    assert not (config_dir / "widget.yaml").exists()
    assert result.deleted == ("widget.yaml",)
