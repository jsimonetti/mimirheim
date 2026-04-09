# Plan 42 — Formalize mode semantics, activation triggers, and replace legacy fields

## Motivation

The existing `zero_export_mode` boolean fields have been overloaded beyond their
original purpose. They exist in three contexts simultaneously:

1. `BatteryCapabilitiesConfig.zero_export_mode` — hardware capability flag.
2. `BatteryOutputsConfig.zero_export_mode` — MQTT output topic for the command.
3. `DeviceSetpoint.zero_export_mode` — per-step published command (bool | None).

This creates ambiguity: the same name describes a static hardware property, a
topic configuration field, and a runtime per-step command. Plans 43 and 44 cannot
be implemented correctly without resolving this.

This plan replaces all three layers with explicit, purpose-scoped fields. Because
there is no backwards compatibility requirement, old field names are removed completely.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §5 Config schema conventions and validation
- §6 Module boundary rules
- §8 Post-processing and publication pipeline
- §10 Fault resilience

---

## Scope

This plan is specification-first. It does not change solver constraints.
It replaces legacy fields, establishes the mode taxonomy, defines the activation
trigger per device class, and documents the full compatibility matrix.

---

## Mode taxonomy

### 1. Two-layer architecture

For each device:

- **Capability** (static, config): which feedback control modes the hardware
  physically supports, and therefore which modes mimirheim will use. Configuring a
  capability is the operator's declaration of intent. If the hardware supports a
  mode but the operator does not want mimirheim using it, the capability flag must not
  be set.
- **Command** (dynamic, per-step output): what mimirheim publishes to the device this
  step, derived from the capability flags, the current solved schedule, and
  arbitration.

There is no separate "policy" layer. The capability flags are the policy. The
arbitration engine (Plan 43) decides per step whether to assert a closed-loop
command, based on the capability flags and what the solver planned for that step.

### 2. Capability flags per device class

#### Battery

- `zero_exchange: bool` — the battery inverter supports a closed-loop
  zero-exchange firmware mode triggered by a boolean register. When set, the
  battery is a candidate for the arbitration engine each step.

The arbitration engine asserts the closed-loop command when the solved exchange
for the step is near zero. On steps where the solver plans significant charge or
discharge power (e.g. negative-price periods), the near-zero trigger does not
fire and the solver setpoint is published instead.

#### EV charger

Two independent closed-loop capability flags:

- `zero_exchange: bool` — the EVSE supports a closed-loop zero-exchange
  firmware mode. Requires `v2h: true` (bidirectional power is needed to control
  both import and export directions). The arbitration engine asserts this per step
  under the same near-zero-exchange trigger as the battery.
- `loadbalance: bool` — the EVSE firmware can autonomously follow excess PV
  generation in charge-only mode. It measures net grid current and clamps charge
  power to available surplus to prevent export. mimirheim does not send a numeric
  setpoint when this mode is asserted; the EVSE self-regulates.

These flags are orthogonal. An EV charger may support both simultaneously (V2H
hardware that also has a loadbalance charge mode). The arbitration engine decides
which closed-loop mode to assert per step based on operating conditions.

Schema validation must reject:
- `zero_exchange: true` when `v2h: false` (cannot regulate bidirectional
  exchange without discharge capability).

No explicit `policy` field exists on `EvChargerConfig`. Capability flags declare
what is available; the arbitration engine decides when to use each.

#### PV

PV inverters have inverter-side control only; they cannot source grid imbalance.
The existing closed-loop zero-export capability is retained under a renamed flag
(see Field replacement below). There is no new capability flag for PV in this plan.

---

## Activation trigger per device class

The activation trigger is the same for all closed-loop capable devices. The
arbitration engine (Plan 43) fires when:

1. The solved schedule for the step has near-zero grid exchange (import and export
   both below `control.exchange_epsilon_kw`).
2. The device has the relevant capability flag set.
3. Additional per-mode conditions are met (EV plugged in for `zero_exchange`;
   always active for `loadbalance` when plugged in — see below).

There is no explicit operator-configured policy field that enables or disables
individual closed-loop modes. The capability flag IS the activation gate.

### Battery and EV `zero_exchange` activation

The trigger is step-conditional: closed-loop mode is asserted only on steps where
the solved schedule shows near-zero grid exchange. On steps where the solver plans
significant charge or discharge power, the near-zero condition is not met and the
solver setpoint is published instead.

This is intentional: during a negative-price period the solver will plan maximum
charge power. The arbitration layer sees large import on those steps and does not
assert closed-loop mode. The battery charges according to setpoint. On flat-price
periods where the solver has little incentive to move power, the solved exchange
naturally approaches zero and the closed-loop command fires.

### EV `loadbalance` activation

`loadbalance` differs from `zero_exchange` in its trigger: it is
always active when the EV is plugged in and `capabilities.loadbalance` is set.
It is not conditioned on the solved grid exchange for the step. The EVSE firmware
self-regulates continuously. mimirheim does not publish a numeric setpoint in any step
where `loadbalance` is the asserted mode.

