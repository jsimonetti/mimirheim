# Plan 55 — Hybrid inverter post-solve integration gaps

## Motivation

Plan 54 correctly wired the hybrid inverter into the MILP model, the config
schema, and the MQTT publisher. However a review of the full solve pipeline
identified three functional gaps that were not part of Plan 54's scope:

| Finding | Component | Impact |
|---|---|---|
| F1 (critical) | `control_arbitration.py` | Hybrid inverter never included in `_collect_zex_capable`; `zero_exchange_active` is initialised to `False` and is never promoted to `True`. The exchange-mode firmware feature is silently non-functional. Also: missing branches in `_max_charge_kw`, `_efficiency_at_power`, and `_TYPE_PRIORITY`. |
| F2 (high) | `post_process.py` | `_CONTROLLABLE_TYPES` excludes `"hybrid_inverter"`. When dispatch is suppressed, the hybrid inverter retains its planned kW while batteries and EVs are zeroed. Grid import/export figures in the idle schedule are wrong. |
| F3 (medium) | `model_builder.py` | `_compute_soc_credit` does not iterate `bundle.hybrid_inverter_inputs`. SOC credit from hybrid battery charge is ignored, understating gain for hybrid inverter users and potentially triggering unnecessary dispatch suppression. |

Two housekeeping items are addressed alongside the functional fixes:

| Finding | Component | Impact |
|---|---|---|
| F4 (low) | `mqtt_client.py` | Trigger debounce interval `5.0` is a bare magic number. |
| F5 (low) | `mqtt_client.py` | `_on_connect`'s `connect_flags` parameter is unused in the function body. |

---

## Relevant source locations

```
mimirheim/core/control_arbitration.py   — _collect_zex_capable, _max_charge_kw,
                                          _efficiency_at_power, _TYPE_PRIORITY,
                                          _build_candidates (wear cost lookup)
mimirheim/core/post_process.py          — _CONTROLLABLE_TYPES, _build_idle_result
mimirheim/core/model_builder.py         — _compute_soc_credit
mimirheim/io/mqtt_client.py             — _on_message, _on_connect
tests/unit/test_control_arbitration.py
tests/unit/test_post_process.py
tests/unit/test_model_builder.py
```

## IMPLEMENTATION_DETAILS sections

§9 (Arbitration engine and closed-loop enforcer selection), §6 (boundary rules).

---

## Design decisions

### D1. Hybrid inverter type priority sits between EV and PV in arbitration

`_TYPE_PRIORITY` scores `battery=3`, `ev_charger=2`, `pv=1`. The hybrid
inverter combines battery and AC inverter into one unit. Its regulation
bandwidth is comparable to a standalone battery; it is preferred over a pure
EV (which has a charge-only constraint most of the time). Assign
`"hybrid_inverter": 2` — equal to EV. Ties are broken by headroom and name,
which is sufficient.

### D2. `_max_charge_kw` for hybrid inverters uses `max_charge_kw` from config

`HybridInverterConfig.max_charge_kw` is the DC-bus side limit. The AC-side
import limit is `max_charge_kw / inverter_efficiency`. For arbitration
purposes, the AC-side limit is the correct headroom denominator because
`DeviceSetpoint.kw` is net AC power. Use:

```python
if sp_type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is None:
        return 0.0
    return hi_cfg.max_charge_kw / hi_cfg.inverter_efficiency
```

### D3. `_efficiency_at_power` for hybrid inverters uses `inverter_efficiency`

A hybrid inverter has a single AC-to-DC conversion stage (`inverter_efficiency`)
rather than stacked segments. Return the inverter efficiency as the efficiency
score. This places the hybrid inverter above PV (0.0) but below a well-tuned
multi-segment battery (which may score > `inverter_efficiency` in its most
efficient segment).

```python
if sp_type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is None:
        return 0.0
    return hi_cfg.inverter_efficiency
```

### D4. Wear cost lookup in `_build_candidates` adds a hybrid inverter branch

The existing wear-cost lookup only checks `sp.type == "battery"`. A hybrid
inverter also has `wear_cost_eur_per_kwh` in its config:

