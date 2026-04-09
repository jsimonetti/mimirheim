# Step 18 — Variable PV production with solver control

## References

- IMPLEMENTATION_DETAILS §7, subsection "Device Protocol"
- IMPLEMENTATION_DETAILS §8, subsection "PV device (fixed forecast)"
- `mimirheim/devices/pv.py` — `PvDevice`
- `mimirheim/config/schema.py` — `PvCapabilitiesConfig`, `PvConfig`
- `mimirheim/core/model_builder.py` — `build_and_solve`, power-balance assembly

---

## Files to modify

- `mimirheim/config/schema.py`
- `mimirheim/devices/pv.py`
- `tests/unit/test_pv_constraints.py`

---

## Background

Today `PvDevice.net_power(t)` returns a Python `float` equal to the forecast value.
The power balance in `build_and_solve` treats PV as a fixed parameter. The solver has
no way to reduce PV production — it must accept (and export, if necessary) every kWh
the array generates.

This is a problem when the export price is negative (the grid charges you to export).
The solver cannot curtail PV to avoid the forced export cost, even if the inverter
hardware supports power limiting.

This plan makes PV production a solver decision variable when the inverter supports it.
Two capability modes are introduced:

**Continuous power limit (`capabilities.power_limit: True`)**
The inverter accepts any production setpoint in `[0, forecast_kw]`. The solver adds
a continuous variable `pv_kw[t]` with upper bound `max(0.0, forecast[t])`. The solver
can reduce production to any level, including zero, at each step.

**On/off switching (`capabilities.on_off: True`)**
The inverter can only be fully on (produces the full forecast) or fully off (produces
zero). The solver adds a binary variable `pv_on[t]`. Production is `forecast[t] *
pv_on[t]`.

The two modes are independent. Both can be enabled simultaneously: the solver then
has a binary `pv_on[t]` and a continuous `pv_kw[t]`, with the constraint
`pv_kw[t] <= forecast[t] * pv_on[t]`.

When neither capability is enabled, PV remains a fixed forecast parameter (no change
in solver behaviour).

---

## Files to create

- None. All changes are in existing files.

---

## Tests first

Add to `tests/unit/test_pv_constraints.py`. Use a real solver with `T=2`, `dt=0.25`.

- `test_pv_fixed_forecast_net_power_is_float` — with both capabilities disabled,
  `pv.net_power(t)` returns a Python `float` equal to the forecast value. Confirm by
  checking `isinstance(pv.net_power(0), float)`.
- `test_pv_power_limit_variable_bounded_by_forecast` — with `power_limit: True` and
  a forecast of 5 kW, assert that the solver variable for `pv_kw[t]` has upper bound
  5.0 at each step (no overshooting the forecast).
- `test_pv_power_limit_curtails_at_negative_export_price` — two steps: step 0 has
  export price −0.05 EUR/kWh (you pay to export), step 1 has export price +0.10.
  Base load is zero. PV forecast is 3 kW. With `power_limit: True` and a
  `minimize_cost` strategy, assert that `pv.net_power(0)` resolves to 0.0 after
  the solve (solver turns off PV at step 0 to avoid paying to export) while step 1
  remains at 3 kW.
- `test_pv_on_off_binary_produces_full_or_zero` — with `on_off: True` and a forecast
  of 4 kW, force the solver to produce exactly 2 kWh over T=2 steps (constrain
  net export). Assert that each step's production is either 0.0 or 4.0, not an
  intermediate value.
- `test_pv_on_off_curtails_at_negative_export_price` — same scenario as
  `test_pv_power_limit_curtails_at_negative_export_price` but with `on_off: True`.
  Assert `pv_on[0] == 0` (binary off at the negative-price step).
- `test_pv_power_limit_and_on_off_combined` — with both capabilities enabled, assert
  that `pv_kw[t] <= forecast[t] * pv_on[t]` is satisfied at all steps.
- `test_pv_zero_export_mode_capability_flag_unchanged` — enabling `on_off` or
  `power_limit` does not suppress the `zero_export_mode` capability or change what
  is published on the `zero_export_mode` topic.

