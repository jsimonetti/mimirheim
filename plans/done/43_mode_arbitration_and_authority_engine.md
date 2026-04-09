# Plan 43 — Arbitration engine, enforcer selection, and numeric robustness

## Motivation

Plan 42 defines the policy modes and replaces the field names. This plan
implements the behavioral logic: which device is the closed-loop enforcer for
each step, how candidates are scored, and how numeric noise is prevented from
causing command churn.

This plan entirely replaces `apply_zero_export_flags` in `post_process.py`.
That function must be deleted, not extended.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §3 Device lifecycle and contracts
- §8 Post-processing pipeline
- §10 Fault resilience and deterministic behavior

---

## Prerequisites

Plan 42 must be complete. `DeviceSetpoint.zero_exchange_active` must exist.
`apply_zero_export_flags` must already reference `zero_exchange_active` (even
if it has not been deleted yet).

---

## Design

### 1. New module: `mimirheim/core/control_arbitration.py`

Single public entry point:

```python
def assign_control_authority(
    result: SolveResult,
    bundle: SolveBundle,
    config: MimirheimConfig,
) -> SolveResult:
```

This function:
- Takes the post-threshold schedule.
- Iterates over steps.
- For each step, applies selection logic to choose at most one closed-loop
  enforcer among all `zero_exchange_active`-capable devices.
- Returns a new `SolveResult` with `DeviceSetpoint.zero_exchange_active` set
  correctly for all devices and all steps.

It is a pure function. No I/O, no logging beyond the `logger` the module
declares for itself, no MQTT, no file access.

### 2. Solve loop integration

In `mimirheim/__main__.py`, replace the call to `apply_zero_export_flags` with:

```python
result = assign_control_authority(result, bundle, config)
```

The `apply_zero_export_flags` call is removed. There is no dual path.

### 3. Delete `apply_zero_export_flags`

Remove `apply_zero_export_flags` and all supporting private functions from
`mimirheim/core/post_process.py`:

- `apply_zero_export_flags`
- `_absorption_headroom`
- `_max_charge_kw_from_config`
- `_ENFORCER_PRIORITY`
- `_ZEM_EPSILON`

Also remove from `post_process.py` module docstring the description of
`apply_zero_export_flags`. The module docstring must describe only
`apply_gain_threshold`.

### 4. Step classification

For each step, compute:

```
grid_exchange_kw = max(step.grid_import_kw, step.grid_export_kw)
```

(The two are mutually exclusive from the solver's power balance; exactly one
is nonzero per step, or both are zero at the deadband boundary.)

A step is a **near-zero-exchange step** when:
```
step.grid_import_kw <= exchange_epsilon_kw
AND step.grid_export_kw <= exchange_epsilon_kw
```

where `exchange_epsilon_kw` is a config parameter (see §7 below).

For steps that are not near-zero-exchange, all devices get `zero_exchange_active=False`.
No enforcer is needed when the solver has already dispatched the devices to produce
a nonzero net exchange.

### 5. Candidate eligibility

A device is a candidate enforcer for a step if all of the following hold:

1. It has `capabilities.zero_exchange: true` (battery or EV) or is a PV
   array with `capabilities.zero_export: true`. There is no separate
   policy field — the capability flag is the opt-in.
2. Its `zero_exchange_active` field is not `None` (i.e., capability is present in
   the schema and the output topic is configured).
3. For EV: `bundle.ev_inputs[name].available` is True (vehicle is plugged in).
4. Its absorption headroom for the step is >= `headroom_margin_kw`.

Absorption headroom is computed per device type:

**Battery or EV (charging direction):**
```
headroom = max_charge_kw - current_charge_kw + current_discharge_kw
```
where `current_charge_kw = max(0.0, -sp.kw)` and
`current_discharge_kw = max(0.0, sp.kw)`.

`max_charge_kw` is the sum of `charge_segments[*].power_max_kw`, or the last
breakpoint of the SOS2 curve if that model is configured.

**PV (curtailment direction):**
```
headroom = sp.kw
```
The inverter can absorb the full surplus by curtailing to zero.

### 6. Scoring eligible candidates

Among eligible candidates, rank by the following four-level cascade (all
descending). Each level is only consulted when the previous level produces a
tie. In practice, level 1 resolves the vast majority of real installations;
levels 3 and 4 are defensive tiebreakers that should not fire in production.

