# Plan 63 — Computed output-capability properties on device config models

## Purpose

`mqtt_publisher.py` and `ha_discovery.py` both read `capabilities` and `outputs`
fields on device config objects to decide which topics to publish or register.
Each decision requires a compound guard of the form:

```python
cfg.capabilities.zero_exchange and cfg.outputs.exchange_mode is not None
```

These identical or near-identical expressions appear independently in both
modules, meaning the same rule is expressed in two places. Any change to the
rule (e.g. adding a third condition) must be applied twice, and it is easy for
the two copies to drift.

This plan adds computed `@property` methods to `BatteryConfig`, `EvConfig`,
`PvConfig`, and `HybridInverterConfig` that centralise each guard. The IO
modules are then updated to call the property instead of repeating the
expression. No behaviour changes; this is a pure readability and
maintainability improvement.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §1 — Pydantic models use `extra="forbid"`. Properties are not fields and are
  unaffected by this setting.
- §6 — Module boundary rules. `config/schema.py` must not import from `core/`
  or `io/`. These properties involve only config sub-objects, so no new imports
  are introduced.
- §8 — Vendor capability flags. Documents the intent of each capability flag and
  confirms that capabilities only affect publishing and output dispatch, not the
  solver model.

---

## Scope

**In scope:**
- `mimirheim/config/schema.py` — add `@property` methods to four config classes.
- `mimirheim/io/mqtt_publisher.py` — replace inline compound guards with
  property calls.
- `mimirheim/io/ha_discovery.py` — replace inline compound guards with property
  calls.
- `tests/unit/test_config_schema.py` — new tests for each property.

**Not in scope:**
- Any change to solver logic (`core/`, `devices/`).
- Any change to `control_arbitration.py`. Its capability reads are single-field
  lookups (`cfg.capabilities.zero_exchange`), not compound output guards, and
  don't benefit from this pattern.
- Any change to MQTT topic behaviour, payload format, or publish timing.
- Documentation updates (the properties encode no new design decision; they are
  a refactor of existing logic).

---

## Properties to add

### `BatteryConfig`

```python
@property
def has_exchange_mode_output(self) -> bool:
    """True when the zero-exchange capability is declared and an output topic is configured.

    Both conditions must hold for mimirheim to publish to the exchange_mode
    topic: the hardware must support the closed-loop mode (capability flag),
    and an MQTT topic must be configured to receive the assertion (output topic).
    """
    return self.capabilities.zero_exchange and self.outputs.exchange_mode is not None
```

### `EvConfig`

```python
@property
def has_exchange_mode_output(self) -> bool:
    """True when zero-exchange is declared and an exchange_mode topic is configured."""
    return self.capabilities.zero_exchange and self.outputs.exchange_mode is not None

@property
def has_loadbalance_output(self) -> bool:
    """True when loadbalance is declared and a loadbalance_cmd topic is configured."""
    return self.capabilities.loadbalance and self.outputs.loadbalance_cmd is not None
```

### `PvConfig`

```python
@property
def is_controllable(self) -> bool:
    """True when mimirheim actively dispatches this array.

    A PV array is controllable when it operates in staged, continuous
    power-limit, or on/off mode. A fixed array (no capabilities, no stages)
    is not controllable: mimirheim treats its output as a constant and publishes
    no control commands to it.
    """
    return (
        self.production_stages is not None
        or self.capabilities.power_limit
        or self.capabilities.on_off
    )

@property
def has_power_limit_output(self) -> bool:
    """True when a power-limit setpoint topic should be published each cycle."""
    return (
        (self.capabilities.power_limit or self.production_stages is not None)
        and self.outputs.power_limit_kw is not None
    )

@property
def has_zero_export_output(self) -> bool:
    """True when a zero-export mode boolean topic should be published each cycle."""
    return self.capabilities.zero_export and self.outputs.zero_export_mode is not None

@property
def has_on_off_output(self) -> bool:
    """True when an on/off mode boolean topic should be published each cycle."""
    return self.capabilities.on_off and self.outputs.on_off_mode is not None

@property
def has_is_curtailed_output(self) -> bool:
    """True when a mode-agnostic curtailment status topic should be published."""
    return self.is_controllable and self.outputs.is_curtailed is not None
```

### `HybridInverterConfig`

```python
@property
def has_exchange_mode_output(self) -> bool:
    """True when the zero-exchange capability is declared and an output topic is configured."""
    return self.capabilities.zero_exchange and self.outputs.exchange_mode is not None
```

---

## Callers to update

After the properties are added, replace the inline guards in both IO modules:

### `mimirheim/io/mqtt_publisher.py`

