# 66 — Periodic climb to 100% SOC (battery care)

Status: agreed and implemented. The open question in section 6 was settled: **no
cap on normal cycling**. `capacity_kwh` remains the working ceiling, so a full
charge is reachable at any time and the ratchet plus the deadline is the whole
mechanism.

---

## 1. Ground truth: what BatteryLife actually does

Read from `victronenergy/dbus-systemcalc-py`, `delegates/batterylife.py` (master). Line
references are to that file.

| Question | Answer in the source |
|---|---|
| What triggers a step up | `on_discharged()` (L216), called when the state machine enters `BLDischarged`, i.e. `is_active_soc_low` (L133): SOC at or below the enforced floor, and below 100. **Not** a daily timer and **not** "failed to reach 100%" |
| Step size | `SocSwitchIncrement = 5.0` (L40), **percentage points of SOC**, not 5% of capacity |
| Cap | `SocSwitchMax = AbsorptionLevel - SocSwitchIncrement` = **80.0** (L45). Escape hatch: if the configured floor itself exceeds 80, `active_soclimit` returns it unbounded (L268-273) |
| What resets it | Not a true 100%. `on_absorption()` at SOC >= **85** removes 5 points (L222); `on_float()` at SOC >= **95** removes 5 for absorption and 5 for float, so up to 10 in one go (L227) |
| How the two combine | `active_soclimit = bound(0, max(minsoclimit, soclimit), 80)` (L268). The enforced floor is the **higher** of configured and dynamic |
| Re-arming | `_on_timer` clears all flags in the first 15-minute window after local midnight (L339-344). Each of the three transitions can therefore fire at most **once per calendar day** |

Two corrections to the task brief, both material:

1. **The trigger is a deep discharge, not a missed 100%.** A day spent cycling between
   50% and 70%, never touching the floor, does not ratchet anything. The mechanism
   punishes reaching the floor, and only indirectly encourages reaching the top.
2. **The reset is a decay, not a snap-back.** Reaching 95% once removes at most 10
   points. A floor that has climbed from 10 to 60 needs five more days at 95% to
   unwind, and `adjust_soc_limit` bases every step on `max(minsoclimit, soclimit)`, so
   it can never decay below the configured floor.

Also present but separate from the ratchet: `BLForceCharge` (L173), entered after
`ForceChargeInterval` = 24 h continuously in `BLDischarged`, which charges at 5 A.
That is the safety net for a battery pinned at the floor, not part of the climb.

The live Cerbo values in the brief are consistent with this reading:
`max(MinimumSocLimit 35, SocLimit 10)` bounded to 80 gives `ActiveSocLimit 35`.

**Worth stating plainly: a rising floor alone never guarantees a 100% charge.** The cap
is 80, and nothing in the delegate commands a charge to full. The floor squeezes the
usable window until normal solar and grid behaviour tops the battery up as a side
effect. On a system that is export-limited or short of sun, it can fail to balance
indefinitely.

## 2. What we keep, what we change, and why

**Keep**

- Raising the floor rather than commanding a charge. The solver keeps the choice of
  *when*, which is the entire reason to do this inside the optimiser.
- Enforced floor is the higher of configured and dynamic.
- An explicit cap.
- Reset keyed to an observed SOC, never a planned one.

**Change, with reasons**

| Change | Why |
|---|---|
| Trigger on **time since the last observed full charge**, not on hitting the floor | The thing that damages cells is elapsed time without balancing. Victron's trigger conflates that with depth of discharge. mimirheim is a 15-minute planning loop with the full history available on a retained topic, so it can key off the fact it actually cares about |
| Step **once per target interval**, not once per calendar day | The interval is the configured policy; a daily step is Victron's timer leaking into the design |
| On success, **reset the dynamic floor to zero** rather than decaying 5-10 points/day | Victron decays because its trigger fires per discharge episode and it needs hysteresis. With a time-based trigger, a success means the goal was met, and the next interval starts clean |
| Floor expressed in **kWh** | `BatteryConfig` is in kWh throughout (`capacity_kwh`, `min_soc_kwh`). The step and cap stay configured as percentages of capacity and are converted once |
| Add a **full-charge deadline constraint** as the mechanism that actually reaches full | See below. This is the deviation that needs the most justification |

