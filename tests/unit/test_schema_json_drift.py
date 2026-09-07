"""Drift and round-trip tests for `mimirheim/config/schema.json`.

Verifies that the committed `schema.json` file stays in sync with the live
`MimirheimConfig.model_json_schema()` output, and that a couple of
representative config dicts (as a setup wizard would produce) validate as
expected.

Split out of the former `test_schema_ui_annotations.py` (deleted by plan 68
Decision 10, which migrated the `ui_label`/`ui_group` vocabulary that the
rest of that file tested for). This module covers a distinct concern --
`schema.json` staleness and Pydantic round-tripping -- that has nothing to do
with the field vocabulary and so survives that deletion.

What this module does not do:
- It does not check the bundled config-editor schemas under
  `mimirheim_helpers/config_editor/config_editor/schemas/bundled/` --
  that is `mimirheim_helpers/config_editor/tests/unit/test_schema_drift.py`.
- It does not test the config editor UI itself (that is test_config_editor_*.py).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from mimirheim.config.schema import MimirheimConfig


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
