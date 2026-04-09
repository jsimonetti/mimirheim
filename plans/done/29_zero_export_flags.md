# Step 29 — Zero-export mode flags for PV and EV devices

## References

- IMPLEMENTATION_DETAILS §1 — Configuration parsing & validation
- IMPLEMENTATION_DETAILS §6 — Pydantic config models
- IMPLEMENTATION_DETAILS §7 — SolveBundle and per-device input models (`DeviceSetpoint`)
- IMPLEMENTATION_DETAILS §8 — `post_process.py`, `apply_zero_export_flags` (currently marked as
  "not yet implemented")
- `mimirheim/config/schema.py` — `PvCapabilitiesConfig`, `PvOutputsConfig`, `CapabilitiesConfig`, `EvConfig`
- `mimirheim/core/bundle.py` — `DeviceSetpoint.zero_export_mode`
- `mimirheim/core/model_builder.py` — EV setpoint construction (currently leaves `zero_export_mode=None`)
- `mimirheim/core/post_process.py` — `apply_zero_export_flags` stub (documented, not implemented)
- `mimirheim/io/mqtt_publisher.py` — PV control output topics section
- `mimirheim/io/ha_discovery.py` — PV zero-export discovery

---

## Background

`zero_export_mode` is a boolean hardware command: when `True`, the device is instructed to
prevent grid export at its point of connection. The solver makes the _economic_ decision (does
export make sense this step?); the post-processor translates that decision into the boolean flag
that hardware automation reads.

Today this capability is modelled only on PV inverters (`PvCapabilitiesConfig.zero_export_mode`).
The flag is set to a constant `False` in `model_builder.py` and `apply_zero_export_flags` in
`post_process.py` is documented but empty.

Modern EV chargers also support this concept — sometimes called dynamic load balancing or
zero-export mode. The charger listens to a reference topic and continuously adjusts its charge
rate to absorb excess solar and prevent grid export. V2H-capable EVs extend this further: they
can also _discharge_ into the home to cover load, keeping the grid connection at exactly zero in
both directions.

The signal for both PV and EV is the same: if the solver chose `grid_export_kw == 0` at step
`t`, the hardware should enforce zero export. If the solver chose to export, the hardware should
follow passively. The post-processor computes this per step from the solved schedule.

---

## What this plan does NOT do

- It does not change the solver model. No new MIP variables or constraints are added.  
  `zero_export_mode` is a post-solve publication decision, not a solver input.
- It does not add a new "zero-export enforcer" constraint to the grid device. The solver already
  models the zero-export limit as `grid.export_limit_kw = 0` when
  `constraints.max_export_kw = 0.0` is set in config. The flag is orthogonal: it tells the
  hardware to enforce zero export _locally_, independent of the solver's grid model.
- It does not implement solar-following / dynamic ramp-rate control. The flag is a step-level
  boolean, not a continuous power reference.

---

## Config design

### Why a dedicated `EvCapabilitiesConfig`

`CapabilitiesConfig` is shared between `BatteryConfig` and `EvConfig`. Batteries do not have a
`zero_export_mode` command topic — they respond to a continuous power setpoint, not a boolean
mode register. Adding `zero_export_mode` to `CapabilitiesConfig` would give battery configs a
field that is meaningless for them and would violate `extra="forbid"`.

A dedicated `EvCapabilitiesConfig` is the correct approach. It retains the existing fields
(`staged_power`, `zero_export_support`) and adds:

```python
zero_export_mode: bool = Field(
    default=False,
    description=(
        "EV charger supports a discrete zero-export mode command. When True, mimirheim publishes "
        "a boolean retained command to outputs.zero_export_mode after each solve. The charger "
        "uses this to modulate its charge rate (or V2H discharge rate) to keep grid export at "
        "zero. Also known as dynamic load balancing."
    ),
)
```

`BatteryConfig` continues to use the existing `CapabilitiesConfig` unchanged.

### New `EvOutputsConfig`

Currently `EvConfig` has no `outputs` sub-object. Add one, parallel to `PvOutputsConfig`:

```python
class EvOutputsConfig(BaseModel):
    """MQTT output topic names for EV charger control commands.

    All fields are optional. A topic is only published if the corresponding
    capability flag is True and the topic is not None.
    """
    model_config = ConfigDict(extra="forbid")

    zero_export_mode: str | None = Field(
        default=None,
        description="MQTT topic for the zero-export mode boolean command.",
    )
```

Add `outputs: EvOutputsConfig = Field(default_factory=EvOutputsConfig)` to `EvConfig`.

---

## `apply_zero_export_flags` logic

The rule for both PV and EV devices is identical:

> At step `t`, `zero_export_mode = True` if `step.grid_export_kw == 0.0`; `False` otherwise.

When the solver assigned zero grid export, it has implicitly decided that zero-export
enforcement is appropriate. When the solver assigned positive export, the inverter/charger
should operate freely and maximise production/discharge.

