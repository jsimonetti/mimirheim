# Step 38 — Schema UI annotations and coverage test

## Purpose

This step annotates every field in the mimirheim Pydantic configuration schema
with UI metadata, exports the annotated schema to a committed JSON file, and
introduces tests that make schema-to-editor drift a CI failure rather than a
silent product defect.

No editor is built in this step. The deliverable is a tested, annotated schema
that step 39 can consume directly. The annotations are also useful standalone:
they make `schema.json` a machine-readable source for documentation generators
and future tooling.

---

## References

- `mimirheim/config/schema.py` — the single source of truth for all config models
- `tests/unit/test_config_schema.py` — existing config validation tests
- IMPLEMENTATION_DETAILS §1 — Pydantic `extra="forbid"` rule
- AGENTS.md — "every Pydantic model must include `extra="forbid"`"

---

## Design: annotation keys

Add the following keys to `json_schema_extra` on every `Field()` call in
`mimirheim/config/schema.py`. All keys are prefixed `ui_` to namespace them
away from standard JSON Schema vocabulary.

| Key | Type | Required | Purpose |
|---|---|---|---|
| `ui_label` | `str` | Yes, on all fields | Human-readable field label for form inputs. Plain English, no jargon. Example: `"Battery capacity"` |
| `ui_unit` | `str \| None` | When the field has a physical unit | Unit spelled out for non-technical readers. Example: `"kilowatt-hours (kWh)"`, `"kilowatts (kW)"`, `"euros per kWh"`, `"°C"`. Not needed for booleans, strings, or pure counts. |
| `ui_hint` | `str \| None` | When the value source is non-obvious | One sentence explaining where to find the value or what it controls. Example: `"Found on the inverter datasheet as 'usable capacity'."` |
| `ui_group` | `"basic" \| "advanced"` | Yes, on all fields | `"basic"` fields are shown to first-time users; `"advanced"` fields are collapsed by default. Every field without a default must be `"basic"`. |

### Named-map device key annotation (`ui_instance_name_description`)

Named-map devices (batteries, PV arrays, EV chargers, etc.) use Python `dict`
keys as device names. These names become MQTT topic path segments. Since the
dict key is not a Pydantic field, this annotation is placed on the surrounding
model via `model_config`:

```python
class BatteryConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "ui_instance_name_description": (
                "A short identifier for this battery, e.g. 'home_battery'. "
                "This name appears in all MQTT topic paths for this device "
                "(e.g. mimir/input/battery/<name>/soc). "
                "Changing it after initial setup will break any automations "
                "that reference those topics."
            ),
        },
    )
```

Apply the same pattern to `EvConfig`, `PvConfig`, `HybridInverterConfig`,
`DeferrableLoadConfig`, `StaticLoadConfig`, `ThermalBoilerConfig`,
`SpaceHeatingConfig`, and `CombiHeatPumpConfig`.

---

## Grouping guide

The following table documents the intended `ui_group` assignment for the main
device models. Use it as reference while annotating to keep groupings consistent.

### `BatteryConfig`

| Field | Group | Notes |
|---|---|---|
| `capacity_kwh` | basic | Required |
| `min_soc_kwh` | basic | Required |
| `charge_segments` / `charge_efficiency_curve` | basic | Required (one of the two) |
| `discharge_segments` / `discharge_efficiency_curve` | basic | Required |
| `wear_cost_eur_per_kwh` | basic | — |
| `optimal_lower_soc_kwh` | advanced | Optional SOC penalty |
| `soc_low_penalty_eur_per_kwh_h` | advanced | — |
| `reduce_charge_above_soc_kwh` | advanced | Optional derating |
| `reduce_charge_min_kw` | advanced | — |
| `reduce_discharge_below_soc_kwh` | advanced | — |
| `reduce_discharge_min_kw` | advanced | — |
| `capabilities.*` | advanced | Hardware flags |
| `inputs.*` | advanced | MQTT topic overrides |
| `outputs.*` | advanced | MQTT topic overrides |

Apply the same principle to all devices: fields that are required or have the
highest user impact are `"basic"`; tuning parameters, MQTT overrides, and
hardware flags are `"advanced"`.

---

## Files to modify

- `mimirheim/config/schema.py` — add `json_schema_extra` to every `Field()` call
  and `ui_instance_name_description` to device model `model_config` entries
- `mimirheim_helpers/prices/nordpool/nordpool/config.py`
- `mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py`
- `mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py`
- `mimirheim_helpers/baseload/static/baseload_static/config.py`
- `mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py`
- `mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py`
- `mimirheim_helpers/reporter/reporter/config.py`
- `mimirheim_helpers/scheduler/scheduler/config.py`

