# Step 30 — Staged power output for PV inverters

## References

- IMPLEMENTATION_DETAILS §6 — Pydantic config models
- IMPLEMENTATION_DETAILS §8 — PV device, `add_constraints`, `net_power`
- `mimirheim/config/schema.py` — `PvConfig`, `PvCapabilitiesConfig`
- `mimirheim/devices/pv.py` — `PvDevice`, `PvInputs`
- `mimirheim/core/model_builder.py` — PV setpoint extraction
- `tests/unit/test_pv_constraints.py`

---

## Background

Some inverters do not accept a continuous power setpoint. Instead, they expose a small set of
discrete operating levels — for example 0 kW, 1.5 kW, 3.0 kW, and 4.5 kW. Writing any other
value to the hardware register has no effect or is rejected.

Today `capabilities.power_limit` models continuous curtailment and `capabilities.on_off`
models the binary special case (0 or full). Neither covers a multi-level discrete inverter.

This plan adds `production_stages: list[float]` to `PvConfig`. When provided, the solver
selects exactly one stage per time step using binary variables. The effective output at step
`t` is `min(stage_kw, forecast[t])`: the inverter produces up to its setpoint, but not more
than the available solar.

---

## What this plan does NOT do

- It does not model inverter DC–AC efficiency differences between stages. mimirheim works from AC
  output forecasts; the efficiency between stages is already embedded in the forecast.
- It does not add support for staged power to batteries or EVs (those already use segment
  variables, which is the correct model for storage).
- It does not change the `power_limit` or `on_off` paths. Those remain unchanged.

---

## Config design

### New field: `production_stages`

Add `production_stages: list[float] | None = None` to `PvConfig`.

```yaml
pv_arrays:
  pv_roof:
    max_power_kw: 4.5
    topic_forecast: "mimir/input/pv_forecast"

    production_stages:   # discrete power levels the inverter accepts, in kW
      - 0.0              # must include 0.0 (off stage)
      - 1.5
      - 3.0
      - 4.5

    capabilities:
      power_limit: false  # must be false when production_stages is provided
      zero_export_mode: false
      on_off: false       # must be false when production_stages is provided

    outputs:
      power_limit_kw: "mimir/output/pv_roof/power_limit_kw"  # receives the chosen stage value
```

### Validation rules (model_validator on `PvConfig`)

1. All stage values must be `≥ 0.0`.
2. Stages must be strictly increasing.
3. The first stage must be `0.0` (the off state). Without it the solver cannot turn the array
   off, which would force production even when export is unprofitable.
4. If `production_stages` is provided, `capabilities.power_limit` must be `False`. Continuous
   curtailment and staged curtailment are mutually exclusive — the hardware cannot do both.
5. If `production_stages` is provided, `capabilities.on_off` must be `False`. On/off is a
   two-stage special case; a user who wants on/off should use `production_stages: [0.0, X]`
   instead of enabling both.
6. `max_power_kw` must be `≥` the last (largest) stage value. A stage larger than
   `max_power_kw` could never be the active stage in any realistic forecast scenario, which
   indicates a config error.

### Published setpoint

`power_limit_kw` in the schedule setpoint already carries the production limit value. When
staged mode is active, mimirheim publishes the *stage's kW value* (not the effective clipped
output). The receiving automation programs exactly that value into the inverter register.

The `power_limit_kw` output topic is therefore shared between continuous and staged modes. No
new output topic is needed.

---

## Solver formulation

### Variables

For each time step `t` and each stage index `s`:

```
stage_active[t, s] ∈ {0, 1}
```

This is a binary variable: 1 if stage `s` is selected at step `t`, 0 otherwise. Total binary
variables added: `H × len(production_stages)`.

### Exactly-one constraint (selection)

At each step exactly one stage must be active:

```
Σ_s  stage_active[t, s]  =  1      ∀ t
```

This replaces the need for an SOS1 set (an explicit constraint is simpler and equally fast for
small stage counts).

### Effective output and power balance

The effective AC output at step `t` when stage `s` is active is:

```
effective_output[t, s]  =  min(forecast[t], stage_kw[s])
```

Both values are constants known before the solve, so this precomputation happens in Python
before any variable is created. It is not a nonlinear construct.

The net power expression inserted into the power balance is:

```
pv_kw[t]  =  Σ_s  effective_output[t, s] × stage_active[t, s]
```

This is a linear combination of binary variables with scalar coefficients — a standard
integer linear form.

### Consequence for `net_power`

`net_power(t)` returns the linear expression `Σ_s effective_output[t, s] × stage_active[t, s]`.
The model builder evaluates this via `_eval_net_power(ctx, pv.net_power(t))` after solving,
yielding the scalar effective output for the schedule.

### Setting `power_limit_kw` in the setpoint

After the solve, the model builder must extract the *chosen stage kW* (not the effective
output) to populate `DeviceSetpoint.power_limit_kw`. This is what gets sent to the hardware.
The active stage index is found by inspecting which `stage_active[t, s]` rounded to 1.

In `model_builder.py`, extend the PV setpoint extraction block:

```python
if pv.config.production_stages is not None:
    # Find the active stage: the one whose binary rounded to 1.
    chosen_stage_kw = 0.0
    for s, stage_kw in enumerate(pv.config.production_stages):
        if round(ctx.solver.val(pv._stage_active[t, s])) == 1:
            chosen_stage_kw = stage_kw
            break
    power_limit_kw = chosen_stage_kw if caps.power_limit else None
    # Note: caps.power_limit is False when production_stages is set (enforced by
    # config validation). We still want to publish the stage value, so override:
    power_limit_kw = chosen_stage_kw  # always publish when staged
```

