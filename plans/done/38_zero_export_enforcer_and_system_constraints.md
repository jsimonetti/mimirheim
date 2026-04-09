# Plan 38 — Zero-export enforcer selection and system-level dispatch constraints

## Motivation

Three connected issues identified during review of zero-export mode semantics.

### Issue 1 — Broadcasting zero_export_mode to all capable devices is wrong

Current behaviour: `apply_zero_export_flags` sets `zero_export_mode=True` on *every*
device that has `capabilities.zero_export_mode` enabled, for every step where
`grid_export_kw ≈ 0`.

This is architecturally incorrect. Each ZEM-capable device runs an independent
closed-loop firmware that watches the grid CT and adjusts its own output to keep
export at zero. When two or more such controllers run simultaneously on the same
AC bus they fight:

1. Battery absorbs 1 kW surplus → PV curtails 1 kW simultaneously → CT reads 1 kW
   import → both back off → surplus reappears → loop.
2. The oscillation amplitude is bounded only by the firmware's control bandwidth.
   In practice it causes unnecessary wear, hunting-induced inverter faults on some
   hardware, and potentially brief export spikes between correction cycles.

**Fix:** exactly one device is the enforcer per step. All others hold their
scheduled kW setpoints.

### Issue 2 — Inter-battery roundtrip energy waste (and same for EVs)

When two batteries are present, the solver may dispatch battery A to discharge
while battery B charges in the same step. Because there are two independent
`mode[t]` binaries (one per device), nothing prevents this today. The result is
energy round-tripping through two inverter efficiencies for no net gain.

The same issue applies to two EV chargers: one discharging V2H while another
charges.

Battery-to-EV and EV-to-battery flows are economically legitimate (different
wear costs, SOC constraints, departure targets) and must remain allowed.

**Fix:** shared system-direction binary per step for batteries, and a separate
one for EVs. Config-gated behind `constraints.prevent_battery_roundtrip` and
`constraints.prevent_ev_roundtrip`.

### Issue 3 — EV minimum charge power not enforced

AC EVSE hardware cannot deliver less than 6 A per phase. Below this threshold the
EVSE disconnects entirely. The current stacked-segment model has `lb=0.0` on every
segment variable, so the solver can dispatch an EV at 0.3 kW, which is not
physically realisable.

The minimum is implicit in hardware but not in the model. It needs a Big-M binary
variable: either the EV charges at ≥ min_kw or it does not charge at all in that
step.

The minimum is derived from config: `min_charge_kw = first_segment.power_max_kw`
per the interpretation that the first segment represents the lowest feasible
operating point, or from an explicit `min_charge_kw` config field. The first
segment approach is not reliable (segments can be very granular). An explicit
config field is cleaner.

V2H discharge: the 6 A minimum likely does not apply to the DC-DC stage of V2H
inverters. This plan treats discharge as continuous (lb=0.0).

---

## Relevant IMPLEMENTATION_DETAILS sections

- §3 Device constraint API (`add_variables`, `add_constraints`, `setpoint`)
- §4 Post-processing (`apply_zero_export_flags`)
- §6 Module boundary rules (devices never import from io)
- §10 Fault resilience

---

## Part A — Zero-export enforcer selection (post-process, no solver change)

### Design

Replace the current broadcast logic in `apply_zero_export_flags` with a
selection algorithm that designates exactly one enforcer per step.

**Enforcer selection criterion:**

For a step with `grid_export_kw ≈ 0`, the enforcer is the ZEM-capable device
with the highest *absorption headroom* at its scheduled operating point:

```
absorption_headroom(device, step) =
    max_charge_kw(device) - actual_charge_kw(device, step)
    + actual_discharge_kw(device, step)
```

Reading this: the device can absorb more by either ramping charge up or ramping
discharge down. The sum of both is the total available absorption range from
the current operating point.