| Before | After |
|---|---|
| `(pv_cfg.capabilities.power_limit or pv_cfg.production_stages is not None) and pv_cfg.outputs.power_limit_kw is not None` | `pv_cfg.has_power_limit_output` |
| `pv_cfg.capabilities.zero_export and pv_cfg.outputs.zero_export_mode is not None` | `pv_cfg.has_zero_export_output` |
| `pv_cfg.capabilities.on_off and pv_cfg.outputs.on_off_mode is not None` | `pv_cfg.has_on_off_output` |
| `pv_cfg.outputs.is_curtailed is not None` (curtailed block guard) | `pv_cfg.outputs.is_curtailed is not None` — **leave unchanged** (publisher uses setpoint runtime value, not capability, as the primary guard; the `outputs.is_curtailed is not None` check is already sufficient here) |
| `ev_cfg.capabilities.zero_exchange and ev_cfg.outputs.exchange_mode is not None` | `ev_cfg.has_exchange_mode_output` |
| `ev_cfg.capabilities.loadbalance and ev_cfg.outputs.loadbalance_cmd is not None` | `ev_cfg.has_loadbalance_output` |
| `bat_cfg.capabilities.zero_exchange and bat_cfg.outputs.exchange_mode is not None` | `bat_cfg.has_exchange_mode_output` |
| `hi_cfg.capabilities.zero_exchange and hi_cfg.outputs.exchange_mode is not None` | `hi_cfg.has_exchange_mode_output` |

Note: each guard in `mqtt_publisher.py` also includes a runtime check on the
setpoint value (e.g. `and setpoint.zero_exchange_active is not None`). That
runtime check is not part of the property and must be preserved inline.

### `mimirheim/io/ha_discovery.py`

| Before | After |
|---|---|
| `(pv_cfg.capabilities.power_limit or pv_cfg.production_stages is not None) and pv_cfg.outputs.power_limit_kw is not None` | `pv_cfg.has_power_limit_output` |
| `pv_cfg.capabilities.zero_export and pv_cfg.outputs.zero_export_mode is not None` | `pv_cfg.has_zero_export_output` |
| `pv_cfg.capabilities.on_off and pv_cfg.outputs.on_off_mode is not None` | `pv_cfg.has_on_off_output` |
| `is_controllable = (production_stages is not None or capabilities.power_limit or capabilities.on_off)` local variable | replace with `pv_cfg.is_controllable` |
| `is_controllable and pv_cfg.outputs.is_curtailed is not None` | `pv_cfg.has_is_curtailed_output` |
| `ev_cfg.capabilities.zero_exchange and ev_cfg.outputs.exchange_mode is not None` | `ev_cfg.has_exchange_mode_output` |
| `ev_cfg.capabilities.loadbalance and ev_cfg.outputs.loadbalance_cmd is not None` | `ev_cfg.has_loadbalance_output` |
| `bat_cfg.capabilities.zero_exchange and bat_cfg.outputs.exchange_mode is not None` | `bat_cfg.has_exchange_mode_output` |

Note: the hybrid inverter block in `ha_discovery.py` was not yet present at the
time of writing (check before implementing). If it exists, apply
`hi_cfg.has_exchange_mode_output` there too.

---

## TDD workflow

### Step 1 — Confirm baseline

```bash
uv run pytest
```

All tests must pass before any changes are made. Report any pre-existing failures
to the user and stop.

### Step 2 — Write tests (red)

Add a new class `TestOutputCapabilityProperties` to
`tests/unit/test_config_schema.py`.

For each property, write three tests:
1. Returns `True` when capability is enabled and output topic is set.
2. Returns `False` when capability is disabled (output topic set but irrelevant).
3. Returns `False` when output topic is `None` (capability enabled but no topic).

Additional tests for `PvConfig.is_controllable`:
4. Returns `True` for `production_stages` (staged mode).
5. Returns `True` for `capabilities.power_limit`.
6. Returns `True` for `capabilities.on_off`.
7. Returns `False` for a fixed array (no capabilities, no stages).

Run `uv run pytest tests/unit/test_config_schema.py -k TestOutputCapabilityProperties`
and confirm all new tests fail (the properties do not yet exist).

### Step 3 — Implement properties (green)

Add the `@property` methods listed above to `mimirheim/config/schema.py`, inside
the respective config classes.

Run `uv run pytest tests/unit/test_config_schema.py -k TestOutputCapabilityProperties`
and confirm all new tests pass.

### Step 4 — Update callers

Update `mqtt_publisher.py` and `ha_discovery.py` per the substitution tables
above.

Run `uv run pytest` and confirm no regressions. The existing publisher and
discovery tests cover the call paths and will catch any mistakes.

### Step 5 — Final check

```bash
uv run pytest
```

All tests must pass. Move this file to `plans/done/`.

---

## Acceptance criteria

- `BatteryConfig.has_exchange_mode_output`, `EvConfig.has_exchange_mode_output`,
  `EvConfig.has_loadbalance_output`, `PvConfig.is_controllable`,
  `PvConfig.has_power_limit_output`, `PvConfig.has_zero_export_output`,
  `PvConfig.has_on_off_output`, `PvConfig.has_is_curtailed_output`, and
  `HybridInverterConfig.has_exchange_mode_output` all exist on their respective
  classes.
- Each property is covered by at least three tests (True, False-capability,
  False-topic). `is_controllable` has at least four tests.
- `mqtt_publisher.py` and `ha_discovery.py` contain no inline compound guards of
  the form `cfg.capabilities.X and cfg.outputs.Y is not None`.
- `uv run pytest` passes with no regressions.