## Files to create

- `mimirheim/config/schema.json` — committed snapshot of `MimirheimConfig.model_json_schema()`
- `scripts/generate_schema_json.py` — one-shot script to regenerate `schema.json`
- `tests/unit/test_schema_ui_annotations.py` — coverage and round-trip tests

---

## Tests first

Create `tests/unit/test_schema_ui_annotations.py` with the following tests.
All tests must fail before any annotations are added to `schema.py`.

### Coverage test: every field has a `ui_label`

Walk the JSON Schema produced by `MimirheimConfig.model_json_schema()`. For
every property — required or optional — assert that a `"ui_label"` key is
present. Collect all violations and report them together rather than failing
on the first missing annotation, to make the output actionable.

```python
def test_all_fields_have_ui_label() -> None:
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_labels(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields are missing ui_label annotations:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
```

`_collect_missing_ui_labels` is a recursive helper that walks `properties`
and `$defs` dict entries. It does not skip optional fields.

### Coverage test: every field has a `ui_group`

Same walk, but assert that every property (required or optional) has a
`"ui_group"` key with value `"basic"` or `"advanced"`.

```python
def test_all_fields_have_ui_group() -> None:
    schema = MimirheimConfig.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_group(schema, path="MimirheimConfig", violations=violations)
    assert not violations, (
        "The following fields are missing ui_group annotations:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
```

### Coverage test: named-map models have `ui_instance_name_description`

For each named-map device model class (battery, PV, EV, etc.), assert that
`model.model_json_schema()` contains a top-level `"ui_instance_name_description"`
key.

```python
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
    schema = model_cls.model_json_schema()
    assert "ui_instance_name_description" in schema, (
        f"{model_cls.__name__} is missing ui_instance_name_description in model_config"
    )
```

### Schema file freshness test

Assert that the committed `mimirheim/config/schema.json` matches the live
output of `MimirheimConfig.model_json_schema()`. If they diverge, the error
message instructs the developer to run `python scripts/generate_schema_json.py`.

```python
def test_schema_json_is_up_to_date() -> None:
    schema_path = Path(__file__).parents[2] / "mimirheim" / "config" / "schema.json"
    live = MimirheimConfig.model_json_schema()
    committed = json.loads(schema_path.read_text())
    assert live == committed, (
        "mimirheim/config/schema.json is out of date. "
        "Run: python scripts/generate_schema_json.py"
    )
```

### Round-trip test: a minimal valid config validates cleanly

```python
def test_wizard_minimal_output_validates() -> None:
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
            "roof_pv": {"max_power_kw": 8.0}
        },
        "static_loads": {"base_load": {}},
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["home_battery"].capacity_kwh == 13.5
```

### Round-trip test: deliberately invalid values are rejected

```python
def test_wizard_invalid_output_is_rejected() -> None:
    raw = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": -1.0, "export_limit_kw": 25.0},  # negative import: invalid
    }
    with pytest.raises(ValidationError):
        MimirheimConfig.model_validate(raw)
```

Run `uv run pytest tests/unit/test_schema_ui_annotations.py` — all tests must
fail before annotations are added.

---

## Implementation

### 1. Annotate `mimirheim/config/schema.py`

Add `json_schema_extra` to every `Field()` call throughout `schema.py`.
Work section by section in the order the file is structured:

1. Infrastructure section: `MqttConfig`, `GridConfig`
2. Strategy section: `BalancedWeightsConfig`, `ObjectivesConfig`, `ConstraintsConfig`
3. Tuning section: `SolverConfig`, `ReadinessConfig`, `ControlConfig`
4. I/O section: `InputsConfig`, `OutputsConfig`, `HomeAssistantConfig`, `DebugConfig`, `ReportingConfig`
5. Shared sub-models: `SocTopicConfig`, `EfficiencySegment`, `EfficiencyBreakpoint`
6. Battery section: all battery models, ending with `BatteryConfig`
7. EV section: all EV models
8. PV section: all PV models
9. Hybrid inverter section
10. Load sections: deferrable, static
11. Thermal sections: boiler, space heating, combi heat pump
12. Building thermal model section
13. `MimirheimConfig` top-level fields

After each section, run the test suite to check that `test_all_fields_have_ui_label`
makes progress rather than doing everything in one large commit.

### 2. Add `ui_instance_name_description` to named-map model configs

In the `model_config = ConfigDict(extra="forbid", ...)` of each device model
listed in the test above, add the `json_schema_extra` key. See the
`BatteryConfig` example in the design section.

### 3. Write `scripts/generate_schema_json.py`