For PV (curtailment-capable): `headroom = actual_production_kw`. The PV can
absorb the full surplus by curtailing to zero. This is the correct metric because
the PV firmware clamps output, so its effective "absorption" is production headroom.

**Helper: extracting actual power from DeviceSetpoint**

`DeviceSetpoint.kw` is the net AC power (positive = export to AC bus / produce /
discharge; negative = import / consume / charge). From `kw`:

- Battery/EV discharging: `kw > 0` → `discharge_kw = kw`, `charge_kw = 0`
- Battery/EV charging: `kw < 0` → `charge_kw = abs(kw)`, `discharge_kw = 0`
- PV: `kw >= 0` always → `production_kw = kw`

Max charge / max discharge per device is the sum of segment `power_max_kw`
values, readable from config.

**Tiebreak:** if multiple devices have equal headroom (unlikely in practice),
prefer battery > EV > PV. The battery is the most capable real-time regulator
and its wear cost is already accounted for in the objective.

**Non-enforcer devices:** receive `zero_export_mode=False`. Their kW setpoint
is unchanged — the firmware holds its assigned setpoint, it does not self-regulate.

**Steps with export (`grid_export_kw > 0`):** all devices get
`zero_export_mode=False`. No change vs current.

### Files to create/edit

- `mimirheim/core/post_process.py` — rewrite `apply_zero_export_flags` logic

### Tests to write first (in `tests/unit/test_post_process.py`)

All new tests must fail before the implementation is written.

1. `test_enforcer_is_single_device_per_step` — when three capable devices exist,
   only one gets `zero_export_mode=True` per step.

2. `test_battery_chosen_over_pv_when_equal_headroom` — battery wins tiebreak over
   PV when headroom is equal.

3. `test_ev_chosen_when_higher_headroom_than_battery` — EV with 8 kW headroom
   beats battery with 0.5 kW headroom.

4. `test_pv_chosen_when_only_capable_device` — single PV device with ZEM
   capability gets the flag.

5. `test_no_capable_devices_leaves_all_flags_none` — if no device has the
   capability, all `zero_export_mode` fields remain `None`.

6. `test_export_step_clears_all_flags` — step with `grid_export_kw = 1.5` sets
   all capable devices to `False`.

### Acceptance criteria

- All 6 new tests pass.
- All existing `apply_zero_export_flags` tests continue to pass (they already use
  single-device configs, so behaviour is identical for those cases).
- No solver changes.

---

## Part B — System-direction constraints: anti-roundtrip (solver change)

### Design

When two or more batteries are present, the solver may simultaneously discharge
battery A and charge battery B in the same step. Because each battery has an
independent `mode[t]` binary, the model places no restriction on their relative
directions.

Simultaneous charge/discharge within the same device type is always dominated by
the alternative of doing less: if A discharges 1 kW DC and B receives that kW as
AC input, the net energy stored is $1 \times \eta_{dis} \times \eta_{chg}$ kWh
— strictly less than if A did nothing and B charged 1 kW less. The efficiency and
wear costs in the objective already penalise this, but the penalty only tightens
the LP relaxation rather than hard-ruling it out. Under precise numeric conditions
(e.g. a step where the SOC penalty on A is slightly higher than the roundtrip
loss) the solver may still produce a roundtripping schedule that is technically
optimal under the objective but physically undesirable and confusing to operators.

**Design decision:** always apply the shared direction binary when ≥2 batteries
(or ≥2 EVs with V2H) are present. This is a declarative structural constraint:
it expresses the physical principle that energy flows should not go in a loop, and
it adds only one binary variable per step per device type — a negligible MILP
complexity increase. There is no config flag; the constraint is unconditional.

The documented edge case — battery A has a high SOC penalty and battery B has a
departure deadline, requiring opposite directions in the same step — is handled by
spreading the two actions across adjacent steps. The 15-minute horizon gives the
solver ample room to sequence them. The constraint is therefore not a feasibility
risk in any realistic residential configuration.

