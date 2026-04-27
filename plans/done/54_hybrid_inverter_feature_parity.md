# Plan 54 — Hybrid inverter feature parity with Battery and PV devices

## Motivation

`HybridInverterDevice` was written as a correct but minimal first-pass
implementation. Compared to the dedicated `Battery` and `PvDevice` it is
missing several features that affect both optimisation quality and hardware
correctness:

| Gap | Impact |
|---|---|
| No `terminal_soc_var()` | Solver treats end-of-horizon stored energy as worthless → drains battery every cycle |
| No `optimal_lower_soc_kwh` / penalty | No soft lower SOC preference → solver ignores user-defined reserve |
| No SOC derating constraints | Solver may charge/discharge at full power near SOC extremes → hardware cut-out |
| No `min_charge_kw` / `min_discharge_kw` | Solver can command below hardware minimum operating power → inverter ignores setpoints |
| No `capabilities` / `outputs` config | No zero-exchange closed-loop mode → advanced firmware features unused |
| `objective_terms()` uses DC power for wear cost | Should use the same AC net power convention the Battery uses, for consistent EUR accounting |

**PV capabilities** (`power_limit`, `on_off`, `zero_export`, `production_stages`)
are explicitly out of scope for this plan. Most hybrid inverters do not accept
external MPPT setpoints, and the solver already curtails PV internally via the
DC bus balance. Publishing a power limit to an MPPT input would require
confirmed hardware support. Defer to a separate plan when a concrete device
type requires it.

---

## Relevant source locations

```
mimirheim/config/schema.py                             — HybridInverterConfig (line ~1573)
mimirheim/devices/hybrid_inverter.py
mimirheim/core/bundle.py                               — HybridInverterInputs
mimirheim/core/model_builder.py                        — hybrid inverter schedule extraction
mimirheim/io/mqtt_publisher.py                         — output topic publishing
tests/unit/test_hybrid_inverter_constraints.py
tests/unit/test_config_schema.py
tests/unit/test_mqtt_publisher.py
```

## IMPLEMENTATION_DETAILS sections

§1 (Pydantic models — extra="forbid"), §6 (boundary rules).

---

## Design decisions

### 1. `terminal_soc_var()` is a one-liner

`HybridInverterDevice` already maintains `self.soc: dict[int, Any]`. The method
returns `self.soc.get(ctx.T[-1])`. The `ObjectiveBuilder._terminal_soc_terms`
method already handles any device that exposes this method via `getattr`.

### 2. `optimal_lower_soc_kwh` and `_soc_low` variables follow Battery exactly

The `_soc_low[t]` auxiliary variable (slack for the soft lower bound), the
constraint `soc_low[t] >= optimal_lower_soc_kwh - soc[t]`, and the penalty
objective term are identical to Battery. The Battery implementation can be
read verbatim; this plan is explicit about which code to port.

### 3. SOC derating constraints are DC-bus side, not AC side

In `Battery.add_constraints`, the derating bound is applied to
`charge_ac_kw(t)` (AC power). In `HybridInverterDevice`, the controllable
charge power is `bat_charge_dc[t]` (DC bus side), not an AC-side variable.
The derating constraints are applied to `bat_charge_dc[t]` and
`bat_discharge_dc[t]` directly. The linear slope calculation is identical
(same two-point formula).

The `max_charge_kw` and `max_discharge_kw` config fields play the same role as
the sum of segment power_max_kw values in Battery — they are the Big-M
coefficients and the reference power level for the derating function.

### 4. Min power floors follow Battery exactly

```python
# charge floor: only active when mode[t]=1 (charging)
if self.config.min_charge_kw is not None:
    ctx.solver.add_constraint(
        self.bat_charge_dc[t] >= self.config.min_charge_kw * self.mode[t]
    )
# discharge floor: only active when mode[t]=0 (discharging)
if self.config.min_discharge_kw is not None:
    ctx.solver.add_constraint(
        self.bat_discharge_dc[t] >= self.config.min_discharge_kw * (1 - self.mode[t])
    )
```