```python
"""Regenerate mimirheim/config/schema.json from the live Pydantic schema.

Run this script whenever mimirheim/config/schema.py is modified:

    python scripts/generate_schema_json.py

The output file is committed to the repository. A test in
tests/unit/test_schema_ui_annotations.py asserts that the committed file
matches the live output, so CI will catch staleness automatically.
"""
import json
from pathlib import Path

from mimirheim.config.schema import MimirheimConfig

OUTPUT = Path(__file__).parents[1] / "mimirheim" / "config" / "schema.json"

def main() -> None:
    schema = MimirheimConfig.model_json_schema()
    OUTPUT.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"Written: {OUTPUT}")

if __name__ == "__main__":
    main()
```

### 4. Annotate all helper config modules

Apply the same annotation pass (`ui_label`, `ui_unit`, `ui_hint`, `ui_group`
on every `Field()`) to each of the eight helper config files listed in the
files-to-modify section above. Work module by module. The same rules apply:
`ui_label` and `ui_group` are mandatory on every field; `ui_unit` and
`ui_hint` where appropriate.

Helper schemas are not exported to individual JSON files — only
`mimirheim/config/schema.json` is committed. The coverage tests below are
sufficient to guard against drift.

Add the following parameterised test to `test_schema_ui_annotations.py`
after annotating the helper modules:

```python
import pytest
from nordpool.config import NordpoolConfig
from pv_fetcher.config import PvFetcherConfig
from pv_ml_learner.config import PvLearnerConfig
from baseload_static.config import BaseloadConfig as BaseloadStaticConfig
from baseload_ha.config import BaseloadConfig as BaseloadHaConfig
from baseload_ha_db.config import BaseloadConfig as BaseloadHaDbConfig
from reporter.config import ReporterConfig
from scheduler.config import SchedulerConfig

@pytest.mark.parametrize("model_cls", [
    NordpoolConfig,
    PvFetcherConfig,
    PvLearnerConfig,
    BaseloadStaticConfig,
    BaseloadHaConfig,
    BaseloadHaDbConfig,
    ReporterConfig,
    SchedulerConfig,
])
def test_helper_all_fields_have_ui_label(model_cls: type) -> None:
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_labels(schema, path=model_cls.__name__, violations=violations)
    assert not violations, (
        f"{model_cls.__name__}: the following fields are missing ui_label:\n"
        + "\n".join(f"  {v}" for v in violations)
    )

@pytest.mark.parametrize("model_cls", [
    NordpoolConfig,
    PvFetcherConfig,
    PvLearnerConfig,
    BaseloadStaticConfig,
    BaseloadHaConfig,
    BaseloadHaDbConfig,
    ReporterConfig,
    SchedulerConfig,
])
def test_helper_all_fields_have_ui_group(model_cls: type) -> None:
    schema = model_cls.model_json_schema()
    violations: list[str] = []
    _collect_missing_ui_group(schema, path=model_cls.__name__, violations=violations)
    assert not violations, (
        f"{model_cls.__name__}: the following fields are missing ui_group:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
```

### 5. Generate `schema.json`

```bash
uv run python scripts/generate_schema_json.py
```

Commit the output. From this point forward, any change to `schema.py` that is
not followed by a `schema.json` regeneration will cause `test_schema_json_is_up_to_date`
to fail in CI.

---

## Acceptance criteria

- `uv run pytest tests/unit/test_schema_ui_annotations.py` passes with zero failures.
- `uv run pytest` (full suite) shows no regressions.
- `mimirheim/config/schema.json` is committed and matches the live schema output.
- Every `Field()` call in `schema.py` and in all eight helper config modules
  has `json_schema_extra` with at minimum `ui_label` and `ui_group`.
- Every named-map device model's `model_config` has `ui_instance_name_description`.

---

## Commit

```bash
git add mimirheim/config/schema.py mimirheim/config/schema.json \
        scripts/generate_schema_json.py \
        tests/unit/test_schema_ui_annotations.py \
        mimirheim_helpers/prices/nordpool/nordpool/config.py \
        mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py \
        mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py \
        mimirheim_helpers/baseload/static/baseload_static/config.py \
        mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py \
        mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py \
        mimirheim_helpers/reporter/reporter/config.py \
        mimirheim_helpers/scheduler/scheduler/config.py
git commit -m "feat: add UI annotations to config schema and all helper configs

Add ui_label, ui_unit, ui_hint, and ui_group to every Field() in
MimirheimConfig, all sub-models, and all eight helper config modules.
Add ui_instance_name_description to named-map device models.

Coverage applies to all fields (required and optional alike).

Export schema to mimirheim/config/schema.json. Coverage tests assert
that every field in every config module is annotated and that the
committed JSON file stays in sync with the live Pydantic output.

This is the foundation for the config editor service (step 39).
"
```