### The deviation that matters: floor plus deadline

Requirement 3 asks for a climb rather than a command, and warns against "charge to 100%
at time T" because it ignores prices. Agreed on the warning, but a floor alone cannot
satisfy requirement 2, for the reason given at the end of section 1: capped at 80, it
never forces a full charge.

The proposal is two cooperating parts:

- **The ratchet floor**, Victron-shaped: each elapsed interval without a measured full
  charge raises the dynamic floor by `step_pct` of capacity, capped at `cap_pct`. This
  narrows the usable band and biases the plan upward at no cost in the objective.
- **A deadline**, and only once it enters the solve horizon: when
  `last_full_utc + target_interval` falls within the horizon, constrain the SOC at
  one step to reach `full_threshold_kwh`. Outside the horizon it adds nothing.

The deadline is a constraint on *state at a time*, not a command to charge at a time.
The solver chooses which quarter-hours to buy in, exactly as it already does for
`EvInputs.target_soc_kwh` with `window_latest` — the precedent for this shape is in the
codebase. That is what requirement 3 is actually asking for; what it warns against is
fixing the *charging* time, which this does not.

**Hard, not priced.** A soft penalty was built and abandoned. It is worth recording why,
because it looks like the safer option: it cannot make the model infeasible, so it needs
no reachability guard. Two things killed it.

1. It does not work under `minimize_consumption`. That strategy is lexicographic: phase 1
   minimises total import with only the *constraints* in the model, then locks
   `Σ import <= I*`, and only phase 2 sees device objective terms. A penalty added in
   phase 2 cannot buy energy the phase-1 budget already excluded, at any magnitude. Under
   `balanced` the same term competes with the configured weights and can be outbid at the
   defaults. A constraint is respected by both, because both phases carry it.
2. The penalty was a knob with no physical meaning. Setting it correctly required knowing
   that terminal SOC is internally credited at roughly the average import price divided by
   the step length; too low and the feature silently does nothing, too high and it
   distorts the reported objective value. Nothing could validate it, because the right
   value depends on the site's prices.

### Where the deadline step comes from

A hard constraint has one requirement: the step and the target must be reachable, or the
model is infeasible and a policy about cell balancing costs the entire schedule.

Reachability was first estimated in ~200 lines that walked the charge trajectory applying
derating, `min_charge_kw`, the capacity ceiling and the efficiency curve. Five review
rounds each found another configuration where it was wrong, each narrower than the last,
and wrong in *both* directions — estimate high and the solve is infeasible, estimate low
and an overdue deadline is dropped on every rolling solve. The estimator was reimplementing
the solver, and losing.

It is now answered by the solver. On the few cycles where a target is due, the model is
solved once minimising `Σ_b Σ_t max(0, target_b − soc_b[t])` over the due batteries — no
prices, no economics, and always feasible since holding the current SOC is a solution.
The resulting trajectory is a **witness**:

Summing each battery's own gap, rather than maximising total stored energy, is what keeps
this honest with two batteries: `max Σ soc` rewards filling the efficient battery past its
own threshold before charging a less efficient one at all, so a jointly optimal trajectory
can leave one short when a trajectory satisfying both exists. The gap variables exist for
the probe only and never enter the economic objective, so they are not a priced preference
and cannot be outbid.

- The target is pinned to the first step at or after the deadline where the witness
  reached the threshold. Because the witness satisfies that constraint, the model with it
  added is feasible by construction — that holds on every branch below, so the policy can
  never make the solve infeasible.
- "At or after" is what handles an overdue battery. Its deadline is step 0, and the anchor
  lands on an early quarter-hour it can genuinely be full by — the first such step in the
  witness rather than a proven earliest, and not on a step that is
  infeasible now, and not on a horizon end that recedes by one step on every rolling solve.
- When the witness never reaches the threshold, the best SOC it did reach is imposed
  instead — but only on a *proven optimal* probe. A trajectory that merely ran out of time
  is a lower bound on what the hardware can do, not a ceiling, and treating it as one
  would rewrite a reachable target down to whatever the solver happened to find. In that
  case nothing is imposed and `enforced_target_kwh` stays absent beside a set
  `full_target_kwh`, which is the signal that the probe could not settle the question.