For EV this covers both the charging side (load-balance charger to absorb excess solar) and the
V2H side (discharge from vehicle to prevent export while covering load).

The function signature already exists in `post_process.py` and is described in the module
docstring as "not yet implemented". This plan implements it.

```python
def apply_zero_export_flags(result: SolveResult, config: MimirheimConfig) -> SolveResult:
    """Set zero_export_mode on PV and EV device setpoints for each schedule step.

    For each step t, a device's zero_export_mode flag is True when the solver
    chose grid_export_kw == 0 at that step, and False when the solver chose
    to export. The flag is only populated when the device's capability is
    enabled (capabilities.zero_export_mode is True); it remains None for
    devices that do not support the command.

    This function does not alter kw values or grid power — it only sets the
    boolean flag that the MQTT publisher reads to send the hardware command.
    """
```

**Implementation note:** Compare using `abs(step.grid_export_kw) < 1e-6` rather than
`== 0.0`. HiGHS's default primal feasibility tolerance is 1e-7, so a variable sitting at
its lower bound of 0 reads back as at most ~1e-7. The 1e-6 epsilon is sufficient to absorb
this numerical noise without masking any genuine economic decision.

A configurable margin larger than this epsilon is deliberately not provided. If the solver
assigned a small but non-trivial export (e.g. 0.05 kW), that was an economic decision. A
post-process margin that activates zero-export mode for such a step would put the hardware
and the solver at odds — the solver optimised for a small export, but the hardware would
suppress it.

Note: `grid.export_limit_kw` is the physical grid connection capacity (set by the DNO/fuse
rating). `constraints.max_export_kw` is an additional policy cap narrower than that physical
limit. Neither is the right knob for zero-export mode — a user may want to permit export in
general while still using the device's built-in zero-export algorithm when the solver
happens to choose zero export. The `zero_export_mode` flag is orthogonal to both.

**Returns:** A new `SolveResult` with updated setpoints. Does not mutate the input.

---

## Call order in `__main__.py`

After each solve, the solve loop currently calls:

```python
result = apply_gain_threshold(result, bundle, config)
```

After this plan:

```python
result = apply_gain_threshold(result, bundle, config)
result = apply_zero_export_flags(result, config)
```

`apply_zero_export_flags` must run _after_ `apply_gain_threshold` because the gain threshold
may replace the schedule with an idle one (zeroing controllable devices). The zero-export flags
must reflect the final published schedule, not the raw solver output.

---

## Publisher changes

`mqtt_publisher.py` section 4 (PV control output topics) covers PV zero-export publishing but
has no EV equivalent. Add a section 5:

```
# 5. EV charger zero-export mode command topic.
for device_name, setpoint in current.devices.items():
    if setpoint.type != "ev_charger":
        continue
    ev_cfg = self._config.ev_chargers.get(device_name)
    if ev_cfg is None:
        continue
    if (
        ev_cfg.capabilities.zero_export_mode
        and ev_cfg.outputs.zero_export_mode is not None
        and setpoint.zero_export_mode is not None
    ):
        self._client.publish(
            ev_cfg.outputs.zero_export_mode,
            "true" if setpoint.zero_export_mode else "false",
            qos=1,
            retain=True,
        )
```

---

## `model_builder.py` changes

EV setpoints currently hard-code `zero_export_mode=None`. Change this to match the PV pattern:

```python
ev_zero_export_mode = False if ev.config.capabilities.zero_export_mode else None
device_setpoints[ev.name] = DeviceSetpoint(
    kw=_eval_net_power(ctx, ev.net_power(t)),
    type="ev_charger",
    zero_export_mode=ev_zero_export_mode,
)
```

This initialises the field so `apply_zero_export_flags` can overwrite it per step. The PV
section in `model_builder.py` already does the same: `zero_export_mode = False if
caps.zero_export_mode else None`.

---

## `ha_discovery.py` changes

Add EV zero-export mode entity discovery, parallel to the existing PV block:

```python
if ev_cfg.capabilities.zero_export_mode and ev_cfg.outputs.zero_export_mode is not None:
    zem_id = f"{device_id}_{name}_zero_export_mode"
    publish(
        f"{discovery_prefix}/binary_sensor/{zem_id}/config",
        {
            "name": f"{device_name} {name} Zero Export Mode",
            "state_topic": ev_cfg.outputs.zero_export_mode,
            "payload_on": "true",
            "payload_off": "false",
            "device_class": "running",
            ...availability fields...
        }
    )
```

---

## Config validation warning

Add a startup warning in `MimirheimConfig` (as a `model_validator`) when all of the following
are true simultaneously:

- `constraints.max_export_kw` is set to `0.0`
- At least one PV, EV, or battery device is configured
- No PV device has `capabilities.zero_export_mode: true`
- No EV device has `capabilities.zero_export_mode: true`

In this configuration the solver enforces zero export in the plan, but if any device uses
`staged_power: true` the hardware will deliver a quantized setpoint that may differ from the
solver's continuous optimum. The resulting real-time power imbalance has no hardware device
capable of compensating it. The warning text should read:

> `constraints.max_export_kw is 0.0 but no device has zero_export_mode enabled. Staged-power
> devices will not compensate for quantization errors and accidental export is possible at
> runtime. Enable zero_export_mode on at least one PV or EV device to allow real-time
> compensation.`

This is a warning, not a validation error — the config is not wrong, and the user may have
external hardware that handles it outside mimirheim. It fires only once at startup via `logging.warning`.

---

## Files to modify

| File | Change |
|---|---|
| `mimirheim/config/schema.py` | Add `EvCapabilitiesConfig`, `EvOutputsConfig`; update `EvConfig` |
| `mimirheim/core/model_builder.py` | Populate `zero_export_mode` on EV setpoints |
| `mimirheim/core/post_process.py` | Implement `apply_zero_export_flags`; update module docstring |
| `mimirheim/io/mqtt_publisher.py` | Add EV zero-export topic publishing |
| `mimirheim/io/ha_discovery.py` | Add EV zero-export mode entity |
| `mimirheim/__main__.py` | Call `apply_zero_export_flags` in solve loop |

## Files to create

None.

---

## Tests first

### `tests/unit/test_post_process.py` (new file)

Write these tests before implementing `apply_zero_export_flags`. Use a minimal helper to build
`SolveResult` and `MimirheimConfig` fixtures. All tests require a real `MimirheimConfig`, so use
`model_validate` with a minimal dict.

- `test_pv_zero_export_mode_true_when_export_zero` — build a two-step result where step 0 has
  `grid_export_kw=0.0` and `grid_import_kw=1.0`, step 1 has `grid_export_kw=2.0`. PV device has
  `capabilities.zero_export_mode=True`. Assert `step[0].devices["pv1"].zero_export_mode is True`
  and `step[1].devices["pv1"].zero_export_mode is False`.

- `test_pv_zero_export_mode_none_when_capability_disabled` — same result but PV has
  `capabilities.zero_export_mode=False`. Assert `zero_export_mode is None` at both steps.

- `test_ev_zero_export_mode_true_when_export_zero` — EV charger with
  `capabilities.zero_export_mode=True`. Step 0: `grid_export_kw=0.0`. Step 1:
  `grid_export_kw=1.5`. Assert `step[0].devices["ev1"].zero_export_mode is True` and
  `step[1].devices["ev1"].zero_export_mode is False`.

- `test_ev_zero_export_mode_none_when_capability_disabled` — EV charger with
  `capabilities.zero_export_mode=False`. Assert `zero_export_mode is None`.

- `test_ev_v2h_zero_export_mode_set` — EV discharging (kw > 0) at step 0 with
  `grid_export_kw=0.0`: assert `zero_export_mode is True`. The flag applies regardless of charge
  direction; V2H discharge to cover load is the same zero-export signal as charging.

- `test_gain_threshold_then_zero_export_flag_order` — verify that
  `apply_zero_export_flags` operates on the idle schedule produced by `apply_gain_threshold`
  and not on the original solve result. A suppressed schedule has all controllable devices at 0
  kW; the EV setpoint's `zero_export_mode` should still reflect the idle schedule's
  `grid_export_kw`, not the original solver output.

- `test_infeasible_result_unchanged` — pass an infeasible `SolveResult` (empty schedule).
  Assert the result is returned unchanged (no iteration over empty list, no crash).

- `test_result_is_not_mutated` — assert that the original `SolveResult` passed to
  `apply_zero_export_flags` is not modified (new object returned).

### `tests/unit/test_config_schema.py` additions

- `test_ev_capabilities_zero_export_mode_defaults_false` — `EvConfig` with no capabilities
  set; assert `capabilities.zero_export_mode is False`.
- `test_ev_capabilities_zero_export_mode_can_be_enabled` — `EvConfig` with
  `capabilities: {zero_export_mode: true}`; assert `capabilities.zero_export_mode is True`.
- `test_ev_outputs_zero_export_mode_topic` — `EvConfig` with
  `outputs: {zero_export_mode: "mimir/ev/zem"}`; assert value stored correctly.
- `test_battery_config_rejects_zero_export_mode_field` — `BatteryConfig` with
  `capabilities: {zero_export_mode: true}` must raise `ValidationError` (`extra="forbid"`).

---

## Acceptance criteria

All tests in `tests/unit/test_post_process.py` pass.

All `test_ev_capabilities_*` and `test_battery_config_rejects_zero_export_mode_field` tests pass.

`uv run pytest` produces no regressions.

`apply_zero_export_flags` is called in the solve loop in `__main__.py` and appears in the module
docstring of `post_process.py` as an implemented transformation (remove the "not yet implemented"
qualifier).

EV setpoints in golden files are unchanged in content (the new `zero_export_mode` field is
`None` for all existing golden file scenario configs where EV capabilities are not set).
Regenerate goldens with `--update-golden` if needed.

---

## Move to done

```bash
mv plans/29_zero_export_flags.md plans/done/
```