1. **Efficiency score**: operating-point efficiency for the expected compensation.

   Expected compensation = `max(grid_import_kw, grid_export_kw)` for the step.
   If the expected compensation is <= `exchange_epsilon_kw`, use a nominal score
   of 1.0 for all battery and EV candidates (no meaningful discrimination at
   near-zero power).

   For a battery or EV with segment model: identify the segment that the expected
   compensation falls into (by cumulative `power_max_kw` threshold). Use that
   segment's `efficiency`. Recall that the maximum charge power for a segment
   model is the sum of all segment `power_max_kw` values; this is also how the
   headroom computation works (see §5). If the expected compensation exceeds total
   capacity, use the lowest-efficiency segment (device is overstressed).

   For a battery or EV with SOS2 curve: linearly interpolate efficiency between
   the two adjacent breakpoints that bracket the expected compensation.

   For PV: score is `0.0`. PV curtailment forfeits free generation; it is always
   the last resort. Battery and EV will always outscore PV when they are eligible.
   The `0.0` score is intentional, not a placeholder.

   **When level 1 fires in practice**: any two devices with different efficiency
   curves or different model types. In a typical single-battery + EV installation,
   this resolves the winner outright.

2. **Headroom margin**: `absorption_headroom - required_compensation`. Larger
   margin means the device has more slack to track step-to-step disturbances.

   **When level 2 fires in practice**: two devices with the same efficiency score
   at the expected compensation — meaning two identical battery models, or two
   identical EV charger models. Rare in a residential installation; common in a
   dual-battery configuration with matched hardware.

3. **Wear proxy**: prefer the device with lower `wear_cost_eur_per_kwh` from
   config. PV has no wear cost parameter; treat as `0.0`.

   **When level 3 fires in practice**: two devices with the same efficiency AND
   the same headroom margin. This requires identical hardware and identical current
   operating points. Essentially a floating-point coincidence; this level exists
   to guarantee a defined output even in that degenerate case.

4. **Deterministic tie-break**: `(device_type_priority, device_name)` where
   type priorities are `battery=3, ev_charger=2, pv=1`.

   **When level 4 fires in practice**: only if levels 1–3 are all exactly tied.
   This is the final guard against non-determinism under floating-point arithmetic.
   The name sort ensures the same device is always chosen regardless of map
   iteration order.

The device with the highest tuple value is the enforcer. All others receive
`zero_exchange_active=False`.

### 7. Hysteresis and dwell

To prevent step-to-step enforcer switching when scores are close:

- Keep a retained enforcer state across steps within a single `assign_control_authority`
  call (i.e., per solve, not across solves).
- Challenge: switch only if challenger score exceeds current enforcer score by
  `switch_delta` (a config parameter; default 0.05).
- Minimum dwell: once selected, a device remains enforcer for at least
  `min_enforcer_dwell_steps` consecutive steps (default 2) unless it becomes
  ineligible (e.g., EV unplugs, headroom drops below margin).

### 8. `loadbalance` mode for EV

When an EV has `capabilities.loadbalance: true` and is plugged in,
`assign_control_authority` sets `loadbalance_active=True` for all steps
regardless of grid exchange. There is no step-conditional activation for
loadbalance. The EV's kW setpoint is zeroed in the output (not published) because
the EVSE firmware takes full control.

If a battery with `capabilities.zero_exchange: true` is present and
becomes the `zero_exchange_active` enforcer for a step, the EV's `loadbalance_active`
is set to `False` for that step. The loadbalance EVSE and the battery's closed-loop
exchange controller target the same physical grid current; only one may be
authoritative per step.

An EV may have both `capabilities.zero_exchange: true` and
`capabilities.loadbalance: true` simultaneously. The arbitration engine treats
`zero_exchange` as higher priority: if the step qualifies for
`zero_exchange` enforcement (near-zero exchange, EV plugged) and the EV is
selected as enforcer, `zero_exchange_active=True` and `loadbalance_active=False` are
set for that step. `loadbalance_active=True` is only set on steps where the EV is
not the `zero_exchange` enforcer.

### 9. Config additions

Add to a new `ControlConfig` Pydantic model in `schema.py` (top-level config key `control`):