- The probe must run after the grid hard caps are added. `ObjectiveBuilder` adds them at
  the top of `build()`, which is *after* this point, so the builder is constructed early
  and `add_hard_cap_constraints` called explicitly; it is idempotent. Without that the
  witness can rely on import the final model forbids.

Summing the gap over steps rather than measuring it once biases the witness early, since a
kWh stored at step 3 removes gap from every term after it. The guarantee is feasibility,
not earliest-possible and not maximal.

**What the reduced target does not prove.** Minimising the summed gap is not the question
"can this battery reach its target at *some* step". That is a disjunction and needs a
binary per step to ask properly. Two cases make the sum prefer a trajectory that never
reaches a reachable target: a battery hovering just under the threshold for many steps
scores better than one touching it once and then being forced down; and with two
batteries, refilling an already-satisfied one can shave more off the total than lifting
the other to its threshold. So a reduced `enforced_target_kwh` is a floor the model can
certainly meet, not a measured hardware limit. The loss is bounded — the charge is
under-enforced for one cycle and retried on the next, with the ratchet floor still
climbing — and the alternative that avoids the binaries is to enforce nothing at all,
which is strictly worse.

Cost: one extra solve, on deadline cycles only, taking a third of the cycle budget with
the remainder passed to the real solve — the same budget-splitting `minimize_consumption`
already does. At the default seven-day interval that is a handful of cycles a week out of
several hundred.

### Infeasibility trap in the floor

`soc[t] >= floor` with `soc[0]` fixed below the floor is infeasible on the spot, and the
floor can legitimately sit above the current SOC — that is the normal state of a
ratchet that has just stepped. The floor must therefore be applied as
`lb = min(dynamic_floor_kwh, inputs.soc_kwh)` for the hard bound, or through the
existing soft bound (`optimal_lower_soc_kwh` with `soc_low_penalty_eur_per_kwh_h`),
which already models "prefer not to go below this" without risking infeasibility.

**Resolved:** the hard clamp was built. It is what Victron does, it costs nothing in the
objective, and the clamp to the current SOC removes the infeasibility. See "Known
limitations" for what the single-constant clamp gives up.

## 3. Where the state lives

Two timestamps must survive a restart: `last_full_utc` and `care_since_utc`. The floor is
a pure function of a timestamp, the interval and the step, so no accumulator is kept — but
a battery never yet seen full measures its first interval from `care_since_utc`, and
losing that on every restart would keep its first balance charge permanently out of reach.

Following `readiness.py`, the retained MQTT message is authoritative:

- mimirheim observes SOC on the existing per-battery input topic
  (`battery_soc_topic`). When an observed SOC reaches `full_threshold`, it records the
  timestamp.
- It publishes that timestamp, the floor currently in force, and the time since the last
  full charge to a new retained per-battery status topic, `qos=1, retain=True`, via a new
  helper in `helper_common/topics.py` rather than a string literal.
- On startup it subscribes to that topic and takes the retained value as the starting
  `last_full_utc`. No new store, and the observable output doubles as the persistence,
  which is the pattern the brief asks for.

An absent retained value on first run means "never seen full": start the interval at
first observation rather than immediately ratcheting, so a fresh deploy does not step up
on day one.

## 4. Config sketch

Under `batteries.<name>`, all defaults reproducing today's behaviour exactly
(`enabled: false`):

```yaml
soc_ratchet:
  enabled: false
  target_interval_days: 7
  full_threshold_pct: 97.0
  step_pct: 5.0
  cap_pct: 80.0
```

`target_interval_days` default 7: Victron's effective daily cadence is set by a device
that cannot know anything about the site, and a weekly full charge is the common
manufacturer guidance for LFP cell balancing. Weekly also keeps the deadline outside a
48-hour horizon most of the time, so the mechanism is invisible until the week is nearly
up. To be restated in the docstring with this reasoning.

`full_threshold_pct` default 97 rather than 100: a BMS reporting 100 exactly is not
guaranteed even on a completed absorption cycle, and Victron itself resets on 95.

## 5. Tests required

1. The floor steps up when an interval elapses with no observed full charge.
2. The floor resets when an observed SOC crosses the threshold, and does **not** reset
   when a plan merely intended to.
