# Step 02 — Config schema

## References

- IMPLEMENTATION_DETAILS §1 (Pydantic v2 rationale, `extra="forbid"`)
- IMPLEMENTATION_DETAILS §3 (typed sections rationale)
- README.md (config YAML schema — all sections and fields)

---

## Files to create

- `mimirheim/config/schema.py`
- `mimirheim/config/example.yaml`
- `tests/unit/test_config_schema.py`

---

## Tests first

Create `tests/unit/test_config_schema.py`. All tests must fail (ImportError counts as fail) before any implementation exists.

### Happy path

- `test_efficiency_segment_valid` — construct with `power_max_kw=2.0`, `efficiency=0.95`
- `test_battery_config_valid` — full `BatteryConfig` with two charge segments and one discharge segment
- `test_ev_config_valid` — full `EvConfig` including `target_soc_kwh`
- `test_pv_config_valid`
- `test_deferrable_load_config_valid`
- `test_static_load_config_valid`
- `test_grid_config_valid`
- `test_hioo_config_valid` — full config with one battery, one PV array, one EV charger
- `test_hioo_config_minimal` — config with only `grid` and one `static_loads` entry

### Sad path

- `test_efficiency_segment_zero_efficiency_rejected` — `efficiency=0.0` must raise `ValidationError`
- `test_efficiency_segment_above_one_rejected` — `efficiency=1.01` must raise
- `test_battery_config_negative_capacity_rejected` — `capacity_kwh=-1.0` must raise
- `test_hioo_config_extra_field_rejected` — unknown top-level field must raise
- `test_battery_config_extra_field_rejected` — unknown field on `BatteryConfig` must raise
- `test_balanced_weights_config_valid` — construct `BalancedWeightsConfig` with `cost_weight=1.0`, `self_sufficiency_weight=2.0`
- `test_objectives_config_without_balanced_weights` — `ObjectivesConfig()` with no `balanced_weights` must succeed (it is optional)
- `test_hioo_config_duplicate_device_names_rejected` — same name in `batteries` and `ev_chargers` must raise

Run `uv run pytest tests/unit/test_config_schema.py` — all tests must fail before proceeding.

---

## Implementation

Implement all models in `mimirheim/config/schema.py`. Every model must have:

```python
model_config = ConfigDict(extra="forbid")
```

### Models

**`EfficiencySegment`**
- `power_max_kw: float` — Field(gt=0); maximum power through this segment in kW
- `efficiency: float` — Field(gt=0, le=1.0); round-trip efficiency fraction for this power range

**`BatteryConfig`**
- `capacity_kwh: float` — Field(gt=0)
- `min_soc_kwh: float` — Field(ge=0, default=0.0)
- `charge_segments: list[EfficiencySegment]` — Field(min_length=1)
- `discharge_segments: list[EfficiencySegment]` — Field(min_length=1)
- `wear_cost_eur_per_kwh: float` — Field(ge=0, default=0.0)
- `staged_control: bool` — default True; enables binary mode variable (see §8)
- `zero_export_support: bool` — default True

**`EvConfig`**
- `capacity_kwh: float` — Field(gt=0)
- `min_soc_kwh: float` — Field(ge=0, default=0.0)
- `target_soc_kwh: float` — Field(ge=0)
- `charge_segments: list[EfficiencySegment]` — Field(min_length=1)
- `discharge_segments: list[EfficiencySegment]` — default empty list (V2H optional)
- `wear_cost_eur_per_kwh: float` — Field(ge=0, default=0.0)
- `staged_control: bool` — default True

**`PvConfig`**
- `max_power_kw: float` — Field(gt=0); used for clipping unreasonable forecast values
- `topic_forecast: str` — MQTT topic for the power forecast

**`DeferrableLoadConfig`**
- `power_kw: float` — Field(gt=0); fixed power draw when active
- `duration_steps: int` — Field(gt=0); number of consecutive steps the load must run
- `topic_window_earliest: str` — MQTT topic supplying the earliest start datetime
- `topic_window_latest: str` — MQTT topic supplying the latest end datetime

**`StaticLoadConfig`**
- `topic_forecast: str` — MQTT topic for the per-step power forecast

**`GridConfig`**
- `import_limit_kw: float` — Field(ge=0)
- `export_limit_kw: float` — Field(ge=0)

**`BalancedWeightsConfig`**
- `cost_weight: float` — Field(ge=0)
- `self_sufficiency_weight: float` — Field(ge=0)

**`ConstraintsConfig`**
- `max_import_kw: float | None` — default None
- `max_export_kw: float | None` — default None

**`ObjectivesConfig`**
- `balanced_weights: BalancedWeightsConfig | None` — default None; used only when strategy is "balanced"

**`MqttConfig`**
- `host: str`
- `port: int` — default 1883
- `client_id: str`
- `topic_prefix: str` — default "mimirheim"

**`OutputsConfig`**
- `schedule: str` — topic for full schedule JSON
- `current: str` — topic for current-step summary
- `last_solve: str` — topic for solve status

**`DebugConfig`**
- `dump_dir: Path | None` — default None
- `max_dumps: int` — Field(ge=0, default=50)

**`MimirheimConfig`**
- `batteries: dict[str, BatteryConfig]` — default empty
- `pv_arrays: dict[str, PvConfig]` — default empty
- `ev_chargers: dict[str, EvConfig]` — default empty
- `deferrable_loads: dict[str, DeferrableLoadConfig]` — default empty
- `static_loads: dict[str, StaticLoadConfig]` — default empty
- `grid: GridConfig`
- `objectives: ObjectivesConfig`
- `constraints: ConstraintsConfig` — default ConstraintsConfig()
- `mqtt: MqttConfig`
- `outputs: OutputsConfig`
- `debug: DebugConfig` — default DebugConfig()
- `model_validator(mode="after")`: `device_names_unique` — collects all keys from all named-device dicts, raises `ValueError` if any name appears in more than one section or appears more than once

### load_config helper

```python
def load_config(path: str) -> MimirheimConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)
    return MimirheimConfig.model_validate(raw)
```

### mimirheim/config/example.yaml

Write an annotated example config covering at least one of every device type. This file is documentation; it must be valid (parseable by `load_config`).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_config_schema.py
```

All tests green.

---

## Done

```bash
mv plans/02_config_schema.md plans/done/
```