```python
elif sp.type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is not None:
        wear_cost = hi_cfg.wear_cost_eur_per_kwh
```

### D5. `_build_idle_result` must zero hybrid inverter kW

Add `"hybrid_inverter"` to `_CONTROLLABLE_TYPES`. The idle schedule treatment
is identical to battery: set `kw=0.0`, preserve auxiliary fields
(`zero_exchange_active`).

### D6. `_compute_soc_credit` uses `hybrid_inverter_inputs` SOC delta

The hybrid inverter's net AC kW (`DeviceSetpoint.kw`) mixes PV generation and
battery dispatch and cannot be used directly to reconstruct SOC change.
Instead, use the initial SOC from `bundle.hybrid_inverter_inputs[name].soc_kwh`
and the terminal SOC reconstructed from the schedule's battery energy flow.

The schedule does not carry a separate battery kW field for hybrid inverters —
only net AC `kw`. The simplest correct reconstruction uses the initial SOC and
the terminal SOC variable value, but variable values are not available in
`_compute_soc_credit` (it runs after the solver context has been discarded).

The practical solution: add a `terminal_soc_kwh` field to `ScheduleStep` for
hybrid inverters — or more precisely, read it from the solver at the same
point where other variable values are extracted in the step-extraction loop of
`build_and_solve`, and store it in a separate dict alongside the schedule.

**Decision**: extend `ScheduleStep` with an optional `device_soc_kwh` dict
(maps device name → terminal SOC in kWh) alongside `devices`. Populate it
during schedule extraction in `build_and_solve` for all devices that expose
`terminal_soc_var(ctx)`. Then `_compute_soc_credit` can read
`step.device_soc_kwh` for the final step.

This is a clean approach because:
- `terminal_soc_var(ctx)` already exists on both `Battery` and
  `HybridInverterDevice` (added in Plan 54).
- The population loop in `build_and_solve` is a trivial addition next to the
  existing step-extraction code.
- `_compute_soc_credit` no longer needs to reconstruct SOC by summing AC kW
  (which would be wrong for hybrid inverters regardless).
- Battery credit calculation can be unified: both Battery and HybridInverter
  are covered by the same loop iterating `schedule[-1].device_soc_kwh`.

Existing battery tests that check `soc_credit_eur` must still pass. The
numerical result is the same (initial SOC comes from `bundle.battery_inputs`,
terminal SOC from `device_soc_kwh`; the difference equals `soc_delta`
computed previously from AC kW accumulation with η=1 in existing tests).

> **Scope note:** Extending `ScheduleStep` changes the JSON schema of the full
> schedule published to MQTT. The `device_soc_kwh` field must use
> `exclude_none=True` in all serialisation calls (already the case for
> `publish_result` and `debug_dump`). No external schema breakage.

### D7. F4 and F5 are mechanical changes, no design risk

`_DEBOUNCE_SECONDS: float = 5.0` at module level in `mqtt_client.py`.
`connect_flags` → `_connect_flags` in `_on_connect`.

---

## TDD workflow

### Step 1 — Run the baseline

```bash
uv run pytest tests/unit/test_control_arbitration.py \
              tests/unit/test_post_process.py \
              tests/unit/test_model_builder.py -q
```

Record the passing count. Stop if any of these three files have pre-existing
failures.

---

### Step 2 — Write failing tests

#### `tests/unit/test_control_arbitration.py`

Add a helper that builds a minimal config with a hybrid inverter that has
`capabilities.zero_exchange=True` and `outputs.exchange_mode` set. Use it in:

```python
def test_hybrid_inverter_included_in_zex_capable() -> None:
    """A hybrid inverter with zero_exchange=True appears in _collect_zex_capable."""

def test_hybrid_inverter_max_charge_kw_uses_ac_side() -> None:
    """_max_charge_kw for a hybrid_inverter returns max_charge_kw / inverter_efficiency."""

def test_hybrid_inverter_efficiency_uses_inverter_efficiency() -> None:
    """_efficiency_at_power for a hybrid_inverter returns config.inverter_efficiency."""

def test_hybrid_inverter_type_priority_is_two() -> None:
    """_TYPE_PRIORITY["hybrid_inverter"] == 2 (equal to EV, above PV)."""

def test_hybrid_inverter_selected_as_enforcer_when_only_candidate() -> None:
    """assign_control_authority sets zero_exchange_active=True on a
    near-zero-exchange step when the hybrid inverter is the only capable
    device and has sufficient headroom."""

def test_hybrid_inverter_wear_cost_penalises_selection() -> None:
    """When a battery and a hybrid inverter are both candidates but the
    battery has lower wear cost, the battery is selected as enforcer."""
```

