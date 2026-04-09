# Step 21 — Battery optimal SOC penalty

## References

- IMPLEMENTATION_DETAILS §8, subsection "Wear cost in objective terms"
- IMPLEMENTATION_DETAILS §8, subsection "Split charge/discharge variables"
- `mimirheim/devices/battery.py` — `Battery`
- `mimirheim/config/schema.py` — `BatteryConfig`

---

## Files to modify

- `mimirheim/config/schema.py`
- `mimirheim/devices/battery.py`
- `tests/unit/test_battery_constraints.py`
- `tests/unit/test_config_schema.py`

---

## Background

The battery dispatch model minimises cost. The solver will freely drain the battery to
0 (or to `min_soc_kwh`) if prices make it profitable, regardless of how low the SOC
drops. In practice, operators want to maintain a buffer above the hard minimum — a
level at which the battery is available to cover unexpected demand spikes or grid
outages without dropping below the absolute minimum.

This plan introduces a configurable *optimal lower SOC* level. When the battery SOC
is below this level, the objective accrues a penalty proportional to the deficit. This
penalty acts as a soft constraint: the solver can still dispatch below the optimal
level, but only when the price spread makes it sufficiently profitable.

**Model:**

For each time step `t`, introduce auxiliary variable `soc_low[t]` representing the
SOC deficit below the optimal lower level:

```
soc_low[t] = max(0, optimal_lower_soc_kwh − soc[t])
```

Modelled as a linear constraint with a lower bound on the variable:

```
soc_low[t] >= 0                                (lower bound, set as variable bound)
soc_low[t] >= optimal_lower_soc_kwh − soc[t]  (deficit constraint)
```

The objective penalises the deficit:

```
penalty += soc_low_penalty_eur_per_kwh_h × soc_low[t] × dt
```

With a positive coefficient in the minimisation objective, the solver is incentivised
to drive `soc_low[t]` to its lower bound (zero), which it achieves by keeping
`soc[t] ≥ optimal_lower_soc_kwh`. The penalty strength controls how eagerly the
solver defends the optimal level against arbitrage opportunities.

When `optimal_lower_soc_kwh == 0.0` (the default), the constraint
`soc_low[t] >= 0 − soc[t]` is always satisfied at the `soc_low[t] = 0` lower bound.
No variables are added to the solver in this case — the default path is identical to
the current behaviour.

The same logic applies equally to `EvConfig`. This plan covers `BatteryConfig` only.
A subsequent plan may add the same fields to `EvConfig` if needed.

---

## Tests first

Add the following tests. Config-validation tests go in
`tests/unit/test_config_schema.py`; solver-behaviour tests go in
`tests/unit/test_battery_constraints.py`.

### Config tests (`test_config_schema.py`)

- `test_battery_optimal_lower_soc_kwh_defaults_to_zero` — a `BatteryConfig` with no
  `optimal_lower_soc_kwh` field set validates without error and the field value is 0.0.
- `test_battery_soc_low_penalty_defaults_to_zero` — similarly for
  `soc_low_penalty_eur_per_kwh_h`.
- `test_battery_optimal_lower_soc_cannot_exceed_capacity` — providing
  `optimal_lower_soc_kwh` greater than `capacity_kwh` must raise a `ValidationError`.
- `test_battery_optimal_lower_soc_cannot_be_below_min_soc` — providing
  `optimal_lower_soc_kwh` less than `min_soc_kwh` must raise a `ValidationError`.

### Solver-behaviour tests (`test_battery_constraints.py`)

- `test_soc_penalty_no_extra_variables_when_zero` — with default config
  (`optimal_lower_soc_kwh = 0.0`), the number of solver variables created by the
  battery device must equal the count produced by the pre-existing tests.
- `test_soc_low_is_zero_when_soc_above_optimal` — configure
  `optimal_lower_soc_kwh = 4.0 kWh` and force the battery SOC to remain above 4.0
  kWh for all steps. Assert `solver.var_value(soc_low[t]) < 1e-6` at each step.
- `test_soc_low_equals_deficit_when_soc_below_optimal` — configure
  `optimal_lower_soc_kwh = 4.0 kWh` and force the battery SOC to a known value of
  2.0 kWh at step 0. Assert `solver.var_value(soc_low[0])` is approximately 2.0.
- `test_soc_penalty_increases_soc_target` — with two equal-price steps and a battery
  at a low SOC, adding `soc_low_penalty_eur_per_kwh_h > 0` should cause the solver to
  prefer charging (higher terminal SOC) versus the zero-penalty baseline where the
  solver is indifferent.