### 5. `capabilities` and `outputs` config classes are new, minimal

New classes `HybridInverterCapabilitiesConfig` and `HybridInverterOutputsConfig`
are added. Both mirror the Battery equivalents exactly:

```python
class HybridInverterCapabilitiesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    zero_exchange: bool = Field(default=False, ...)

class HybridInverterOutputsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    exchange_mode: str | None = Field(default=None, ...)
```

Two fields are added to `HybridInverterConfig`:

```python
capabilities: HybridInverterCapabilitiesConfig = Field(
    default_factory=HybridInverterCapabilitiesConfig, ...)
outputs: HybridInverterOutputsConfig = Field(
    default_factory=HybridInverterOutputsConfig, ...)
```

### 6. `model_builder` adds `zero_exchange_active` to hybrid setpoints

The hybrid inverter schedule extraction block currently produces:

```python
device_setpoints[hi.name] = DeviceSetpoint(
    kw=_eval_net_power(ctx, hi.net_power(t)),
    type="hybrid_inverter",
)
```

It must be updated to:

```python
hi_cfg = config.hybrid_inverters[hi.name]
hi_zea = False if hi_cfg.capabilities.zero_exchange else None
device_setpoints[hi.name] = DeviceSetpoint(
    kw=_eval_net_power(ctx, hi.net_power(t)),
    type="hybrid_inverter",
    zero_exchange_active=hi_zea,
)
```

### 7. Publisher adds a hybrid inverter `exchange_mode` publishing block

Identical structure to the existing battery `exchange_mode` block (publisher
step 6), but keyed on `setpoint.type == "hybrid_inverter"` and looking up
`config.hybrid_inverters.get(device_name)`.

### 8. `objective_terms()` wear cost uses AC net power

The current implementation calculates wear cost on DC power:

```python
self.config.wear_cost_eur_per_kwh
* (self.bat_charge_dc[t] + self.bat_discharge_dc[t])
* self._dt
```

This is dimensional correct (EUR/kWh × kW × h) but uses DC bus power, which
overstates wear relative to Battery (which uses AC power). For consistency and
to allow fair comparison of wear cost coefficients between Battery and
HybridInverterDevice, use the AC equivalent:

```python
# AC import power = ac_to_dc[t] (already in kW at AC bus side).
# AC export power = dc_to_ac[t] (already in kW at AC bus side).
self.config.wear_cost_eur_per_kwh
* (self.ac_to_dc[t] + self.dc_to_ac[t])
* self._dt
```

This is a minor numeric change; existing tests of wear cost will remain valid
(they use η=1 throughout, so DC and AC quantities are equal).

### 9. Config validators follow Battery's `_validate_derating`

`HybridInverterConfig` gets two new validators:

- `_validate_soc_levels`: identical to Battery's, checks
  `min_soc_kwh <= optimal_lower_soc_kwh <= capacity_kwh`.

- `_validate_derating`: checks paired fields and range constraints.
  `max_charge_kw` / `max_discharge_kw` config fields replace the
  segment-derived max in Battery's validator.

---

## TDD workflow

### Step 1 — write failing tests

#### `tests/unit/test_config_schema.py` — new class `TestHybridInverterConfig`

The file already has tests for other config models. Add a new class:

```python
class TestHybridInverterConfig:
    def test_minimal_config_accepted(self) -> None:
        """Existing minimal config with no new fields parses correctly."""

    def test_optimal_lower_soc_kwh_accepted(self) -> None:
        """optimal_lower_soc_kwh between min_soc_kwh and capacity_kwh is accepted."""

    def test_optimal_lower_soc_kwh_below_min_rejected(self) -> None:
        """optimal_lower_soc_kwh < min_soc_kwh raises ValidationError."""

    def test_optimal_lower_soc_kwh_above_capacity_rejected(self) -> None:
        """optimal_lower_soc_kwh > capacity_kwh raises ValidationError."""

    def test_derating_fields_accepted_when_paired(self) -> None:
        """reduce_charge_above_soc_kwh + reduce_charge_min_kw accepted together."""

    def test_derating_only_one_charge_field_rejected(self) -> None:
        """reduce_charge_above_soc_kwh without reduce_charge_min_kw raises."""

    def test_derating_charge_soc_out_of_range_rejected(self) -> None:
        """reduce_charge_above_soc_kwh outside (min_soc, capacity) raises."""

    def test_min_charge_kw_accepted(self) -> None:
        """min_charge_kw >= 0 is accepted."""

    def test_capabilities_defaults(self) -> None:
        """HybridInverterConfig.capabilities is HybridInverterCapabilitiesConfig with zero_exchange=False."""

    def test_outputs_defaults(self) -> None:
        """HybridInverterConfig.outputs has exchange_mode=None by default."""
```

#### `tests/unit/test_hybrid_inverter_constraints.py` — add new test functions

```python
def test_terminal_soc_var_returns_last_step_variable() -> None:
    """terminal_soc_var(ctx) returns the soc variable at the last step.

    Used by ObjectiveBuilder._terminal_soc_terms to attach a terminal value
    to stored energy at the end of the horizon.
    """

def test_soc_low_variable_absent_when_optimal_lower_soc_zero() -> None:
    """When optimal_lower_soc_kwh == 0.0, no soc_low variables are created.

    The default config has optimal_lower_soc_kwh=0. Confirm _soc_low is empty
    after add_variables().
    """

def test_soc_low_variable_present_when_optimal_lower_soc_configured() -> None:
    """When optimal_lower_soc_kwh > min_soc_kwh, soc_low[t] variables are created."""

def test_soc_low_constraint_is_active_when_soc_below_optimal() -> None:
    """soc_low[t] equals the SOC deficit when the battery is dispatched below
    optimal_lower_soc_kwh.

    Setup:
    - optimal_lower_soc_kwh = 5.0, min_soc_kwh = 0.0.
    - Force SOC to 3.0 kWh (2 kWh below optimal).
    - Verify soc_low[0] ≈ 2.0 kWh.
    """

def test_soc_low_zero_when_soc_above_optimal() -> None:
    """soc_low[t] is zero when SOC >= optimal_lower_soc_kwh."""

def test_min_charge_floor_is_enforced() -> None:
    """bat_charge_dc[t] >= min_charge_kw when mode=1.

    Setup:
    - min_charge_kw = 2.0; force mode[0] = 1 (charging).
    - Minimise bat_charge_dc → solver should be constrained at 2.0.
    """

def test_min_discharge_floor_is_enforced() -> None:
    """bat_discharge_dc[t] >= min_discharge_kw when mode=0 (discharging).

    Setup:
    - min_discharge_kw = 1.5; force mode[0] = 0 (discharging).
    - Minimise bat_discharge_dc → constrained at 1.5 kW.
    """

def test_charge_derating_reduces_max_charge_near_capacity() -> None:
    """bat_charge_dc[t] is limited below max_charge_kw when SOC is near capacity.

    Setup:
    - reduce_charge_above_soc_kwh = 8.0, reduce_charge_min_kw = 1.0,
      capacity_kwh = 10.0, max_charge_kw = 6.0.
    - Pin SOC at the start of step 0 to 9.0 kWh (above threshold).
    - Minimise SOC (maximise charge attempt) → bat_charge_dc[0] must be
      below 6.0 (the derating function limits it).
    """

def test_discharge_derating_reduces_max_discharge_near_min_soc() -> None:
    """bat_discharge_dc[t] is limited when SOC is near min_soc_kwh.

    Setup:
    - reduce_discharge_below_soc_kwh = 3.0, reduce_discharge_min_kw = 1.0,
      min_soc_kwh = 0.0, max_discharge_kw = 6.0.
    - Pin SOC to 1.0 kWh at start (below threshold).
    - Minimise SOC (maximise discharge) → bat_discharge_dc[0] < 6.0.
    """

def test_objective_terms_includes_soc_low_penalty() -> None:
    """When soc_low_penalty_eur_per_kwh_h > 0, objective_terms returns a penalty
    expression for steps where soc_low[t] is in scope.
    """
```