#### `tests/unit/test_post_process.py`

Add a helper that builds a minimal `SolveResult` with a hybrid inverter
device setpoint carrying a non-zero `kw`. Use it in:

```python
def test_hybrid_inverter_zeroed_in_idle_schedule() -> None:
    """When dispatch is suppressed, the hybrid inverter kW is set to 0.0
    in the idle schedule."""

def test_idle_schedule_grid_balance_correct_with_hybrid_inverter() -> None:
    """The grid_import_kw / grid_export_kw in the idle schedule are computed
    from the correct power balance when a hybrid inverter is present.

    Setup: hybrid inverter discharging at 3 kW, base load at 2 kW, no PV.
    After suppression: hybrid idle → grid_import = 2 kW, grid_export = 0."""
```

#### `tests/unit/test_model_builder.py`

Add a helper that builds a minimal `SolveBundle` and `MimirheimConfig` with
a hybrid inverter. Use it in:

```python
def test_schedule_step_carries_device_soc_kwh_for_hybrid_inverter() -> None:
    """Each ScheduleStep.device_soc_kwh contains the terminal SOC in kWh for
    each hybrid inverter at that step."""

def test_soc_credit_includes_hybrid_inverter_battery() -> None:
    """_compute_soc_credit returns a nonzero credit when a hybrid inverter
    charges its battery during the horizon.

    Setup: single hybrid inverter, horizon=1, starts at 5 kWh, ends at 7 kWh.
    Credit = avg_import_price × 2 kWh × battery_discharge_efficiency."""
```

Confirm all new tests fail before writing any implementation.

---

### Step 3 — Implement F4 and F5 (mqtt_client.py)

These are the smallest, lowest-risk changes. Do them first so they can be
confirmed green before touching the solver pipeline.

**Changes:**

- Add `_DEBOUNCE_SECONDS: float = 5.0` near the top of the module (after
  imports, before `_HA_STATUS_TOPIC`).
- Replace the bare `5.0` literal in `_on_message` with `_DEBOUNCE_SECONDS`.
- Rename `connect_flags` to `_connect_flags` in `_on_connect`. Add a one-line
  comment: `# paho v2 always passes an empty ConnectFlags object; unused here.`

Run `uv run pytest tests/unit/test_mqtt_client.py -q` — must still pass.

---

### Step 4 — Implement F2 (`post_process.py`)

Add `"hybrid_inverter"` to `_CONTROLLABLE_TYPES`.

```python
_CONTROLLABLE_TYPES: frozenset[str] = frozenset({
    "battery", "ev_charger", "deferrable_load", "hybrid_inverter"
})
```

No other change required. `_build_idle_result` already handles any device
whose type is in `_CONTROLLABLE_TYPES` uniformly (zero kW, preserve auxiliary
fields).

Run: `uv run pytest tests/unit/test_post_process.py -q` — new tests should now pass.

---

### Step 5 — Implement F3 (`model_builder.py` + `bundle.py`)

#### 5a. Extend `ScheduleStep` in `bundle.py`

Add a new optional field:

```python
device_soc_kwh: dict[str, float] = Field(
    default_factory=dict,
    description=(
        "Terminal SOC in kWh for each storage device at this time step. "
        "Populated for all devices that expose terminal_soc_var(ctx). "
        "Empty for devices without an SOC variable (PV, static load, etc.)."
    ),
)
```

#### 5b. Populate `device_soc_kwh` in `build_and_solve`

Inside the step-extraction loop in `build_and_solve`, after the per-device
setpoints are assembled but before `schedule.append(...)`, add:

```python
device_soc_kwh: dict[str, float] = {}
for device in all_devices:
    get_soc = getattr(device, "terminal_soc_var", None)
    if get_soc is not None:
        var = get_soc(ctx)
        if var is not None:
            device_soc_kwh[device.name] = _round4(ctx.solver.var_value(var))
```

Pass `device_soc_kwh=device_soc_kwh` to `ScheduleStep(...)`.

Note: `ObjectiveBuilder` already calls `terminal_soc_var` via the same
`getattr` pattern. This is the same duck-typed dispatch and requires no
protocol change.

#### 5c. Rewrite `_compute_soc_credit` to use `device_soc_kwh`

Replace the existing battery and EV SOC-delta reconstruction with a single
loop over `schedule[-1].device_soc_kwh`:

```python
def _compute_soc_credit(
    bundle: SolveBundle,
    schedule: list[ScheduleStep],
    config: MimirheimConfig,
    dt: float,
) -> float:
    ...
    n = len(schedule)
    avg_import_price = sum(bundle.horizon_prices[t] for t in range(n)) / n
    credit = 0.0

    # For each storage device that exposes a terminal SOC, compute the credit
    # as: avg_import_price × delta_cell_kwh × avg_discharge_efficiency.
    # delta_cell_kwh = terminal_soc - initial_soc.
    # Initial SOC comes from the bundle; terminal SOC from device_soc_kwh.

    terminal_soc = schedule[-1].device_soc_kwh

    for name, inputs in bundle.battery_inputs.items():
        bat_cfg = config.batteries.get(name)
        discharge_eff = _avg_discharge_efficiency(
            bat_cfg.discharge_segments if bat_cfg else None,
            bat_cfg.discharge_efficiency_curve if bat_cfg else None,
        )
        term_soc = terminal_soc.get(name)
        if term_soc is None:
            continue
        soc_delta = term_soc - inputs.soc_kwh
        credit += avg_import_price * soc_delta * discharge_eff

    for name, inputs in bundle.hybrid_inverter_inputs.items():
        hi_cfg = config.hybrid_inverters.get(name)
        if hi_cfg is None:
            continue
        # Discharge efficiency for the battery cells inside the hybrid inverter.
        # The inverter stage efficiency is already captured in dc_to_ac; here
        # we only credit the stored cell energy at its battery discharge eff.
        discharge_eff = hi_cfg.battery_discharge_efficiency
        term_soc = terminal_soc.get(name)
        if term_soc is None:
            continue
        soc_delta = term_soc - inputs.soc_kwh
        credit += avg_import_price * soc_delta * discharge_eff

    for name, inputs in bundle.ev_inputs.items():
        if not inputs.available:
            continue
        ev_cfg = config.ev_chargers.get(name)
        discharge_eff = _avg_discharge_efficiency(
            ev_cfg.discharge_segments if ev_cfg else None,
            None,
        )
        term_soc = terminal_soc.get(name)
        if term_soc is None:
            continue
        soc_delta = term_soc - inputs.soc_kwh
        credit += avg_import_price * soc_delta * discharge_eff

    return credit
```

> **Why this is numerically compatible with existing battery tests:**
> Previously, `soc_delta` was reconstructed by accumulating `−kw × dt` across
> all steps. The new approach reads `terminal_soc_var` directly. Both yield
> the same value: the constraint `soc[t] = soc[t-1] + (charge - discharge) × dt`
> is enforced by the solver, so the variable value at `T-1` equals exactly
> what the accumulation formula would produce. Floating-point residuals are
> eliminated in both cases by `_round4`.

Run: `uv run pytest tests/unit/test_model_builder.py -q` — new tests should now pass.

---

### Step 6 — Implement F1 (`control_arbitration.py`)

Make five targeted additions to the module. All are additive (no existing
code is removed or restructured).

#### 6a. `_TYPE_PRIORITY`

```python
_TYPE_PRIORITY: dict[str, int] = {
    "battery": 3, "hybrid_inverter": 2, "ev_charger": 2, "pv": 1
}
```

#### 6b. `_max_charge_kw` — add hybrid inverter branch

After the `ev_charger` branch:

```python
if sp_type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is None:
        return 0.0
    # The AC-side import limit is the DC charge limit divided by inverter
    # efficiency. DeviceSetpoint.kw is net AC power, so the headroom
    # calculation must use the AC limit, not the DC-bus limit.
    return hi_cfg.max_charge_kw / hi_cfg.inverter_efficiency
```

#### 6c. `_efficiency_at_power` — add hybrid inverter branch

After the `ev_charger` branch:

```python
if sp_type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is None:
        return 0.0
    # A hybrid inverter has a single AC-to-DC conversion stage. Its
    # efficiency at any operating point is approximately inverter_efficiency.
    # This places it above PV (0.0) and below a high-efficiency battery
    # segment where efficiency may be 0.97-0.99.
    return hi_cfg.inverter_efficiency
```

#### 6d. Wear cost in `_build_candidates` — add hybrid inverter branch

In the wear-cost block that currently only checks `sp.type == "battery"`:

```python
elif sp.type == "hybrid_inverter":
    hi_cfg = config.hybrid_inverters.get(name)
    if hi_cfg is not None:
        wear_cost = hi_cfg.wear_cost_eur_per_kwh
```

#### 6e. `_collect_zex_capable` — add hybrid inverter iteration

```python
for name, hi_cfg in config.hybrid_inverters.items():
    if hi_cfg.capabilities.zero_exchange:
        capable.add(name)
```

No change needed to `assign_control_authority`'s main loop or the
`enforcer_is_battery` flag. The flag is used to suppress EV loadbalance when
the battery is enforcing; a hybrid inverter enforcing is semantically
equivalent (same controller oscillation risk). The existing check
`config.batteries.get(enforcer_name) is not None` stays unchanged; it will
simply return `None` for hybrid inverters, meaning `enforcer_is_battery=False`.
This is correct: a hybrid inverter and an EVSE loadbalance controller regulate
different physical quantities and do not interfere the same way.

> If a future plan determines that hybrid-inverter-enforcing also conflicts with
> EV loadbalance, the `enforcer_is_battery` check can be extended then.

Run: `uv run pytest tests/unit/test_control_arbitration.py -q` — new tests
should now pass.

---

### Step 7 — Full test suite

```bash
uv run pytest -q --tb=short
```

All tests in the three targeted files must now pass. Pre-existing failures in
`mimirheim_helpers/pv/pv_ml_learner/` are known and unrelated; they must not
increase in count.

---

## Files to create or edit

| File | Action |
|---|---|
| `mimirheim/core/bundle.py` | Add `device_soc_kwh: dict[str, float]` to `ScheduleStep` |
| `mimirheim/core/model_builder.py` | Populate `device_soc_kwh` in step-extraction loop; rewrite `_compute_soc_credit` |
| `mimirheim/core/post_process.py` | Add `"hybrid_inverter"` to `_CONTROLLABLE_TYPES` |
| `mimirheim/core/control_arbitration.py` | Add `"hybrid_inverter": 2` to `_TYPE_PRIORITY`; hybrid inverter branches in `_max_charge_kw`, `_efficiency_at_power`, `_build_candidates`; add iteration in `_collect_zex_capable` |
| `mimirheim/io/mqtt_client.py` | Add `_DEBOUNCE_SECONDS`; rename `connect_flags` |
| `tests/unit/test_control_arbitration.py` | New hybrid inverter arbitration tests |
| `tests/unit/test_post_process.py` | New hybrid inverter idle schedule tests |
| `tests/unit/test_model_builder.py` | New `device_soc_kwh` and SOC credit tests |

---

## Acceptance criteria

- [ ] `test_hybrid_inverter_included_in_zex_capable` passes.
- [ ] `test_hybrid_inverter_selected_as_enforcer_when_only_candidate` passes.
- [ ] `test_hybrid_inverter_zeroed_in_idle_schedule` passes.
- [ ] `test_idle_schedule_grid_balance_correct_with_hybrid_inverter` passes.
- [ ] `test_schedule_step_carries_device_soc_kwh_for_hybrid_inverter` passes.
- [ ] `test_soc_credit_includes_hybrid_inverter_battery` passes.
- [ ] All pre-existing tests in the three targeted files continue to pass.
- [ ] Full `uv run pytest` shows no new failures beyond the known pv_ml_learner set.
