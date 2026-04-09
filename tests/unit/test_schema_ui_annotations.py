"""Tests for UI annotation coverage on all mimirheim Pydantic config models.

Verifies that every field in every config model (main and helper) has the
required ui_label and ui_group annotations, that named-map device models
carry ui_instance_name_description, and that the committed schema.json file
stays in sync with the live Pydantic output.

What this module does not do:
- It does not test the correctness of annotation values (e.g. whether a
  ui_label is grammatically sound). That is a human review concern.
- It does not test the config editor UI itself (that is test_config_editor_*.py).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from mimirheim.config.schema import (
    BatteryConfig,
    CombiHeatPumpConfig,
    DeferrableLoadConfig,
    EvConfig,
    HybridInverterConfig,
    MimirheimConfig,
    PvConfig,
    SpaceHeatingConfig,
    StaticLoadConfig,
    ThermalBoilerConfig,
)


# ---------------------------------------------------------------------------
# Recursive annotation walker
# ---------------------------------------------------------------------------

def _collect_missing_ui_labels(
    schema: dict[str, Any],
    path: str,
    violations: list[str],
    *,
    _visited: set[str] | None = None,
) -> None:
    """Recursively walk a JSON Schema dict and collect fields missing ui_label.

    Walks ``properties`` at the current level and recurses into ``$defs`` for
    referenced sub-models. Does not skip optional fields (fields that have a
    ``default`` key): every field must have a ui_label regardless of whether
    it is required.

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
            # Resolve $ref if the field is a reference.
            ref = field_schema.get("$ref", "")
            if ref.startswith("#/$defs/"):
                ref_name = ref[len("#/$defs/"):]
                if ref_name not in _visited:
                    _visited.add(ref_name)
                    ref_schema = defs.get(ref_name, {})
                    _walk(ref_schema, field_path)
                continue
            # anyOf / allOf / oneOf: check each variant inline.
            for key in ("anyOf", "allOf", "oneOf"):
                for variant in field_schema.get(key, []):
                    vref = variant.get("$ref", "")
                    if vref.startswith("#/$defs/"):
                        vref_name = vref[len("#/$defs/"):]
                        if vref_name not in _visited:
                            _visited.add(vref_name)
                            _walk(defs.get(vref_name, {}), field_path)
            if "ui_label" not in field_schema:
                violations.append(field_path)

    _walk(schema, path)

    # Also walk any $defs not yet visited (e.g. shared sub-models).
    for def_name, def_schema in defs.items():
        if def_name not in _visited:
            _visited.add(def_name)
            _walk(def_schema, def_name)


def _collect_missing_ui_group(
    schema: dict[str, Any],
    path: str,
    violations: list[str],
    *,
    _visited: set[str] | None = None,
) -> None:
    """Recursively walk a JSON Schema dict and collect fields missing ui_group."""
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
            if "ui_group" not in field_schema:
                violations.append(field_path)
            elif field_schema["ui_group"] not in ("basic", "advanced"):
                violations.append(
                    f"{field_path} (invalid ui_group value: {field_schema['ui_group']!r})"
                )

    _walk(schema, path)

    for def_name, def_schema in defs.items():
        if def_name not in _visited:
            _visited.add(def_name)
            _walk(def_schema, def_name)


# ---------------------------------------------------------------------------
# MimirheimConfig coverage tests
# ---------------------------------------------------------------------------

