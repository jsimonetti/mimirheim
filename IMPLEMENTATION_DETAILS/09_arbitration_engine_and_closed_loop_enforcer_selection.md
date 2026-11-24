# 9. Arbitration engine and closed-loop enforcer selection

**Module: `mimirheim/core/control_arbitration.py`**

After `build_and_solve()` and `apply_gain_threshold()`, the solve loop calls:

```python
result = assign_control_authority(result, bundle, config)
```

This pure function sets `DeviceSetpoint.zero_exchange_active` (and `loadbalance_active` for EVs) on every step in the schedule. It is the sole code path that sets these fields.

## Why the solver does not zero out closed-loop device variables

When a battery or EV will operate in closed-loop zero-exchange mode for a step, the solver variable for that device is **not** suppressed or fixed to zero. This is intentional.

The device will still be physically charging or discharging — it is just doing so autonomously under firmware control rather than following a numeric setpoint from mimirheim. If the solver does not model that behavior, the SOC trajectory across the horizon becomes incorrect: the solver assumes the battery is idle when it is actually absorbing or supplying several kilowatts. Incorrect SOC estimates cause wrong decisions on the steps immediately before and after the closed-loop step.

The correct model is: let the solver plan a numeric setpoint for the battery on closed-loop steps, because that plan represents the best prediction of what the hardware will actually do (absorbing surplus to achieve near-zero exchange). The post-process layer then overrides the published command with the closed-loop enable flag. The solver plan is advisory; the hardware firmware performs the real-time enforcement.

SOC state is continuous across all steps, including closed-loop steps. If the hardware does not track the solved setpoint exactly (expected — firmware PID loops are not perfect), the next solve cycle corrects the SOC trajectory using the fresh reading from MQTT.

## Enforcer selection

A step is **near-zero-exchange** when:

```
grid_import_kw <= exchange_epsilon_kw  AND  grid_export_kw <= exchange_epsilon_kw
```

Only near-zero-exchange steps trigger enforcer selection. All other steps clear all closed-loop flags.

A device is an **eligible candidate** for a near-zero-exchange step when:

1. Its capability flag is set (`zero_exchange` for batteries/EVs, `zero_export` for PV).
2. For EVs: the vehicle is plugged in (`bundle.ev_inputs[name].available` is True).
3. Its absorption headroom is >= `config.control.headroom_margin_kw`.
4. For batteries: it can regulate in both directions, judged on its start- and end-of-step state of charge against full and against the effective floor, and the step is not a protected hold (see "Battery eligibility" and "Protected holds" below).
5. For hybrid inverters: its battery is not near full or near empty (see "Hybrid inverter eligibility" below).

**Absorption headroom** is the additional power the device can absorb at its current operating point: for batteries and EVs, `max_charge_kw - actual_charge_kw + actual_discharge_kw`; for PV, the current production kW. For a battery or hybrid inverter whose SoC is known, the SoC-aware `absorb` value from "Battery eligibility" or "Hybrid inverter eligibility" replaces this formula, both for the threshold and for scoring level 2. Without a known SoC the formula above applies to batteries as before.

Candidates are scored by a four-level cascade (descending; later levels break ties):

1. Efficiency at the expected compensation power (PV always scores 0.0 — last resort).
2. Headroom margin (headroom minus expected compensation — more slack is better).
3. Wear proxy: lower `wear_cost_eur_per_kwh` wins.
4. Type priority (battery=3, EV=2, PV=1), then device name (lexicographic).

### Battery eligibility

The forecast error an enforcer corrects has no known sign, so a battery must be able to move at least `headroom_margin_kw` both ways around its planned setpoint:

```
absorb = charge_limit(soc)    (0 when full)   - charge_kw    + discharge_kw
supply = discharge_limit(soc) (0 when empty)  - discharge_kw + charge_kw
full   = start or end SoC >= capacity_kwh - margin_kwh
empty  = start or end SoC <= floor_kwh    + margin_kwh
eligible when absorb >= headroom_margin_kw and supply >= headroom_margin_kw,
              and both are above 0 (so headroom_margin_kw = 0 still excludes full and empty)
margin_kwh = max(headroom_margin_kw * 0.25 h, 0.001 * capacity_kwh)
floor_kwh  = max(min_soc_kwh, min(battery_care.floor_kwh, measured SoC))
```

`soc` is the start-of-step state of charge: the previous step's `soc_kwh` (which is an end-of-step value), or `bundle.battery_inputs[name].soc_kwh` for the first step. `charge_limit` and `discharge_limit` apply the linear derating of `reduce_charge_above_soc_kwh` / `reduce_discharge_below_soc_kwh`, the same lines the battery device imposes on the solver, computed by the shared `mimirheim/devices/derating.py` (§8, "Power derating near SOC extremes"). `margin_kwh` is the energy needed to sustain the margin for one step, with a floor of 0.1 % of capacity, so that solver or measurement rounding does not make a full or empty battery eligible when `headroom_margin_kw` is 0. The end-of-step SoC is checked too: a planned discharge that reaches the floor by the end of the step has already used the room a further deficit would need, and likewise for a charge that reaches the top.

`charge_kw` and `discharge_kw` are the planned power, with power at or below 0.05 kW counted as zero: solver noise must not lend a full or empty battery headroom (`kw = +0.003` at 100 % is idle, not a discharge). The threshold is a constant in `control_arbitration.py`, not a configuration field. `margin_kwh` is the only SoC tolerance in the arbitration.

