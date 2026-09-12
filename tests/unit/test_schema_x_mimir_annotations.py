"""Tests for x-mimir-label/x-mimir-group annotation coverage on all mimirheim
Pydantic config models.

Verifies that every field carrying a ui_label also carries an identical
x-mimir-label, and that every field carrying a ui_group also carries the
correspondingly mapped x-mimir-group ("basic" -> "Basic",
"advanced" -> "Advanced"). This is the coverage test for step 71_6's
mechanical migration of config-editor (v1)'s ui_label/ui_group hints into
the x-mimir- namespace config-editor-v2's adapter reads.

What this module does not do:
- It does not test that ui_label/ui_group are themselves present and valid
  (that is test_schema_ui_annotations.py's job).
- It does not test config-editor-v2's own translation logic
  (jedison_mapping.py); that is config-editor-v2's own test suite.
"""
from __future__ import annotations

from typing import Any

import pytest

from mimirheim.config.schema import MimirheimConfig

from config_editor_v2.registry import REGISTRY, resolve_model

_GROUP_MAP = {"basic": "Basic", "advanced": "Advanced"}


def _collect_missing_x_mimir_label(
    schema: dict[str, Any],
    path: str,
    violations: list[str],
    *,
    _visited: set[str] | None = None,
) -> None:
    """Recursively walk a JSON Schema dict and collect ui_label/x-mimir-label mismatches.

    Mirrors test_schema_ui_annotations.py's `_collect_missing_ui_labels` walk,
    but checks that every field carrying `ui_label` also carries an identical
    `x-mimir-label`, rather than checking for `ui_label` itself.

    Args:
        schema: The JSON Schema dict to walk.
        path: Dotted path prefix for reporting (e.g. "MimirheimConfig").
        violations: List to append violation strings to.
        _visited: Internal set of already-visited ``$defs`` names to prevent
            infinite recursion on self-referential schemas.
    """
    if _visited is None:
        _visited = set()

    defs = schema.get("$defs", {})

    def _walk(node: dict[str, Any], node_path: str) -> None:
        props = node.get("properties", {})
        for field_name, field_schema in props.items():
            field_path = f"{node_path}.{field_name}"
            ref = field_schema.get("$ref", "")
            if ref.startswith("#/$defs/"):
                ref_name = ref[len("#/$defs/"):]
                if ref_name not in _visited:
                    _visited.add(ref_name)
                    _walk(defs.get(ref_name, {}), field_path)
                continue
            for key in ("anyOf", "allOf", "oneOf"):
                for variant in field_schema.get(key, []):
                    vref = variant.get("$ref", "")
                    if vref.startswith("#/$defs/"):
                        vref_name = vref[len("#/$defs/"):]
                        if vref_name not in _visited:
                            _visited.add(vref_name)
                            _walk(defs.get(vref_name, {}), field_path)
            ui_label = field_schema.get("ui_label")
            if ui_label is None:
                continue
            x_mimir_label = field_schema.get("x-mimir-label")
            if x_mimir_label is None:
                violations.append(f"{field_path} (missing x-mimir-label)")
            elif x_mimir_label != ui_label:
                violations.append(
                    f"{field_path} (x-mimir-label {x_mimir_label!r} != ui_label {ui_label!r})"
                )

    _walk(schema, path)

    for def_name, def_schema in defs.items():
        if def_name not in _visited:
            _visited.add(def_name)
            _walk(def_schema, def_name)


def _collect_missing_x_mimir_group(
    schema: dict[str, Any],
    path: str,
    violations: list[str],
    *,
    _visited: set[str] | None = None,
) -> None:
    """Recursively walk a JSON Schema dict and collect ui_group/x-mimir-group mismatches.

    Mirrors test_schema_ui_annotations.py's `_collect_missing_ui_group` walk,
    but checks that every field carrying `ui_group` also carries the
    corresponding mapped `x-mimir-group`.
    """
    if _visited is None:
        _visited = set()

    defs = schema.get("$defs", {})

    def _walk(node: dict[str, Any], node_path: str) -> None:
        props = node.get("properties", {})
        for field_name, field_schema in props.items():
            field_path = f"{node_path}.{field_name}"
            ref = field_schema.get("$ref", "")
            if ref.startswith("#/$defs/"):
                ref_name = ref[len("#/$defs/"):]
                if ref_name not in _visited:
                    _visited.add(ref_name)
                    _walk(defs.get(ref_name, {}), field_path)
                continue
            for key in ("anyOf", "allOf", "oneOf"):
                for variant in field_schema.get(key, []):
                    vref = variant.get("$ref", "")
                    if vref.startswith("#/$defs/"):
                        vref_name = vref[len("#/$defs/"):]
                        if vref_name not in _visited:
                            _visited.add(vref_name)
                            _walk(defs.get(vref_name, {}), field_path)
            ui_group = field_schema.get("ui_group")
            if ui_group is None:
                continue
            expected = _GROUP_MAP.get(ui_group)
            x_mimir_group = field_schema.get("x-mimir-group")
            if x_mimir_group != expected:
                violations.append(
                    f"{field_path} (x-mimir-group {x_mimir_group!r} != expected {expected!r} "
                    f"for ui_group {ui_group!r})"
                )

    _walk(schema, path)

    for def_name, def_schema in defs.items():
        if def_name not in _visited:
            _visited.add(def_name)
            _walk(def_schema, def_name)


# ---------------------------------------------------------------------------
# MimirheimConfig coverage tests
# ---------------------------------------------------------------------------

def test_every_ui_label_has_a_matching_x_mimir_label() -> None:
    """Every field with a ui_label in MimirheimConfig must have an identical x-mimir-label."""
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_x_mimir_label(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields have a ui_label with no matching x-mimir-label:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_every_ui_group_has_a_matching_x_mimir_group() -> None:
    """Every field with a ui_group in MimirheimConfig must have the mapped x-mimir-group."""
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_x_mimir_group(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields have a ui_group with no matching x-mimir-group:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# Registered helper model coverage tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.name)
def test_helper_every_ui_label_has_a_matching_x_mimir_label(entry: Any) -> None:
    """Every field with a ui_label in each registered model must have a matching x-mimir-label."""
    model_cls = resolve_model(entry)
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_x_mimir_label(schema, path=entry.name, violations=violations)
    assert not violations, (
        f"{entry.name} ({entry.model_path}): the following fields have a ui_label "
        "with no matching x-mimir-label:\n" + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.parametrize("entry", REGISTRY, ids=lambda e: e.name)
def test_helper_every_ui_group_has_a_matching_x_mimir_group(entry: Any) -> None:
    """Every field with a ui_group in each registered model must have the mapped x-mimir-group."""
    model_cls = resolve_model(entry)
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_x_mimir_group(schema, path=entry.name, violations=violations)
    assert not violations, (
        f"{entry.name} ({entry.model_path}): the following fields have a ui_group "
        "with no matching x-mimir-group:\n" + "\n".join(f"  {v}" for v in violations)
    )