def test_all_fields_have_ui_label() -> None:
    """Every field in MimirheimConfig and all sub-models must have a ui_label."""
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_labels(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields are missing ui_label annotations:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


def test_all_fields_have_ui_group() -> None:
    """Every field in MimirheimConfig and all sub-models must have a valid ui_group."""
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_group(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields are missing ui_group annotations:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.parametrize("model_cls", [
    BatteryConfig,
    PvConfig,
    EvConfig,
    HybridInverterConfig,
    DeferrableLoadConfig,
    StaticLoadConfig,
    ThermalBoilerConfig,
    SpaceHeatingConfig,
    CombiHeatPumpConfig,
])
def test_named_map_model_has_instance_name_description(model_cls: type) -> None:
    """Named-map device models must carry ui_instance_name_description in their schema."""
    schema = model_cls.model_json_schema()
    assert "ui_instance_name_description" in schema, (
        f"{model_cls.__name__} is missing ui_instance_name_description in model_config. "
        "Add it via json_schema_extra in ConfigDict."
    )


# ---------------------------------------------------------------------------
# schema.json freshness test
# ---------------------------------------------------------------------------

def test_schema_json_is_up_to_date() -> None:
    """The committed schema.json must match the live MimirheimConfig.model_json_schema() output.

    If this test fails, regenerate the file:
        python scripts/generate_schema_json.py
    """
    schema_path = Path(__file__).parents[2] / "mimirheim" / "config" / "schema.json"
    assert schema_path.exists(), (
        f"schema.json not found at {schema_path}. "
        "Run: python scripts/generate_schema_json.py"
    )
    live = MimirheimConfig.model_json_schema()
    committed = json.loads(schema_path.read_text())
    assert live == committed, (
        "mimirheim/config/schema.json is out of date. "
        "Run: python scripts/generate_schema_json.py"
    )


# ---------------------------------------------------------------------------
# Round-trip tests
# ---------------------------------------------------------------------------

def test_wizard_minimal_output_validates() -> None:
    """A minimal config dict (as a wizard would produce) validates cleanly."""
    raw = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 13.5,
                "min_soc_kwh": 1.4,
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"unit": "percent"}},
            }
        },
        "pv_arrays": {
            "roof_pv": {"max_power_kw": 8.0},
        },
        "static_loads": {"base_load": {}},
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["home_battery"].capacity_kwh == 13.5


def test_wizard_invalid_output_is_rejected() -> None:
    """A config dict with an invalid field value is rejected by Pydantic."""
    raw = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": -1.0, "export_limit_kw": 25.0},
    }
    with pytest.raises(ValidationError):
        MimirheimConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# Helper config coverage tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("model_cls_import", [
    ("nordpool.config", "NordpoolConfig"),
    ("pv_fetcher.config", "PvFetcherConfig"),
    ("pv_ml_learner.config", "PvLearnerConfig"),
    ("baseload_static.config", "BaseloadConfig"),
    ("baseload_ha.config", "BaseloadConfig"),
    ("baseload_ha_db.config", "BaseloadConfig"),
    ("reporter.config", "ReporterConfig"),
    ("scheduler.config", "SchedulerConfig"),
])
def test_helper_all_fields_have_ui_label(model_cls_import: tuple[str, str]) -> None:
    """Every field in each helper config model must have a ui_label annotation."""
    module_name, class_name = model_cls_import
    import importlib
    module = importlib.import_module(module_name)
    model_cls = getattr(module, class_name)
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_labels(schema, path=class_name, violations=violations)
    assert not violations, (
        f"{class_name} ({module_name}): the following fields are missing ui_label:\n"
        + "\n".join(f"  {v}" for v in violations)
    )


@pytest.mark.parametrize("model_cls_import", [
    ("nordpool.config", "NordpoolConfig"),
    ("pv_fetcher.config", "PvFetcherConfig"),
    ("pv_ml_learner.config", "PvLearnerConfig"),
    ("baseload_static.config", "BaseloadConfig"),
    ("baseload_ha.config", "BaseloadConfig"),
    ("baseload_ha_db.config", "BaseloadConfig"),
    ("reporter.config", "ReporterConfig"),
    ("scheduler.config", "SchedulerConfig"),
])
def test_helper_all_fields_have_ui_group(model_cls_import: tuple[str, str]) -> None:
    """Every field in each helper config model must have a valid ui_group annotation."""
    module_name, class_name = model_cls_import
    import importlib
    module = importlib.import_module(module_name)
    model_cls = getattr(module, class_name)
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_group(schema, path=class_name, violations=violations)
    assert not violations, (
        f"{class_name} ({module_name}): the following fields are missing ui_group:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
