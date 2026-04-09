# Step 08 — PV device

## References

- IMPLEMENTATION_DETAILS §8 (Device method contract)
- README.md (PV forecast MQTT topic, power sign convention)

---

## Files to create

- `mimirheim/devices/pv.py`
- `tests/unit/test_pv_constraints.py`

---

## Tests first

Create `tests/unit/test_pv_constraints.py`. Use `T=4`, `dt=0.25`. Tests must fail before any implementation exists.

- `test_pv_net_power_equals_forecast` — forecast `[2.0, 1.5, 0.0, 3.0]`; after `add_variables` and `add_constraints`, the net power at each step must equal the forecast value (these are constants, not variables — verify by checking the expression evaluates to a constant)
- `test_pv_negative_forecast_clipped_to_zero` — forecast `[2.0, -0.5, 1.0, 0.0]`; assert `net_power(1)` evaluates to 0.0, not -0.5
- `test_pv_net_power_positive` — PV produces power; net_power is positive (sign convention: positive = producing)
- `test_pv_adds_no_variables` — variable count on solver before and after `add_variables` is unchanged
- `test_pv_objective_terms_zero` — `objective_terms(0)` returns 0

Run `uv run pytest tests/unit/test_pv_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/pv.py` — the simplest device. PV output is a forecast parameter, not a decision variable. The solver cannot curtail PV in v1 (curtailment control may be added in a future step if the grid device cannot absorb all generation).

**`PvInputs`** (define in this file or in `bundle.py`):
- `forecast_kw: list[float]` — per-step forecast values

**`PvDevice`**:
- `add_variables` — no-op; PV has no decision variables
- `add_constraints` — no-op; the forecast is a parameter, not a constraint
- `net_power(t)` — returns `max(0.0, self._forecast[t])`; a constant float, not a solver expression. This is compatible with the power balance constraint because HiGHS accepts mixed constant/variable expressions. Clip to zero: a forecast of -0.1 kW due to sensor noise should not pull the power balance negative.
- `objective_terms(t)` — returns 0

The forecast list is stored during `add_constraints` from `PvInputs`. `net_power` must be callable only after `add_constraints` has been called.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_pv_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/08_device_pv.md plans/done/
```