#### `tests/unit/test_mqtt_publisher.py` — add hybrid inverter zero-exchange test

```python
def test_hybrid_inverter_exchange_mode_published_when_zero_exchange_true() -> None:
    """When capabilities.zero_exchange is True and zero_exchange_active is True,
    the exchange_mode topic is published with payload 'true'.
    """

def test_hybrid_inverter_exchange_mode_not_published_when_capability_false() -> None:
    """When capabilities.zero_exchange is False, no exchange_mode publish occurs."""
```

Confirm all new tests **fail** before any implementation.

### Step 2 — implement

Implement in this order. Run `uv run pytest` after each sub-step.

#### 2a. `mimirheim/config/schema.py`

1. Add `HybridInverterCapabilitiesConfig` and `HybridInverterOutputsConfig`
   classes (insert immediately before `HybridInverterConfig`).
2. Add new optional fields to `HybridInverterConfig`:
   - `optimal_lower_soc_kwh: float = 0.0`
   - `soc_low_penalty_eur_per_kwh_h: float = 0.0`
   - `reduce_charge_above_soc_kwh: float | None = None`
   - `reduce_charge_min_kw: float | None = None`
   - `reduce_discharge_below_soc_kwh: float | None = None`
   - `reduce_discharge_min_kw: float | None = None`
   - `min_charge_kw: float | None = None`
   - `min_discharge_kw: float | None = None`
   - `capabilities: HybridInverterCapabilitiesConfig`
   - `outputs: HybridInverterOutputsConfig`
3. Add `_validate_soc_levels` model validator.
4. Add `_validate_derating` model validator (uses `self.max_charge_kw` and
   `self.max_discharge_kw` directly instead of segment sums).

#### 2b. `mimirheim/devices/hybrid_inverter.py`

1. Add `self._soc_low: dict[int, Any] = {}` to `__init__`.
2. In `add_variables`, after the per-step SOC variable: create `_soc_low[t]`
   variables when `config.optimal_lower_soc_kwh > config.min_soc_kwh`.
   Bounds: `lb=0.0, ub=(optimal_lower_soc_kwh - min_soc_kwh)`.
3. Add `terminal_soc_var(ctx: ModelContext) -> Any | None` method.
4. In `add_constraints`, add per-step constraints:
   - Soft SOC constraint (only when `_soc_low` is populated).
   - Min charge floor (only when `config.min_charge_kw is not None`).
   - Min discharge floor (only when `config.min_discharge_kw is not None`).
5. After the per-step loop, add:
   - Charge derating constraints (only when `reduce_charge_above_soc_kwh is not None`).
   - Discharge derating constraints (only when `reduce_discharge_below_soc_kwh is not None`).
6. Update `objective_terms(t)`:
   - Change wear cost from DC to AC power convention.
   - Add `soc_low_penalty_eur_per_kwh_h * _soc_low[t] * _dt` when `t in self._soc_low`.

#### 2c. `mimirheim/core/model_builder.py`

In the hybrid inverter schedule-extraction block (~line 463), add
`zero_exchange_active` to the `DeviceSetpoint`:

```python
hi_cfg = config.hybrid_inverters[hi.name]
hi_zea = False if hi_cfg.capabilities.zero_exchange else None
device_setpoints[hi.name] = DeviceSetpoint(
    kw=_eval_net_power(ctx, hi.net_power(t)),
    type="hybrid_inverter",
    zero_exchange_active=hi_zea,
)
```