Introduce one binary variable `bat_system_mode[t]` per step (shared across all
batteries):

```
bat_system_mode[t] ∈ {0, 1}
  1 = all batteries charging this step
  0 = all batteries discharging this step
```

Replace the per-battery `mode[t]` Big-M guard with constraints that link each
battery to the shared binary:

```
total_charge[bat_i, t]    ≤ max_charge_kw[bat_i]    × bat_system_mode[t]
total_discharge[bat_i, t] ≤ max_discharge_kw[bat_i] × (1 − bat_system_mode[t])
```

When only one battery is present, no shared variable is created and the
per-device `mode[t]` is used unchanged. The condition is checked in
`build_and_solve`:

```python
if len(batteries) >= 2:
    # create bat_system_mode[t] and link each battery to it
```

The same structure applies to EVs: `ev_system_mode[t]` is introduced when ≥2
EV chargers are present, regardless of whether any of them have discharge segments
configured. When an EV has no discharge segments, its total_discharge is always
zero and the `(1 − ev_system_mode[t])` upper bound is trivially non-binding, so
the shared variable adds no constraining effect for that device. The capability
check is deliberately omitted to keep the activation logic uniform.

**Battery-to-EV and EV-to-battery cross-flows remain unrestricted.** The shared
binaries are per device type, not global.

### Implication for `mode[t]` variable ownership

Currently each `Battery` device adds its own `mode[t]` variable in
`add_variables`. This is retained for the single-battery case. When the shared
constraint is active, `build_and_solve` creates the shared variable after all
`add_variables` calls and passes it to each device via a new method
`set_external_mode(mode_vars: dict[int, Any])`. The device replaces its internal
per-step mode references with the externally supplied ones before constraints are
assembled.

### Files to create/edit

- `mimirheim/devices/battery.py` — add `set_external_mode(mode_vars: dict[int, Any])`
  method
- `mimirheim/devices/ev.py` — same
- `mimirheim/core/model_builder.py` — in `build_and_solve`, after all `add_variables`
  calls, if ≥2 batteries exist create shared variables and call
  `set_external_mode` on each battery; same for ≥2 V2H-capable EVs
- `tests/unit/test_battery_constraints.py` — new tests
- `tests/unit/test_ev_constraints.py` — new tests

### Tests to write first

Battery:
1. `test_two_batteries_cannot_roundtrip` — solve with a price spread that would
   otherwise incentivise roundtrip; confirm both batteries move in the same
   direction per step.
2. `test_single_battery_uses_per_device_mode` — single battery: `set_external_mode`
   is never called; per-device `mode[t]` is used.
3. `test_two_batteries_both_can_be_idle` — a step where both batteries are
   neither charging nor discharging is still feasible with the shared binary.

EV:
4. `test_two_v2h_evs_cannot_roundtrip` — same structure as battery test 1.
5. `test_two_charge_only_evs_get_shared_mode` — two EVs without discharge
   segments; `ev_system_mode` is still created; both EVs are constrained to
   charge-only direction (discharge bound is trivially satisfied).

### Acceptance criteria

- All 5 new tests pass.
- Golden file scenarios unaffected (all use single-device configs).
- No regression in existing battery and EV tests.

---

## Part C — Device minimum operating power (solver change)

### Motivation

All segment variables have `lb=0.0`, so the LP relaxation allows dispatch at
arbitrarily small power levels — e.g. 0.01 kW — when a binary direction variable
is set. Hardware cannot operate at such setpoints:

- AC EVSE hardware is subject to IEC 61851, which mandates ≥ 6 A per phase.
  Single-phase 6 A at 230 V = 1.38 kW; three-phase = 4.14 kW.
- Battery inverters similarly have a minimum operating power below which they
  shut down rather than hold a setpoint.

Without a floor constraint the solver generates physically unrealisable schedules.

