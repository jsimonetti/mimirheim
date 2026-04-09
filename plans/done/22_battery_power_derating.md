# Step 22 — Battery power derating near SOC extremes

## References

- IMPLEMENTATION_DETAILS §8, subsections "Split charge/discharge variables",
  "Piecewise efficiency (battery and EV)"
- `mimirheim/devices/battery.py` — `Battery.add_constraints`
- `mimirheim/config/schema.py` — `BatteryConfig`

---

## Files to modify

- `mimirheim/config/schema.py`
- `mimirheim/devices/battery.py`
- `tests/unit/test_battery_constraints.py`
- `tests/unit/test_config_schema.py`

---

## Background

Real battery inverters reduce the maximum charge power as the battery approaches full
capacity, and reduce the maximum discharge power as it approaches the minimum SOC.
This behaviour protects the cells by preventing high current rates at extreme states
of charge.

The reduction is approximately linear. Two physical regions matter:

**Charge derating near full:** When `soc > reduce_charge_above_soc_kwh`, maximum
charge power decreases linearly from `max_charge_kw` (at the threshold) to
`reduce_charge_min_kw` (at `capacity_kwh`). Below the threshold, full charge power
is available.

**Discharge derating near empty:** When `soc < reduce_discharge_below_soc_kwh`,
maximum discharge power decreases linearly from `max_discharge_kw` (at the threshold)
to `reduce_discharge_min_kw` (at `min_soc_kwh`). Above the threshold, full discharge
power is available.

Both derated regions are modelled with linear constraints. Crucially, the constraint
that limits charge power when SOC is high can be expressed entirely in terms of the
existing solver variables `soc[t]` and `charge_total[t]` — it is a single linear
inequality per time step:

```
charge_total[t] ≤ max_charge_kw + slope_c × (soc[t] − reduce_charge_above_soc_kwh)
```

where `slope_c = (reduce_charge_min_kw − max_charge_kw) / (capacity_kwh − reduce_charge_above_soc_kwh)` (always negative).

When `soc[t] < reduce_charge_above_soc_kwh`, the right-hand side is greater than
`max_charge_kw`, so the constraint is non-binding — the existing segment-based upper
bounds on `charge_total[t]` already enforce the maximum. The additional constraint
only tightens the bound when the SOC is above the threshold.

The same structure applies to discharge derating:

```
discharge_total[t] ≤ max_discharge_kw + slope_d × (reduce_discharge_below_soc_kwh − soc[t])
```

where `slope_d = (reduce_discharge_min_kw − max_discharge_kw) / (reduce_discharge_below_soc_kwh − min_soc_kwh)` (always negative, since `min_kw < max_kw` and the denominator is positive).

Both constraints are unconditionally added per time step. When the SOC is outside the
derated region, they are non-binding (the existing bounds take precedence).

All four fields are optional. When they are not set (the default), no derating
constraints are added and behaviour is unchanged.

---

## Tests first

Config validation tests go in `tests/unit/test_config_schema.py`. Solver behaviour
tests go in `tests/unit/test_battery_constraints.py`.

### Config tests (`test_config_schema.py`)

- `test_battery_derating_all_fields_none_by_default` — a `BatteryConfig` with no
  derating fields set validates without error; all four fields are `None`.
- `test_battery_charge_derating_requires_both_fields` — setting only
  `reduce_charge_above_soc_kwh` without `reduce_charge_min_kw` must raise a
  `ValidationError`. Also test the reverse (only `reduce_charge_min_kw` set).
- `test_battery_discharge_derating_requires_both_fields` — same for the discharge
  pair.
- `test_battery_reduce_charge_above_must_be_in_range` — `reduce_charge_above_soc_kwh`
  must be strictly between `min_soc_kwh` (exclusive) and `capacity_kwh` (exclusive).
  Test that the boundary violations raise.
- `test_battery_reduce_charge_min_must_be_positive_and_below_max` —
  `reduce_charge_min_kw` must be > 0 and < `max_charge_kw`. Test boundary violations.
- `test_battery_reduce_discharge_below_must_be_in_range` — symmetric to the charge
  version.

### Solver tests (`test_battery_constraints.py`)

- `test_charge_derating_no_effect_below_threshold` — configure
  `reduce_charge_above_soc_kwh = 8.0 kWh` with `capacity_kwh = 10.0` and
  `reduce_charge_min_kw = 1.0`. Start SOC at 3.0 kWh (well below the threshold).
  Assert that the solver can charge at the full `max_charge_kw` at step 0.
- `test_charge_derating_limits_power_near_full` — same config, start SOC at 9.5 kWh
  (above threshold). Assert that the charge power at step 0 is below `max_charge_kw`
  and consistent with the linear derating formula.