```yaml
control:
  exchange_epsilon_kw: 0.05   # grid exchange below this is treated as near-zero
  headroom_margin_kw: 0.10    # minimum absorption headroom required for enforcer eligibility
  switch_delta: 0.05          # challenger must exceed current enforcer score by this amount
  min_enforcer_dwell_steps: 2 # minimum consecutive steps before enforcer can change
```

All fields optional with documented defaults. `ControlConfig` uses
`model_config = ConfigDict(extra="forbid")`.

### 10. EV `zero_exchange` unplugged fallback

When the selected enforcer is an EV and the EV is not plugged in for a step
(runtime condition, not schema-time), the EV is ineligible. Arbitration must
re-run for that step using the remaining candidates. If no candidates remain,
all devices get `zero_exchange_active=False` for that step and a debug log line
is emitted.

---

## Files to create/edit

### Tests (write first — all must fail before implementation)

1. New file: `tests/unit/test_control_arbitration.py`
   - `test_no_enforcer_on_nonzero_exchange_step`
   - `test_single_battery_zero_exchange_selected`
   - `test_battery_preferred_over_pv_when_both_eligible`
   - `test_higher_efficiency_device_wins_when_headroom_similar`
   - `test_ineligible_device_below_headroom_margin_excluded`
   - `test_ev_excluded_when_unplugged`
   - `test_loadbalance_ev_always_active_when_plugged`
   - `test_loadbalance_ev_suppressed_when_battery_closed_loop_enforcer`
   - `test_ev_with_both_closed_loop_and_loadbalance_closed_loop_takes_priority`
   - `test_hysteresis_prevents_flapping`
   - `test_min_dwell_respected`
   - `test_deterministic_tie_break_by_name`
   - `test_dispatch_suppressed_schedule_passthrough`

2. `tests/unit/test_post_process.py`
   - Delete or update all tests for `apply_zero_export_flags` (the function
     no longer exists). Tests for `apply_gain_threshold` are unchanged.

3. `tests/unit/test_config_schema.py`
   - `test_control_config_defaults_accepted`
   - `test_control_config_extra_field_rejected`

4. `tests/unit/test_mqtt_publisher.py`
   - Update to use `zero_exchange_active` field name (Plan 42 may have done this;
     verify and add coverage for battery `exchange_mode` and EV
     `exchange_mode` separately from PV `zero_export_mode`).

### Implementation

5. New: `mimirheim/core/control_arbitration.py`
   - Full implementation of `assign_control_authority` as described above.

6. `mimirheim/core/post_process.py`
   - Delete `apply_zero_export_flags`, `_absorption_headroom`,
     `_max_charge_kw_from_config`, `_ENFORCER_PRIORITY`, `_ZEM_EPSILON`.
   - Update module docstring.

7. `mimirheim/__main__.py`
   - Replace `apply_zero_export_flags(result, config)` with
     `assign_control_authority(result, bundle, config)`.
   - Add import for `assign_control_authority`.
   - Remove import for `apply_zero_export_flags`.

8. `mimirheim/config/schema.py`
   - Add `ControlConfig` model and `control: ControlConfig` field on `MimirheimConfig`.

9. `mimirheim/config/example.yaml`
   - Add `control:` section with all fields documented and defaults shown.
   - Add a clarifying comment to the `charge_segments` block under every battery
     example explaining that the sum of `power_max_kw` values is the maximum
     charge power for the device, not just the capacity of one segment. The
     current wording is ambiguous; readers incorrectly assume each segment has
     an independent upper bound rather than a cumulative one.

10. `IMPLEMENTATION_DETAILS.md`
    - Add `control_arbitration.py` under §6 module descriptions.
    - Update §8 post-processing pipeline to include arbitration step.

---

## Acceptance criteria

- `apply_zero_export_flags` and all its private helpers are deleted.
- `assign_control_authority` is the sole code path that sets `zero_exchange_active`.
- At most one device per step has `zero_exchange_active=True`.
- EV unplugged fallback is tested and handled silently.
- Hysteresis and dwell are tested and prevent obvious flapping in multi-step scenarios.
- All unit tests pass.
- Efficiency-aware scoring is active and covered by explicit tests.
- `ControlConfig` exists with `extra="forbid"` and documented defaults.
