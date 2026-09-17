"""Round-trip validation tests for MimirheimConfig.

Historically this module also asserted that every helper config model's
JSON Schema carried `ui_label`/`ui_group` `json_schema_extra` hints, from
before FormSpec existed (see `mimirheim_shared.formspec`). Every helper now
has its own FormSpec and `ConfigOwnerSupport` wiring, the same as mimirheim
core's `mimirheim/config/formspec.py`, so those hints are gone from every
config model and the coverage tests that checked for them have been removed.
Each helper's FormSpec is instead checked for alignment with its model by
`mimirheim_shared.alignment.assert_form_spec_complete` in that helper's own
`tests/unit/test_formspec.py`.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from mimirheim.config.schema import MimirheimConfig


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