- `test_soc_penalty_does_not_prevent_profitable_dispatch` — with a large price spread
  and a high SOC, the solver should still discharge aggressively even when the
  penalty is set. The penalty only defends the *deficit* below the optimal lower
  level; it does not cap discharge above it.

Run `uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py` — all new tests must fail before writing any implementation code.

---

## Implementation

### `mimirheim/config/schema.py` — `BatteryConfig`

Add two fields after `wear_cost_eur_per_kwh`:

```python
optimal_lower_soc_kwh: float = Field(
    default=0.0,
    ge=0.0,
    description=(
        "Preferred minimum state of charge in kWh. When the battery SOC falls "
        "below this level, the solver accrues a penalty proportional to the "
        "deficit. Acts as a soft lower bound: the solver may still dispatch "
        "below this level when the price spread justifies it. Must be >= "
        "min_soc_kwh and <= capacity_kwh."
    ),
)
soc_low_penalty_eur_per_kwh_h: float = Field(
    default=0.0,
    ge=0.0,
    description=(
        "Penalty rate for SOC below optimal_lower_soc_kwh, in EUR per kWh of "
        "deficit per hour. Set to 0.0 (default) to disable the penalty entirely. "
        "A value of 0.10 adds a 0.10 EUR × kWh-deficit × hours cost to the "
        "objective, discouraging dispatch below the optimal level for any price "
        "spread smaller than this rate."
    ),
)
```

Add a Pydantic model validator (after the field declarations):

```python
@model_validator(mode="after")
def _validate_soc_levels(self) -> "BatteryConfig":
    if self.optimal_lower_soc_kwh < self.min_soc_kwh:
        raise ValueError(
            f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
            f">= min_soc_kwh ({self.min_soc_kwh})"
        )
    if self.optimal_lower_soc_kwh > self.capacity_kwh:
        raise ValueError(
            f"optimal_lower_soc_kwh ({self.optimal_lower_soc_kwh}) must be "
            f"<= capacity_kwh ({self.capacity_kwh})"
        )
    return self
```

### `mimirheim/devices/battery.py` — `Battery`

**`add_variables`**

Compute the upper bound for `soc_low[t]`:

```python
soc_low_ub = max(0.0, config.optimal_lower_soc_kwh - config.min_soc_kwh)
```

If `soc_low_ub == 0.0`, skip creating any `soc_low` variables (default path —
no change in solver for the common case).

Otherwise, for each `t` in `ctx.T`:

```python
# soc_low[t] represents how far the battery SOC is below the optimal lower
# level at step t, in kWh. When SOC >= optimal_lower_soc_kwh, soc_low[t] = 0.
# When SOC < optimal_lower_soc_kwh, soc_low[t] = optimal_lower_soc_kwh - soc[t].
#
# Upper bound: the largest possible deficit is when soc[t] == min_soc_kwh.
# Lower bound: 0 (cannot have a negative deficit).
self._soc_low[t] = ctx.solver.add_var(lb=0.0, ub=soc_low_ub, name=f"soc_low_{t}")
```

**`add_constraints`**

After the existing SOC dynamics constraints, if `config.optimal_lower_soc_kwh > config.min_soc_kwh`:

```python
for t in ctx.T:
    # Ensure soc_low[t] captures the deficit below the optimal lower SOC.
    # Combined with the lower bound of 0 on soc_low[t], this pair of
    # constraints models soc_low[t] = max(0, optimal - soc[t]).
    #
    # Why not just constrain soc[t] >= optimal_lower_soc_kwh directly?
    # Because that would be a hard constraint — the solver could not dispatch
    # below the optimal level even at very high price spreads. The soft
    # constraint via soc_low[t] + penalty allows dispatch when it is
    # economically justified.
    ctx.solver.add_constraint(
        self._soc_low[t] >= config.optimal_lower_soc_kwh - self._soc[t]
    )
```

**`objective_terms`**

Add the penalty term when the rate is nonzero:

```python
terms = existing_wear_term  # the existing return value
if config.soc_low_penalty_eur_per_kwh_h > 0.0 and t in self._soc_low:
    terms = terms + config.soc_low_penalty_eur_per_kwh_h * self._soc_low[t] * dt
return terms
```

Comment the penalty term: explain that `dt` converts from EUR/kWh-hour to EUR/step.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_battery_constraints.py tests/unit/test_config_schema.py
```

All tests green.

```bash
uv run pytest tests/scenarios/
```

No golden file changes expected — existing scenarios use default config with
`optimal_lower_soc_kwh = 0.0`.

---

## Done

```bash
mv plans/21_battery_soc_penalty.md plans/done/
```