- `test_discharge_derating_no_effect_above_threshold` — configure
  `reduce_discharge_below_soc_kwh = 2.0 kWh` with `min_soc_kwh = 0.5` and
  `reduce_discharge_min_kw = 0.5`. Start SOC at 8.0 kWh (above the threshold).
  Assert full discharge power is available at step 0.
- `test_discharge_derating_limits_power_near_empty` — same config, start SOC at
  0.8 kWh (below threshold). Assert discharge power is below `max_discharge_kw` and
  consistent with the linear derating formula.
- `test_derating_no_extra_constraints_when_not_configured` — with the default config
  (all four fields None), the constraint count must equal the pre-existing baseline.

Run `uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py -k "derat"` — all tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — `BatteryConfig`

Add four fields after the `soc_low_penalty_eur_per_kwh_h` field added in plan 21
(or after `wear_cost_eur_per_kwh` if plan 21 has not been merged yet):

```python
reduce_charge_above_soc_kwh: float | None = Field(
    default=None,
    description=(
        "SOC level in kWh above which max charge power begins to decrease "
        "linearly. Must be strictly between min_soc_kwh and capacity_kwh. "
        "Must be set together with reduce_charge_min_kw."
    ),
)
reduce_charge_min_kw: float | None = Field(
    default=None,
    ge=0.0,
    description=(
        "Max charge power in kW at full capacity (capacity_kwh). The charge "
        "power limit decreases linearly from max_charge_kw at "
        "reduce_charge_above_soc_kwh to this value at capacity_kwh. Must be "
        "strictly less than the sum of charge segment power_max_kw values. "
        "Must be set together with reduce_charge_above_soc_kwh."
    ),
)
reduce_discharge_below_soc_kwh: float | None = Field(
    default=None,
    description=(
        "SOC level in kWh below which max discharge power begins to decrease "
        "linearly. Must be strictly between min_soc_kwh and capacity_kwh. "
        "Must be set together with reduce_discharge_min_kw."
    ),
)
reduce_discharge_min_kw: float | None = Field(
    default=None,
    ge=0.0,
    description=(
        "Max discharge power in kW at minimum SOC (min_soc_kwh). The discharge "
        "power limit decreases linearly from max_discharge_kw at "
        "reduce_discharge_below_soc_kwh to this value at min_soc_kwh. Must be "
        "strictly less than the sum of discharge segment power_max_kw values. "
        "Must be set together with reduce_discharge_below_soc_kwh."
    ),
)
```

Add a Pydantic model validator that enforces the "both or neither" rule and range
checks. Compute `max_charge_kw` and `max_discharge_kw` inline from the segment lists
for the validator.

### `mimirheim/devices/battery.py` — `Battery.add_constraints`

After the existing SOC dynamics and Big-M charge/discharge guard constraints, add:

```python
max_charge_kw = sum(s.power_max_kw for s in config.charge_segments)
max_discharge_kw = sum(s.power_max_kw for s in config.discharge_segments)

# Charge derating near full SOC.
# When soc[t] > reduce_charge_above_soc_kwh, the maximum charge power
# decreases linearly to reduce_charge_min_kw at capacity_kwh.
# When soc[t] <= reduce_charge_above_soc_kwh, the right-hand side of this
# constraint exceeds max_charge_kw, so the constraint is non-binding.
if config.reduce_charge_above_soc_kwh is not None:
    slope_c = (
        (config.reduce_charge_min_kw - max_charge_kw)
        / (config.capacity_kwh - config.reduce_charge_above_soc_kwh)
    )
    rhs_constant_c = max_charge_kw - slope_c * config.reduce_charge_above_soc_kwh
    for t in ctx.T:
        charge_total = sum(self._charge_seg[t, i] for i in range(len(config.charge_segments)))
        ctx.solver.add_constraint(
            charge_total - slope_c * self._soc[t] <= rhs_constant_c
        )

# Discharge derating near minimum SOC.
# When soc[t] < reduce_discharge_below_soc_kwh, the maximum discharge power
# decreases linearly to reduce_discharge_min_kw at min_soc_kwh.
if config.reduce_discharge_below_soc_kwh is not None:
    slope_d = (
        (config.reduce_discharge_min_kw - max_discharge_kw)
        / (config.reduce_discharge_below_soc_kwh - config.min_soc_kwh)
    )
    rhs_constant_d = max_discharge_kw + slope_d * config.reduce_discharge_below_soc_kwh
    for t in ctx.T:
        discharge_total = sum(self._discharge_seg[t, i] for i in range(len(config.discharge_segments)))
        ctx.solver.add_constraint(
            discharge_total + slope_d * self._soc[t] <= rhs_constant_d
        )
```

Comment every variable in the `slope` and RHS calculations: explain the two-point
linear function and why the constraint is safe to add unconditionally.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py
```

All tests green.

```bash
uv run pytest tests/scenarios/
```

No golden file changes expected — existing scenarios use default config with no
derating fields set.

---

## Done

```bash
mv plans/22_battery_power_derating.md plans/done/
```