When a battery with `zero_exchange` is present and becomes the enforcer
for a step, `loadbalance` is suppressed for that step (the two modes target the
same grid CT measurement; only one controller may be authoritative).

---

## Field replacement (breaking change, no compat layer)

### Battery

Remove from `BatteryCapabilitiesConfig`:
- `zero_export_mode: bool`

Add to `BatteryCapabilitiesConfig`:
- `zero_exchange: bool` — hardware supports a closed-loop zero-exchange
  firmware mode triggered by a boolean register. Default `False`.

Remove from `BatteryOutputsConfig`:
- `zero_export_mode: str | None`

Add to `BatteryOutputsConfig`:
- `exchange_mode: str | None` — MQTT topic to publish the closed-loop enable
  flag. Must be non-None when `capabilities.zero_exchange` is true; a
  model validator must enforce this.

No `policy` field is added to `BatteryConfig`.

### EV charger

Remove from `EvCapabilitiesConfig`:
- `zero_export_mode: bool`

Add to `EvCapabilitiesConfig`:
- `zero_exchange: bool` — hardware supports a closed-loop zero-exchange
  firmware mode. Only meaningful when `v2h: true`. Default `False`.
- `v2h: bool` — hardware supports vehicle-to-home discharge. Default `False`.
- `loadbalance: bool` — EVSE firmware supports charge-only excess-PV following
  mode. Default `False`. May be true simultaneously with `zero_exchange`
  when the hardware supports both modes.

Remove from `EvOutputsConfig`:
- `zero_export_mode: str | None`

Add to `EvOutputsConfig`:
- `exchange_mode: str | None` — MQTT topic for the closed-loop exchange
  enable flag. Must be non-None when `capabilities.zero_exchange` is true.
- `loadbalance_cmd: str | None` — MQTT topic for the loadbalance mode enable
  flag. Must be non-None when `capabilities.loadbalance` is true.

No `policy` field is added to `EvChargerConfig`.

Schema validation must reject:
- `capabilities.zero_exchange: true` when `capabilities.v2h: false`.
- `outputs.exchange_mode: null` when `capabilities.zero_exchange: true`.
- `outputs.loadbalance_cmd: null` when `capabilities.loadbalance: true`.

### PV

Rename `PvCapabilitiesConfig.zero_export_mode` to `PvCapabilitiesConfig.zero_export`.
The `PvOutputsConfig.zero_export_mode` field name is retained unchanged — it was always correct for PV.

The PV behavior does not change semantically; only the field names align with the
new naming convention.

### DeviceSetpoint

Remove `DeviceSetpoint.zero_export_mode: bool | None`.

Replace with `DeviceSetpoint.zero_exchange_active: bool | None`:
- `None`: device class does not support any closed-loop mode.
- `True`: closed-loop mode command is asserted for this step.
- `False`: closed-loop mode is not asserted; numeric setpoint is published.

For EV chargers with `loadbalance` capability, a second field is needed to
distinguish which closed-loop mode is asserted:
- `DeviceSetpoint.loadbalance_active: bool | None` — `None` if device has no
  loadbalance capability; `True` if loadbalance is the asserted mode this step.

Update the docstring on `DeviceSetpoint` to accurately describe all device
classes (battery, EV, PV) rather than the incorrect "for PV devices only"
that exists today.

---

## Mode compatibility matrix (normative)

The matrix is defined in terms of capability flags, not a policy enum. Each row
describes a valid hardware configuration and its runtime behavior.

| Battery `zero_exchange` | EV `zero_exchange` | EV `loadbalance` | Valid at runtime? | Notes |
|------------------------|-------------------|-----------------|-------------------|-------|
| false                         | false                    | false           | Yes               | All solver setpoints; baseline behavior |
| true                          | false                    | false           | Yes               | Battery is sole closed-loop candidate |
| false                         | true                     | false           | Yes               | EV is sole closed-loop candidate; requires V2H |
| true                          | true                     | false           | Arbitrated        | Both are candidates; Plan 43 selects one per step |
| false                         | false                    | true            | Yes               | EV loadbalance always-on when plugged in; battery follows setpoint |
| true                          | false                    | true            | Arbitrated        | Battery closed-loop and EV loadbalance conflict per step; Plan 43 resolves |
| false                         | true                     | true            | Arbitrated        | EV supports both closed-loop exchange and loadbalance; Plan 43 selects per step |
| true                          | true                     | true            | Arbitrated        | All three candidates; Plan 43 selects one per step |
| any                           | any                      | any (PV `zero_export: true`) | Arbitrated | PV curtailment may interact with battery/EV enforcement; Plan 43 resolves |

Rows marked "Arbitrated" are valid configurations. The arbitration engine resolves
them deterministically at runtime. No schema-time rejection applies to these rows.

Schema-time rejections are listed in the Field replacement section above.

---

## Files to create/edit

### Tests (write first — all must fail before implementation)

