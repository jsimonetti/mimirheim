# Plan 44 — Solver accuracy documentation and exchange-shaping secondary term

## Motivation

Plans 42 and 43 establish the policy modes and the post-process arbitration layer.
Plan 44 has two narrow deliverables:

1. **No-suppression documentation.** The solver is currently unaware of which
   steps will have a device in closed-loop mode. That is correct behavior, not a
   gap. The reason needs to be documented explicitly to prevent future regressions
   where someone suppresses closed-loop device variables in the MILP.

2. **Optional exchange-shaping secondary term.** Under symmetric net-zero tariffs
   (NoM), `minimize_cost` already produces near-zero exchange as a natural
   consequence. However, when energy prices are flat or nearly flat, the solver is
   indifferent among solutions with equivalent cost but different exchange
   magnitudes. Adding a small secondary term breaks that indifference in favor of
   lower exchange without distorting the economic objective.

A dedicated `zero_exchange` strategy was considered but rejected. The NoM case
is adequately served by `minimize_cost` with a nonzero `exchange_shaping_weight`
combined with a well-tuned `wear_cost_eur_per_kwh`. Adding a strategy adds
naming surface and decision load for operators without a meaningful behavioral
difference.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §3 Device constraints API
- §4 ObjectiveBuilder behavior
- §8 build_and_solve lifecycle

---

## Prerequisites

Plans 42 and 43 must be complete. `zero_exchange_active` must exist on
`DeviceSetpoint`. `assign_control_authority` must be in the pipeline.

---

## Design

### 1. No solver variable changes for closed-loop steps

When a battery or EV will have `zero_exchange_active=True` on a step, the solver
variable for that device is NOT suppressed or fixed. This is intentional:

- The device will be charging or discharging autonomously. If the solver does not
  model that behavior, the SOC trajectory across the horizon becomes incorrect.
- Zeroing the solver variable would cause wrong SOC estimates on adjacent steps,
  leading to incorrect dispatch on the steps immediately before and after.
- The solver's planned setpoint represents the best prediction of what the hardware
  will actually do. The post-process layer overrides the published command to the
  hardware with the closed-loop enable flag. The solver plan is advisory; the
  hardware enforces near-zero exchange.

The deliverable for this decision is a comment block in `model_builder.py` and
a section in `IMPLEMENTATION_DETAILS.md`. No code changes to the MILP are required.

### 2. Optional exchange-shaping secondary term

Add an optional secondary objective term:

    min lambda_exchange * sum_t (import_t + export_t)

Properties:

- The weight `exchange_shaping_weight` must be chosen so that this term cannot
  dominate the primary cost term. Default `0.0` (disabled). A value of `1e-4`
  EUR/kWh is a reasonable starting point (equivalent to 0.1 EUR/MWh, orders of
  magnitude below typical retail prices).
- Active only when `objectives.exchange_shaping_weight > 0`. Works with any
  existing strategy (`minimize_cost`, `minimize_consumption`, `balanced`).
- Breaks solver indifference in favor of lower exchange when prices are flat.
- Sums the same `grid_import[t]` and `grid_export[t]` variables already used by
  the primary objective. No new variables needed.

### 3. SOC continuity documentation

Document in `IMPLEMENTATION_DETAILS.md` that:

- The SOC state variable is continuous across all steps, including steps where
  a device is in closed-loop mode.
- The solver's planned SOC trajectory on closed-loop steps reflects its best
  prediction of hardware behavior.
- If the hardware does not precisely track the solved setpoint (expected — firmware
  PID loops are not perfect), the next solve cycle self-corrects using the fresh
  SOC reading from MQTT.

This is existing behavior. The documentation is the deliverable.

---

## Files to create/edit

### Tests (write first — all must fail before implementation)

1. `tests/unit/test_objective_builder.py`
   - `test_exchange_shaping_weight_zero_excludes_secondary_term`
   - `test_exchange_shaping_weight_nonzero_adds_secondary_term`

2. `tests/unit/test_config_schema.py`
   - `test_exchange_shaping_weight_defaults_to_zero`
   - `test_exchange_shaping_weight_rejected_when_negative`

### Implementation

3. `mimirheim/config/schema.py`
   - Add `exchange_shaping_weight: float = 0.0` to `ObjectivesConfig`.

4. `mimirheim/core/objective.py`
   - Add exchange-shaping secondary term, activated when
     `objectives.exchange_shaping_weight > 0`.

5. `mimirheim/core/model_builder.py`
   - Add comment block at the schedule extraction site explaining why closed-loop
     steps are not suppressed in the MILP, with a cross-reference to
     `IMPLEMENTATION_DETAILS.md`.

6. `mimirheim/config/example.yaml`
   - Document `exchange_shaping_weight` with default and an annotated example.

7. `IMPLEMENTATION_DETAILS.md`
   - Add subsection: "Closed-loop modes and solver accuracy".
   - Document the no-suppression decision and SOC continuity guarantee.

8. `README.md`
   - Add `exchange_shaping_weight` to the objectives documentation section.

---

## Acceptance criteria

- `exchange_shaping_weight` field exists on `ObjectivesConfig` with default 0.0.
- Setting `exchange_shaping_weight > 0` adds the secondary term to the objective;
  0.0 leaves existing objective behavior completely unchanged.
- Comment in `model_builder.py` explains the no-suppression decision.
- Documentation in `IMPLEMENTATION_DETAILS.md` covers SOC continuity for
  closed-loop steps.
- All new unit tests pass and all existing tests remain green.