#### 2d. `mimirheim/io/mqtt_publisher.py`

After the existing battery `exchange_mode` block (step 6), add a new block for
hybrid inverters. Follow the same pattern:

```python
# 6b. Hybrid inverter exchange_mode output topic.
for device_name, setpoint in current.devices.items():
    if setpoint.type != "hybrid_inverter":
        continue
    hi_cfg = self._config.hybrid_inverters.get(device_name)
    if hi_cfg is None:
        continue
    if (
        hi_cfg.capabilities.zero_exchange
        and hi_cfg.outputs.exchange_mode is not None
        and setpoint.zero_exchange_active is not None
    ):
        self._client.publish(
            hi_cfg.outputs.exchange_mode,
            "true" if setpoint.zero_exchange_active else "false",
            qos=1,
            retain=True,
        )
```

### Step 3 — verify

```bash
uv run pytest tests/unit/test_hybrid_inverter_constraints.py -q --tb=short
uv run pytest tests/unit/test_config_schema.py -q --tb=short
uv run pytest tests/unit/test_mqtt_publisher.py -q --tb=short
uv run pytest -q --tb=short   # full suite — no regressions
```

---

## Acceptance criteria

- [ ] `HybridInverterConfig` accepts `optimal_lower_soc_kwh`, `soc_low_penalty_eur_per_kwh_h`, all derating fields, all floor fields, `capabilities`, and `outputs`.
- [ ] `HybridInverterConfig` validators reject: `optimal_lower_soc_kwh` outside `[min_soc_kwh, capacity_kwh]`; unpaired derating fields; derating fields with SOC out of `(min_soc, capacity)` range.
- [ ] `HybridInverterDevice.terminal_soc_var(ctx)` returns `soc[ctx.T[-1]]`.
- [ ] `_soc_low[t]` variables are created only when `optimal_lower_soc_kwh > min_soc_kwh`.
- [ ] `soc_low[t] >= optimal_lower_soc_kwh - soc[t]` is enforced by a solver constraint.
- [ ] `objective_terms(t)` includes `soc_low_penalty_eur_per_kwh_h * _soc_low[t] * dt` when configured.
- [ ] `objective_terms(t)` wear cost uses AC power (`ac_to_dc[t] + dc_to_ac[t]`).
- [ ] Charge derating: `bat_charge_dc[t]` is limited below `max_charge_kw` when SOC exceeds `reduce_charge_above_soc_kwh`.
- [ ] Discharge derating: `bat_discharge_dc[t]` is limited below `max_discharge_kw` when SOC is below `reduce_discharge_below_soc_kwh`.
- [ ] Min charge floor constraint prevents `bat_charge_dc[t]` < `min_charge_kw` during charging.
- [ ] Min discharge floor constraint prevents `bat_discharge_dc[t]` < `min_discharge_kw` during discharging.
- [ ] `model_builder` passes `zero_exchange_active=False` (or `None`) in hybrid inverter `DeviceSetpoint`.
- [ ] Publisher emits `exchange_mode` topic for hybrid inverters when `capabilities.zero_exchange=True` and topic is configured.
- [ ] All new unit tests pass.
- [ ] Full `uv run pytest` green with no regressions.
- [ ] Existing hybrid inverter tests (`test_hybrid_inverter_constraints.py`) remain green throughout — no pre-existing test is modified.

---

## Out of scope

- Hybrid inverter PV output capabilities (`power_limit`, `on_off`, `zero_export`,
  `production_stages`). The DC bus balance already allows the solver to curtail
  PV internally. External MPPT setpoint control requires confirmed hardware
  support and should be a dedicated plan.
- Efficiency curve model for the battery portion (SOS2 or stacked segments).
  The flat scalar model is physically correct and sufficient for most hardware.
  Curve-based modelling requires changing the DC bus balance variables and is
  a larger standalone change.