Run `uv run pytest tests/unit/test_pv_constraints.py` — all new tests must fail before
writing any implementation code. Existing passing tests must continue to pass after.

---

## Implementation

### `mimirheim/config/schema.py` — `PvCapabilitiesConfig`

Add one field:

```python
on_off: bool = Field(
    default=False,
    description=(
        "Inverter supports discrete on/off control. When True, mimirheim treats PV "
        "as a binary decision variable: the array either produces the full "
        "forecast or is switched off. Mutually usable with power_limit."
    ),
)
```

### `mimirheim/devices/pv.py` — `PvDevice`

`PvDevice` currently has no `add_variables` or `add_constraints` methods — it returns
only a float from `net_power`. Change as follows.

**New instance attribute:** `_net_power: dict[int, Any]` — maps step index to either a
float (fixed mode) or a solver expression (variable mode). Populated in
`add_variables` if capabilities require solver variables, otherwise populated in
`net_power` on first access.

**`add_variables(ctx: ModelContext, config: PvConfig, forecast: list[float]) -> None`**

Store `forecast` as `self._forecast`. Then:

```python
for t in ctx.T:
    f = max(0.0, forecast[t])   # clip any negative forecast values

    if config.capabilities.power_limit and config.capabilities.on_off:
        # Both modes: continuous variable bounded by binary × forecast.
        pv_on = ctx.solver.add_var(var_type="B", name=f"pv_on_{t}")
        pv_kw = ctx.solver.add_var(lb=0.0, ub=f, name=f"pv_kw_{t}")
        # Constraint added in add_constraints.
        self._pv_on[t] = pv_on
        self._pv_kw[t] = pv_kw
        self._net_power[t] = pv_kw

    elif config.capabilities.on_off:
        # Binary only: production is forecast or zero.
        pv_on = ctx.solver.add_var(var_type="B", name=f"pv_on_{t}")
        self._pv_on[t] = pv_on
        self._net_power[t] = f * pv_on

    elif config.capabilities.power_limit:
        # Continuous curtailment: production anywhere in [0, forecast].
        pv_kw = ctx.solver.add_var(lb=0.0, ub=f, name=f"pv_kw_{t}")
        self._pv_kw[t] = pv_kw
        self._net_power[t] = pv_kw

    else:
        # Fixed forecast: no solver variable.
        self._net_power[t] = f
```

**`add_constraints(ctx: ModelContext, config: PvConfig) -> None`**

Only needed when both `on_off` and `power_limit` are True:

```python
if config.capabilities.power_limit and config.capabilities.on_off:
    for t in ctx.T:
        f = max(0.0, self._forecast[t])
        # pv_kw[t] must not exceed forecast[t] × pv_on[t].
        # This is a Big-M constraint: when pv_on[t]=0 the upper bound on
        # pv_kw[t] is already 0 via its variable bound, but this constraint
        # formally links the binary to the power level.
        ctx.solver.add_constraint(self._pv_kw[t] <= f * self._pv_on[t])
```

**`net_power(t: int) -> Any`** (return type broadened to `Any`)

```python
return self._net_power[t]
```

**`objective_terms(t: int) -> int`**

Returns `0` — PV has no wear cost.

### `mimirheim/core/model_builder.py`

No changes required. The power balance already accepts solver expressions from
`device.net_power(t)` because Python numeric operations on solver variables produce
solver expressions. The type annotation on the `Device` Protocol's `net_power` method
may need to change from `float` to `Any | float` or `Any` — update `solver_backend.py`
or `context.py` accordingly if a Protocol type check fails.

### `mimirheim/io/mqtt_publisher.py`

After a solve cycle, the publisher reads setpoints from `ScheduleStep.pv_kw`. Confirm
that `build_and_solve` correctly reads `ctx.solver.var_value(pv_kw[t])` and populates
`ScheduleStep.pv_kw` accordingly. No new MQTT topics are required.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_pv_constraints.py
```

All tests green, including all pre-existing tests.

```bash
uv run pytest tests/scenarios/
```

Existing scenarios use the default (fixed PV) path and must remain green without
golden file updates.

---

## Done

```bash
mv plans/18_variable_pv_production.md plans/done/
```