1. `tests/unit/test_config_schema.py`
   - `test_battery_zero_exchange_false_by_default` — accept with no flag set.
   - `test_battery_zero_exchange_requires_exchange_mode_topic` — reject
     when `zero_exchange: true` and `outputs.exchange_mode` is null.
   - `test_ev_zero_exchange_requires_v2h` — reject when
     `capabilities.zero_exchange: true` and `capabilities.v2h: false`.
   - `test_ev_loadbalance_and_zero_exchange_both_true_accepted` — accept;
     they are orthogonal capabilities.
   - `test_ev_loadbalance_requires_loadbalance_cmd_topic` — reject when
     `capabilities.loadbalance: true` and `outputs.loadbalance_cmd` is null.
   - `test_legacy_zero_export_mode_field_rejected_battery` — confirm `extra="forbid"`
     rejects the old field name.
   - `test_legacy_zero_export_mode_field_rejected_ev` — same for EV.
   - `test_pv_renamed_zero_export_accepted` — accept new field name.
   - `test_pv_legacy_zero_export_mode_field_rejected` — reject old field name.

2. `tests/unit/test_mqtt_publisher.py`
   - `test_zero_exchange_active_true_publishes_exchange_mode_battery`
   - `test_zero_exchange_active_false_does_not_publish_exchange_mode_battery`
   - `test_loadbalance_active_true_publishes_loadbalance_cmd_ev`
   - `test_pv_zero_export_mode_published_when_zero_exchange_active`

### Implementation

3. `mimirheim/config/schema.py`
   - Replace `BatteryCapabilitiesConfig.zero_export_mode` with `zero_exchange`.
   - Replace `BatteryOutputsConfig.zero_export_mode` with `exchange_mode`.
   - Add model validator to `BatteryConfig` enforcing non-null `exchange_mode`
     when `capabilities.zero_exchange` is true.
   - Replace `EvCapabilitiesConfig.zero_export_mode` with `zero_exchange`,
     add `v2h` and `loadbalance` capability flags.
   - Replace `EvOutputsConfig.zero_export_mode` with `exchange_mode`, add
     `loadbalance_cmd`.
   - Add model validators to `EvChargerConfig` for the capability/output
     consistency rules.
   - Rename `PvCapabilitiesConfig.zero_export_mode` to `zero_export`.
   - `PvOutputsConfig.zero_export_mode` is retained unchanged.
   - No `policy` field is added to any device config.

4. `mimirheim/core/bundle.py`
   - Replace `DeviceSetpoint.zero_export_mode: bool | None` with
     `DeviceSetpoint.zero_exchange_active: bool | None`.
   - Add `DeviceSetpoint.loadbalance_active: bool | None` for EV chargers with
     loadbalance capability.
   - Update docstring to accurately cover battery, EV, and PV.

5. `mimirheim/core/model_builder.py`
   - Update all sites that initialize `zero_export_mode` on `DeviceSetpoint` to
     use `zero_exchange_active`. The initial value for all devices is `None`
     (capability absent) or `False` (capability present, not yet asserted).
     Only the arbitration layer (Plan 43) sets `True`.

6. `mimirheim/core/post_process.py`
   - Remove all references to `zero_export_mode`. The field no longer exists.
   - Update `apply_zero_export_flags` to reference `zero_exchange_active` so the
     code compiles with the new field names until Plan 43 deletes it entirely.

7. `mimirheim/io/mqtt_publisher.py`
   - Update `publish_result` to publish to `exchange_mode` and
     `loadbalance_cmd` (EV) and `zero_export_mode` (PV) using `zero_exchange_active`
     and `loadbalance_active` from `DeviceSetpoint`.

8. `mimirheim/config/example.yaml`
   - Replace all `zero_export_mode:` fields with renamed equivalents.
   - Document `zero_exchange`, `v2h`, and `loadbalance` capability flags
     under the EV section.
   - Describe activation behavior in plain language (no policy field to document).

9. `README.md` and `IMPLEMENTATION_DETAILS.md`
   - Update all references to `zero_export_mode`.
   - Add a "Control authority" section describing the two-layer model (capability
     + command) and the activation trigger.

10. `dev/mimirheim.yaml`
    - Migrate to renamed fields.

---

## Acceptance criteria

- All old `zero_export_mode` field names have been removed from schema, bundle,
  publisher, config files, and documentation.
- No `policy` field exists on any device config model.
- `DeviceSetpoint.zero_exchange_active` and `DeviceSetpoint.loadbalance_active`
  exist with correct type annotations and docstrings.
- Schema model validators enforce capability/output consistency rules.
- All existing tests still pass after field renaming (no behavior change in this plan).
- New tests confirm rejection of old field names and enforcement of consistency rules.
- `apply_zero_export_flags` in `post_process.py` references only `zero_exchange_active`
  (full deletion is Plan 43's responsibility).

---

## Notes for follow-up plans

This plan is a hard prerequisite for Plans 43 and 44. No behavioral changes to
selection logic, arbitration, or solver constraints are in scope here.
