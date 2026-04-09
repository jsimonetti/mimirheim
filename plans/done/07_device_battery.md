# Step 07 — Battery device

## References

- IMPLEMENTATION_DETAILS §8, subsections:
  - "Split charge/discharge variables"
  - "Piecewise efficiency (battery and EV)"
  - "Wear cost in objective terms"
  - "Vendor capability flags"

---

## Files to create

- `mimirheim/devices/battery.py`
- `tests/unit/test_battery_constraints.py`

---

## Tests first

Create `tests/unit/test_battery_constraints.py`. Use a real solver with `T=4`, `dt=0.25`. Tests must fail before any implementation exists.

- `test_battery_soc_tracks_charging` — fix all charge at 2 kW per step (single segment, efficiency=1.0); assert SOC increases by `2.0 × 0.25 = 0.5 kWh` per step
- `test_battery_soc_respects_capacity` — provide incentive to overcharge (import price negative); assert `soc[t] <= config.capacity_kwh` at all steps
- `test_battery_soc_respects_min_soc` — provide incentive to overdischarge; assert `soc[t] >= config.min_soc_kwh` at all steps
- `test_battery_no_simultaneous_charge_discharge` — provide conditions where simultaneous charge/discharge would be profitable; solve and verify at each step `charge[t] == 0` or `discharge[t] == 0` (not both nonzero)
- `test_battery_wear_cost_suppresses_cycling` — with `wear_cost_eur_per_kwh` large and equal import/export prices, total throughput should be zero
- `test_battery_single_segment_power_limit` — single segment with `power_max_kw=3.0`; assert total charge never exceeds 3.0 kW at any step
- `test_battery_two_segment_soc_uses_per_segment_efficiency` — two charge segments with different efficiencies; assert SOC increase matches per-segment efficiency calculation at a known operating point
- `test_battery_net_power_sign` — charging produces negative net_power (consuming); discharging produces positive net_power (producing)

Run `uv run pytest tests/unit/test_battery_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/battery.py` implements the Device Protocol for a DC-coupled residential battery.

### Variables (declared in `add_variables`)

For each time step `t` and each charge segment `i`:
- `charge_seg[t, i]` — power delivered to the battery in kW via segment i; bounds `[0, segment.power_max_kw]`
- `discharge_seg[t, i]` — power drawn from the battery in kW via segment i; bounds `[0, segment.power_max_kw]`

For each time step `t`:
- `soc[t]` — state of charge in kWh; bounds `[config.min_soc_kwh, config.capacity_kwh]`
- `mode[t]` — binary variable, 1 = charging, 0 = discharging; always created (unconditionally required to prevent the LP exploiting efficiency spread as free energy)

### Constraints (added in `add_constraints`, receives `BatteryInputs`)

**SOC initialisation:**
- `soc[-1]` is not a variable; use `inputs.soc_kwh` as the initial state when building the t=0 SOC update.

**SOC update (for each t):**
```
soc[t] = soc[t-1]
         + Σ_i (segment_i.efficiency × charge_seg[t, i] × dt)
         - Σ_i ((1 / segment_i.efficiency) × discharge_seg[t, i] × dt)
```
At `t=0`, `soc[t-1]` is replaced by `inputs.soc_kwh` (a constant).

**Simultaneous charge/discharge guard (always applied):**
```
charge_total[t]    ≤ max_charge_kw    × mode[t]
discharge_total[t] ≤ max_discharge_kw × (1 − mode[t])
```
where `max_charge_kw` is the sum of all charge segment `power_max_kw` values, and similarly for discharge.

This is a "Big-M" constraint. The binary `mode[t]` forces the solver to choose a direction at each step. Without it, the solver could simultaneously charge and discharge to exploit any efficiency asymmetry as free energy.

### net_power

```
net_power(t) = Σ_i discharge_seg[t, i] - Σ_i charge_seg[t, i]
```

Positive = net production (discharging). Negative = net consumption (charging).

### objective_terms

```
objective_terms(t) = +wear_cost_eur_per_kwh × (total_charge[t] + total_discharge[t]) × dt
```

Setting `wear_cost_eur_per_kwh=0.0` disables wear modelling.

Comment every variable and constraint to the required depth (AGENTS.md "Comment every non-trivial constraint and variable").

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_battery_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/07_device_battery.md plans/done/
```
