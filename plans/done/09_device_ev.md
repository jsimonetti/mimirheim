# Step 09 — EV charger device

## References

- IMPLEMENTATION_DETAILS §8, subsections:
  - "Split charge/discharge variables"
  - "Piecewise efficiency (battery and EV)"
  - "Wear cost in objective terms"

---

## Files to create

- `mimirheim/devices/ev.py`
- `tests/unit/test_ev_constraints.py`

---

## Tests first

Create `tests/unit/test_ev_constraints.py`. Use a real solver with `T=8`, `dt=0.25` (2-hour horizon). Tests must fail before any implementation exists.

- `test_ev_not_plugged_zero_charge` — `available=False`; assert all `charge_seg[t, i]` are 0 at every step
- `test_ev_reaches_target_soc_within_window` — `available=True`, `soc_kwh=10.0`, `target_soc_kwh=20.0`, sufficient charge capacity; assert `soc` at the window_latest step is `>= 20.0`
- `test_ev_soc_respects_capacity` — incentive to overcharge; assert `soc[t] <= config.capacity_kwh` at all steps
- `test_ev_soc_respects_min_soc` — assert `soc[t] >= config.min_soc_kwh` at all steps
- `test_ev_no_discharge_without_discharge_segments` — `discharge_segments=[]` in config; assert no discharge variables exist in the model
- `test_ev_wear_cost_suppresses_cycling` — large wear cost; equal import/export prices; assert net throughput is minimal
- `test_ev_net_power_sign` — charging is negative net_power; discharging is positive

Run `uv run pytest tests/unit/test_ev_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/ev.py` is structurally identical to `battery.py`. The key differences are:

**Charging window constraint:** When `available=True` and `window_latest` is set, the EV must reach `config.target_soc_kwh` by the window_latest step. Convert `window_latest` to a step index using `bundle.solve_time_utc` and `ctx.dt`:

```python
def _datetime_to_step(dt_value: datetime, solve_time: datetime, dt_hours: float) -> int:
    delta_hours = (dt_value - solve_time).total_seconds() / 3600.0
    return int(delta_hours / dt_hours)
```

Add the constraint: `soc[window_latest_step] >= config.target_soc_kwh`.

**Availability gate:** When `available=False`, add constraints forcing all charge and discharge segment variables to 0 at every step. Do not add a SOC update constraint (the SOC is not meaningful when unplugged). The SOC initial value from `EvInputs.soc_kwh` is still used as the final SOC when the EV reconnects — but this tracking is a concern for future steps; for v1, simply zero all power when unavailable.

**Discharge segments optional:** Check `len(config.discharge_segments) > 0` before creating discharge variables. If no discharge segments are configured, skip discharge variable creation entirely.

`solve_time_utc` is passed in via `add_constraints` — the device receives it from the caller (model_builder), not by fetching it from a global or IO layer.

Comment the availability gate and window constraint to full depth — these are the most important operational constraints for EV users and the most likely to confuse a new contributor.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_ev_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/09_device_ev.md plans/done/
```