### Design

Add optional `min_charge_kw` and `min_discharge_kw` fields to both `BatteryConfig`
and `EvConfig`. When set, a conditional lower bound is enforced: if the device is
active in that direction, the power must meet the minimum.

The existing `mode[t]` binary already means:
- `mode[t] = 1` → device is in charging direction
- `mode[t] = 0` → device is in discharging direction (only meaningful when
  `discharge_segments` is non-empty)

The constraints are therefore:

```
# Charge floor
total_charge[t]    >= min_charge_kw    × mode[t]

# Discharge floor (only when discharge_segments is non-empty)
total_discharge[t] >= min_discharge_kw × (1 − mode[t])
```

Each is a single linear constraint per step. No new variables are introduced.
Both constraints are tightened LP relaxations: they remove fractional `mode[t]`
solutions where the associated power is negligibly small, reducing branch-and-bound
depth.

Default for both fields: `None` (no floor). When `None`, the existing `lb=0.0`
behaviour is preserved.

**Three-phase vs single-phase**: the 6 A minimum translates to different kW values
depending on the installation. This is a config field; mimirheim does not auto-detect
phases.

**For Battery**: `min_discharge_kw` only applies when `discharge_segments` is
non-empty. When charge-only, the field is accepted in config but silently ignored
(no discharge variable exists to constrain).

**For EV**: same conditional on `discharge_segments`. When V2H is unconfigured,
`min_discharge_kw` is accepted but ignored.

### Files to create/edit

- `mimirheim/config/schema.py` — add `min_charge_kw: float | None = None` and
  `min_discharge_kw: float | None = None` to both `BatteryConfig` and `EvConfig`
- `mimirheim/devices/battery.py` — apply floor constraints when the fields are set
- `mimirheim/devices/ev.py` — apply floor constraints when the fields are set
- `tests/unit/test_config_schema.py` — new field tests for both device types
- `tests/unit/test_battery_constraints.py` — new tests
- `tests/unit/test_ev_constraints.py` — new tests

### Tests to write first

Battery:
1. `test_battery_min_charge_kw_enforced` — solver never dispatches battery
   charge between 0 and min_charge_kw; result is zero or ≥ min_charge_kw.
2. `test_battery_min_discharge_kw_enforced` — same for discharge direction.
3. `test_battery_min_charge_kw_none_allows_fractional` — when None, fractional
   dispatch is allowed.
4. `test_battery_min_discharge_kw_ignored_when_no_discharge_segments` — field
   set but discharge_segments empty; no error, no constraint added.

EV:
5. `test_ev_min_charge_kw_enforced` — with min_charge_kw=1.4, solver dispatches
   zero or ≥1.4 kW per charging step.
6. `test_ev_min_discharge_kw_enforced` — same for V2H discharge direction.
7. `test_ev_min_charge_kw_none_allows_fractional` — when None, fractional allowed.
8. `test_ev_min_discharge_kw_ignored_when_no_discharge_segments` — no error.

### Acceptance criteria

- All 8 new tests pass.
- No regression in existing battery and EV tests.
- Golden files unaffected (existing scenarios leave both fields unset, so lb=0
  is preserved).

---

## Sequencing

Implement A before B and C. A is a post-process bugfix that should be in place
before any solver changes alter device operating points. B and C are independent
solver extensions and can be done in either order.

Merge each part separately: A without B or C, B without C (or vice versa).

---

## Acceptance criteria (all parts combined)

- `uv run pytest` — all tests pass, no regressions.
- Running mimirheim with a two-battery config produces a schedule where no step has
  one battery charging and another discharging simultaneously.
- Running mimirheim with a battery or EV and `min_charge_kw: 1.4` produces a schedule
  where every non-zero charge step is ≥ 1.4 kW.
- Exactly one device per step receives `zero_export_mode=True` when multiple
  capable devices are configured and the step has zero export.