Actually, to clean this up: add a helper property `pv.chosen_stage_kw(t)` that returns the
selected stage value after the solve. The model builder calls it:

```python
power_limit_kw = pv.chosen_stage_kw(t) if pv.config.production_stages is not None else (
    pv_kw if caps.power_limit else None
)
```

---

## Interaction with `on_off` and `power_limit`

| Configuration | Solver behaviour |
|---|---|
| No capabilities, no stages | Fixed forecast (no variables) |
| `power_limit: true` | Continuous variable `pv_kw[t]` ∈ [0, forecast] |
| `on_off: true` | Binary `pv_on[t]`; output = forecast × on |
| Both `power_limit` and `on_off` | Continuous + binary; Big-M coupling |
| `production_stages: [...]` | Binary per stage; exactly-one constraint |
| `production_stages` + `power_limit` | **Forbidden by validation** |
| `production_stages` + `on_off` | **Forbidden by validation** |

---

## Files to modify

| File | Change |
|---|---|
| `mimirheim/config/schema.py` | Add `production_stages` to `PvConfig`; add model_validator |
| `mimirheim/devices/pv.py` | Add staged branch in `add_constraints`; add `chosen_stage_kw(t)` |
| `mimirheim/core/model_builder.py` | Use `chosen_stage_kw` for PV `power_limit_kw` setpoint |
| `mimirheim/config/example.yaml` | Add commented `production_stages` example |
| `tests/unit/test_pv_constraints.py` | New staged-mode tests (see below) |
| `tests/unit/test_config_schema.py` | New validation tests |

## Files to create

None.

---

## Tests first

### `tests/unit/test_pv_constraints.py` additions

- `test_staged_pv_selects_one_stage_per_step` — configure stages `[0.0, 1.5, 3.0, 4.5]`,
  forecast `[3.8, 3.8]`, no export penalty. Assert that exactly one of
  `stage_active[t, s]` rounds to 1 at each step, and the others are 0.

- `test_staged_pv_effective_output_capped_by_forecast` — forecast `[1.0, 1.0]`, stages
  `[0.0, 1.5, 3.0]`. The solver has no incentive to curtail (positive export price). Assert
  that the effective output is `1.0` (forecast caps the stage), not `1.5` (the selected stage).

- `test_staged_pv_curtails_at_negative_export_price` — two steps: step 0 export price
  `−0.05` EUR/kWh, step 1 export price `+0.10` EUR/kWh. Forecast `[3.0, 3.0]`, stages
  `[0.0, 1.5, 3.0]`. Base load zero. Assert step 0 selects stage `0.0` (off) and step 1
  selects stage `3.0` (full production).

- `test_staged_pv_chooses_highest_available_stage` — forecast `[2.2, 2.2]`, stages
  `[0.0, 1.5, 3.0, 4.5]`, positive export price, base load zero. The solver should select
  stage `1.5` (highest stage ≤ forecast that is actually achievable — stage `3.0` has
  `effective_output = min(2.2, 3.0) = 2.2`, same as stage `4.5`; solver picks the lowest
  that gives the full forecast, which is implicitly `3.0` since `effective_output[3.0] = 2.2`
  equals `effective_output[4.5] = 2.2`). Actually: the solver should pick stage `3.0` or
  `4.5` (both produce `2.2`). Assert `effective output == 2.2`, and that the chosen
  `power_limit_kw` is one of the stages ≥ `2.2`.

- `test_staged_pv_power_limit_kw_is_stage_not_effective_output` — after solve with forecast
  `[2.2]` and stages `[0.0, 1.5, 3.0]`, assert `setpoint.power_limit_kw` is `3.0` (the stage
  kW) not `2.2` (the effective output). This confirms the hardware receives the stage register
  value, not the clipped forecast.

- `test_staged_and_power_limit_raises` — `PvConfig` with both `production_stages` and
  `capabilities.power_limit: true` must raise `ValidationError`.

- `test_staged_and_on_off_raises` — `PvConfig` with both `production_stages` and
  `capabilities.on_off: true` must raise `ValidationError`.

- `test_staged_missing_zero_stage_raises` — stages `[1.5, 3.0]` (no `0.0`) must raise
  `ValidationError`.

- `test_staged_not_strictly_increasing_raises` — stages `[0.0, 3.0, 1.5]` must raise
  `ValidationError`.

### `tests/unit/test_config_schema.py` additions

- `test_pv_production_stages_valid` — stages `[0.0, 1.5, 3.0, 4.5]`, no capabilities. Passes
  validation.
- `test_pv_production_stages_max_power_below_last_stage_raises` — `max_power_kw: 3.0`,
  stages `[0.0, 1.5, 3.0, 4.5]` (last stage `4.5 > max_power_kw`). Must raise
  `ValidationError`.

---

## Acceptance criteria

All new tests pass.

Existing PV tests (`fixed`, `power_limit`, `on_off`, `both`) pass unchanged.

`test_pv_staged_power_limit_kw_is_stage_not_effective_output` confirms that the published
setpoint is the hardware register value, not the effective clipped output.

`uv run pytest` produces no regressions.

---

## Move to done

```bash
mv plans/30_pv_staged_power.md plans/done/
```
