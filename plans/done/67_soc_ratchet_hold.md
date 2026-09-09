# 67 — Hold at the top, and plan for 100% (soc_ratchet)

Status: agreed, implementing on top of 66.

## 1. Ground truth

Read from `victronenergy/dbus-systemcalc-py`, `delegates/batterylife.py` (master) and
the Victron ESS and Lithium Battery Smart manuals.

| Question | Answer in the source |
|---|---|
| Does BatteryLife hold the battery full | No. `_default` enters `BLAbsorption` the instant `soc >= 85` and `BLFloat` the instant `soc >= 95`; it leaves them 3 points lower (`SocSwitchOffset`). Nothing waits for time at the top |
| Where the balancing dwell comes from | The charger's absorption phase, not BatteryLife. Lithium Battery Smart manual: "Absorption time: 2 hours", "at least 2 hours in absorption charge mode each month", "4 to 8 hours per month for more heavily cycled (off-grid or ESS) systems" |
| What SOC the charger targets | CVL, which is 100%. BatteryLife's 85/95 are its own reset thresholds, not the charge target |

Two consequences for the ratchet as delivered by 66:

1. **It has no hold.** `soc[step] >= target` binds one quarter-hour; the solver is free to
   discharge in the next. Under Dynamic ESS local mode nothing else guarantees the
   charger its 2 hours at CVL. Passive balancing bleeds at tens of mA and only in the
   upper knee; SOC recalibration needs the tail-current condition at CVL. A touch does
   neither.
2. **It plans for the threshold it resets on.** `full_target_kwh` is
   `_full_threshold_kwh`, so it plans for 97% and resets on 97%. 97% may be the edge of
   the knee, not CVL. The executor already separates these: `target_soc: 100`,
   `success_soc: 98`.

## 2. Decisions

| Decision | Value | Why |
|---|---|---|
| `hold_hours` | 2.0 | Victron's absorption time for lithium; matches the executor's `hold_duration: "2h"`. 7 days x 2 h = 8 h/month, the top of Victron's ESS range |
| `target_pct` | 100.0 | Plan for CVL. Reset stays on `full_threshold_pct` (97), because a BMS is not guaranteed to report a round 100 |
| Success is measured, including the hold | yes | 66's own principle: never a planned value. The reset fires when a reading has been at or above the threshold for `hold_hours`, and records the completion time |
| Run detection in the probe | first step at or after the deadline where the witness is at target, then the contiguous run from there, clipped to `hold_steps` and the horizon | Searching for the best run is the per-step disjunction the probe already refuses. A short run is the documented bounded, self-correcting case |
| Tolerance on the way in | `witness >= target - eps` | `target_pct` 100 puts the target on the variable's upper bound; a backend that lands a hair under would otherwise fall to the reduced branch every cycle. Enforce `min(target, run_min - eps)` so the witness still satisfies it |
| `above_since` persistence | in memory only | A restart mid-hold costs one extra hold. Not worth a second retained field |

## 3. Tests first

- `test_config_schema`: defaults; `target_pct < full_threshold_pct` rejected; `hold_hours` bounds.
- `test_battery_care`: `hold_steps`; target and threshold distinct; `track_full_charge`:
  a touch does not reset with a hold set, sustained readings do, a dip restarts the
  run, `hold_hours=0` reproduces a touch.
- `test_battery_care_solver`: hold of 8 steps enforced; run clipped at the horizon end;
  load forcing discharge mid-run yields a shorter enforced run; `target_pct=100` reaches
  the bound.
- `test_readiness`: one wiring test with a hold set.

## 4. Files

`schema.py`, `battery_care.py`, `readiness.py`, `battery.py`, `model_builder.py`,
`bundle.py`, `mqtt_publisher.py`, `schema.json` (regenerated), `README.md`.

## 5. Acceptance

`uv run pytest` green, `uv run ruff check .` clean, `test_schema_json_is_up_to_date`
passing after regeneration.

## 6. Known and out of scope

- A BMS that never reports `full_threshold_pct` never resets, and the target is
  re-imposed at step 0 every cycle. Pre-existing in 66 (pins at 97); with this change
  it pins at 100. A measured-vs-planned divergence alarm is the fix, not this step.
- No hysteresis on the hold: one reading below the threshold restarts it. Revisit if a
  BMS reporting integer percent proves noisy around 97.
- The reduced-target branch (witness never reaches `target_pct`, typically charge
  derating near the top on a short horizon) enforces a single step, not a run. A pack
  in that situation should have `target_pct` set to what it does reach, so the run
  branch applies; extending the fallback to a run is low value because its `best_step`
  under derating is usually the horizon end anyway.
- The executor and the interval are untouched.