3. The floor stops at the cap.
4. The retained timestamp is picked up on restart and the floor is reconstructed.
5. With a deadline inside the horizon and a price curve with a clear cheap window, the
   solver charges in the cheap window. This is the test that proves the mechanism is
   part of the optimisation rather than bolted on.
6. Feature disabled: byte-identical solver behaviour, and a golden scenario unchanged.

## 6. The open question, and how it was settled

Whether normal cycling should be capped below 100% for cell health (commonly 95%).

- **With a cap**, the periodic climb is a deliberate exception to it and must be able to
  reach a true full charge; if the cap clips the climb, nothing balances and the whole
  mechanism is decorative.
- **Without a cap**, the ratchet is the entire mechanism and `capacity_kwh` continues to
  mean what it means today.

**Decision: no cap.** `BatteryConfig` gains no second ceiling. `capacity_kwh`
stays the working limit, the deadline can reach `full_threshold_pct` at any
time, and no configuration loses usable capacity to a policy it did not ask
for. A site that wants a normal-operation ceiling can approximate one today by
lowering `capacity_kwh`, and a proper `max_soc_pct` can be added later without
disturbing anything here.

## 7. Known limitations, accepted deliberately

- **The floor clamp is one constant per horizon.** When the ratchet floor sits
  above the current SOC the bound becomes the current SOC, applied to every
  step, so the plan may charge above the floor and later discharge back to the
  starting level. Holding the floor once reached needs a binary per step or a
  price-blind charge ramp. The deadline is what forces the charge, so the gap
  costs nothing beyond a less tidy trajectory.
- **The cap is configurable, unlike Victron's fixed `SocSwitchMax` of 80.** The
  default matches upstream. A validator rejects a cap at or above
  `full_threshold_pct`, which is the configuration that would pin the battery
  full and useless.
- **The target can be reduced rather than refused.** When no trajectory reaches
  the threshold inside the horizon, the best SOC the witness did reach is
  imposed instead of the policy's target — a floor the model can certainly
  meet, not a measured hardware limit (see above). `enforced_target_kwh` below
  `full_target_kwh` on the status topic is the signal; the alternative —
  dropping the target — would leave the battery that is furthest behind with no
  instruction at all.
- **The witness is feasible, not optimal.** The gap objective biases the anchor
  early but does not prove it is the earliest possible step, and a reduced
  target is a floor rather than a measured limit (see above). Both directions
  are safe: every constraint imposed is one the witness already satisfies, so
  the policy can under-enforce for a cycle but can never make the solve
  infeasible.
- **A full-charge cycle is exempt from dispatch suppression.**
  `apply_gain_threshold` idles the battery when the optimised gain is below
  `min_dispatch_gain_eur`, which would strip the balance charge out of the
  published schedule while the status still reported the target as enforced.
  Mandatory work is now excluded from suppression, alongside EV and deferrable
  deadlines.
- **The schedule payload and debug dumps gain `battery_care`**, populated only
  when at least one battery enables the policy. The reporter fixture was
  updated to match, since it asserts coverage of every `SolveResult` field.

## 8. What was built

| Piece | Where |
|---|---|
| Policy schema | `SocRatchetConfig`, `BatteryConfig.soc_ratchet` in `config/schema.py` |
| Floor and deadline arithmetic | `core/battery_care.py` (`care_plan`, `observe_full_charge`) |
| Deadline anchor probe | `_probe_care_targets` in `core/model_builder.py` |
| Target constraint | `Battery.enforce_care_target` in `devices/battery.py` |
| Suppression exemption | `apply_gain_threshold` in `core/post_process.py` |
| Constraints | `Battery._add_care_constraints` in `devices/battery.py` |
| Status model | `BatteryCareStatus`, `SolveResult.battery_care` in `core/bundle.py` |
| Observation and restart seeding | `ReadinessState._observe_battery_care_locked` |
| Retained publish | `MqttPublisher.publish_battery_care` |
| Payload read-back | `parse_battery_care` in `io/input_parser.py`, registered in `io/mqtt_client.py` |
| Topic helper | `battery_soc_ratchet_topic` in `helper_common/topics.py` |

Tests: `tests/unit/test_battery_care.py` (arithmetic), `test_battery_care_solver.py`
(constraints, cheap-window, disabled no-op), plus additions to `test_readiness.py`,
`test_input_parser.py` and `test_mqtt_publisher.py`.
