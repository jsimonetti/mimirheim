# Step 11 — Static load device

## References

- IMPLEMENTATION_DETAILS §8 (Device method contract)
- README.md (static load forecast MQTT topic)

---

## Files to create

- `mimirheim/devices/static_load.py`
- `tests/unit/test_static_load_constraints.py`

Note: `tests/unit/test_static_load_constraints.py` is not in the canonical test structure. Create it.

---

## Tests first

Create `tests/unit/test_static_load_constraints.py`. Use `T=4`, `dt=0.25`. Tests must fail before any implementation exists.

- `test_static_load_net_power_negative` — forecast `[1.0, 2.0, 0.5, 1.5]`; assert `net_power(t)` returns `-forecast[t]` at each step (load consumes power, hence negative)
- `test_static_load_adds_no_variables` — variable count on solver is unchanged after `add_variables` and `add_constraints`
- `test_static_load_objective_terms_zero` — `objective_terms(0)` returns 0
- `test_static_load_zero_forecast_step` — `net_power(t)` is 0.0 when forecast is 0.0 at that step

Run `uv run pytest tests/unit/test_static_load_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/static_load.py` — the simplest device. Represents inflexible loads whose consumption is known in advance from a forecast (e.g. base household consumption, always-on appliances). The solver cannot control these loads; they are treated as fixed parameters.

**`StaticLoadInputs`**:
- `forecast_kw: list[float]` — per-step power consumption in kW; one entry per horizon step

**`StaticLoad`**:
- `add_variables` — no-op
- `add_constraints` — stores `inputs.forecast_kw` for later use by `net_power`
- `net_power(t)` — returns `-self._forecast[t]`; negative because the load consumes power
- `objective_terms(t)` — returns 0

The sign convention (negative = consuming) matches the power balance constraint in `build_and_solve()`: static load pulls the balance down and must be met by generation or import.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_static_load_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/11_device_static_load.md plans/done/
```