`floor_kwh` is the lowest SoC the plan lets the battery reach. The battery-care ratchet raises it above `min_soc_kwh`; the solver applies it as `soc >= min(floor_kwh, measured SoC)`, so a battery that starts below the ratchet floor is held where it is. A closed-loop controller covering house load would discharge below that level and undo the ratchet.

Consequences:

- An idle battery at 100 % has no absorb headroom and an idle battery at `min_soc_kwh` has no supply headroom. Neither is selected, and the next candidate (a PV array with `zero_export`, an EV, a hybrid inverter) takes the role.
- A battery idle or charging at the top of its range has no absorb headroom, so the plan's hold at the top is never flagged; this needs no separate rule. Checking the end-of-step SoC catches a hold entered slightly low (for example start 99.5 %, end 100 %).
- A battery discharging from 100 % stays eligible when it discharges at least `headroom_margin_kw`: it absorbs a surplus by discharging less. A discharge at or below 0.05 kW counts as idle whatever the margin, so eligibility from the top needs a discharge above both. The same applies to a battery charging at `min_soc_kwh`, which covers a deficit by charging less.
- With a known SoC, a battery already charging or discharging at its power limit is not eligible, because it cannot move further in that direction. Without a known SoC only the absorb formula applies, as before this rule existed.

When a step carries no `soc_kwh` (a schedule whose dispatch `apply_gain_threshold` suppressed), the end-of-step SoC is carried forward as `start - kw * 0.25 h`, clamped to `[0, capacity_kwh]` (not to `min_soc_kwh`, so a measured SoC below the minimum is not moved up). With no measured SoC the step's published end SoC stands in for the start; with neither, the SoC rules are skipped, and only the battery-care hold applies.

Folding the former hold-at-the-top rule into the headroom check keeps the behaviour, except for floating-point ties at the exact boundary: `capacity_kwh * 0.999` and `capacity_kwh - capacity_kwh * 0.001` can differ in the last bits, and the derating line evaluated as `slope * soc + intercept` can round differently from the former interpolation. These differences are around 1e-15 and carry no physical meaning.

### Hybrid inverter eligibility

A hybrid inverter's setpoint is its net AC power, which includes its own PV on the DC bus. Its supply headroom cannot be read from that number: a full battery with PV can still discharge into the AC bus while exporting PV, and an empty one can export more by charging less from PV. The arbitration therefore applies a conservative gate instead of the battery's two-way headroom:

```
excluded when start or end SoC >= capacity_kwh - margin_kwh
           or start or end SoC <= min_soc_kwh  + margin_kwh
absorb   = charge_limit_dc(soc) / inverter_efficiency - charge_kw + discharge_kw
```

`margin_kwh` is the same tolerance as for batteries. `charge_limit_dc` is the derated DC charge ceiling from the shared helper; `charge_kw` and `discharge_kw` are taken from the net AC setpoint, as `_absorption_headroom` already does without a SoC. Hybrid inverters have no battery care, so the floor is `min_soc_kwh`.

The gate errs towards exclusion: a full hybrid that could absorb a surplus by curtailing PV, or an empty one exporting PV, is skipped and the next candidate takes the role. At mid SoC the supply direction is not checked, so a hybrid already discharging at its limit can still be selected.

A step without `soc_kwh` carries the last known SoC forward unchanged. Net AC power says nothing about the battery when PV covers it, so estimating SoC from power would let a full hybrid drift out of the gate. With no measured and no published SoC the gate is skipped.

### Protected holds

A closed-loop controller regulates grid exchange and drops the planned battery target. On a step where the plan holds the battery at a level, that lets the house load drain the hold. Such steps must not carry the flag, and a consumer that protects held steps would decline it anyway, leaving the step with no enforcer and EV load balancing suppressed. A battery is therefore excluded on:

- **Battery-care hold:** `battery_care.<name>.enforced_target_kwh` is set and the step's horizon index `t` satisfies `enforced_step <= t <= enforced_step + enforced_hold_steps`. The range is inclusive and keyed on `t`, not on list position.
  The window includes `enforced_step + enforced_hold_steps` even when the battery was already at the target as the horizon began, in which case the model pinned one step less. The published fields do not distinguish the two cases, and a consumer that protects the hold reads the same window; excluding one step too many costs at most one step without the battery as enforcer, while a narrower window would flag a step that consumer declines.
- **Hold at the top:** a battery idle or charging at the top of its range. The headroom rule above already excludes it (no absorb headroom), so it has no rule of its own. A consumer that protects held steps should classify idle with the same 0.05 kW threshold, otherwise it declines steps mimirheim flagged or the other way round.

An excluded battery is absent from the candidates. The cascade selects the next eligible device, a battery that loses eligibility mid-dwell is released on that step (dwell never keeps an ineligible enforcer), and loadbalance suppression follows the device actually selected.

**Hysteresis:** a challenger must exceed the current enforcer's efficiency score by `config.control.switch_delta` to trigger a switch.

**Minimum dwell:** once selected, a device holds the enforcer role for at least `config.control.min_enforcer_dwell_steps` consecutive steps, unless it becomes ineligible.

## Loadbalance suppression

When a battery is the `zero_exchange_active` enforcer for a step, any EV with `capabilities.loadbalance=True` receives `loadbalance_active=False` for that step. The battery's closed-loop controller and an EVSE loadbalance controller both target the same grid current transformer; only one may be authoritative per step.

An EV that is itself the `zero_exchange_active` enforcer receives `zero_exchange_active=True` and `loadbalance_active=False`. `loadbalance_active=True` is only set on steps where the EV is not the `zero_exchange` enforcer.
