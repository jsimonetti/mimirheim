# Plan 39 — PV control mode simplification

## Motivation

`PvConfig` currently supports three control modes for the inverter:

1. **Continuous** (`capabilities.power_limit: true`): the solver dispatches a
   continuous kW setpoint in `[0, forecast[t]]`.
2. **Binary on/off** (`capabilities.on_off: true`): the inverter is either at full
   forecast output or fully off; no intermediate values.
3. **Discrete stages** (`production_stages: [...]`): the inverter accepts a fixed
   set of kW levels, including 0. Mutually exclusive with modes 1 and 2 today.

Additionally, the schema currently allows modes 1 and 2 to be enabled
simultaneously. The device code in `pv.py` has a dedicated `caps.power_limit and
caps.on_off` branch that handles this combination with a Big-M coupling:
`pv_kw[t] <= forecast[t] * pv_on[t]`.

This combined mode does not correspond to a real hardware configuration. The
two registers are not driven simultaneously:

- A continuous inverter (SMA, Kostal, Fronius) accepts a power limit setpoint.
  Sending 0 achieves off. There is no separate on/off register; the two "modes"
  are the same register at different values.
- A binary on/off inverter (simple relay or firmware toggle) cannot produce
  intermediate power; there is no setpoint register.
- A discrete-stage inverter (Enphase ACB, some Sungrow models) such as the Enphase
  IQ Combiner supports a fixed enumeration of output levels including 0 and 100%.
  This is `production_stages` in config, which is already correctly handled.

Allowing `on_off + power_limit` together silently creates a model that does not
match any hardware: the binary on/off register is irrelevant when you already have
a continuous setpoint that can go to 0. The combined branch adds code complexity,
an extra binary variable per step per array, and a Big-M constraint, for no
practical benefit.

---

## Design

### Schema changes

`PvCapabilitiesConfig` gains a `model_validator` that enforces exactly one active
mode:

- `power_limit: true` and `on_off: true` simultaneously → `ValidationError`
- `production_stages` and any capability → `ValidationError` (already enforced
  on `PvConfig` elsewhere; confirm and document)

The error message must name the conflicting fields explicitly.

### Solver changes in `pv.py`

Remove the `caps.power_limit and caps.on_off` conditional branch in
`add_constraints`. The remaining branches are:

1. `stages is not None` → staged mode (unchanged).
2. `caps.power_limit` → continuous variable `pv_kw[t]` in `[0, forecast[t]]`
   (unchanged).
3. `caps.on_off` → binary `pv_on[t]`; `net_power[t] = forecast[t] * pv_on[t]`
   (unchanged, now its own branch, not a fallback).
4. Neither → fixed forecast (unchanged).

No new variables. No new constraints. This is a code deletion.

### `ha_discovery.py`

`publish_discovery` currently publishes one entry for `power_limit_kw` and one
for `zero_export_mode`. The combined mode has no dedicated discovery logic. Verify
that the discovery section handles the simplified modes correctly and update any
comments that mention the combined mode.

### Documentation

- `mimirheim/config/example.yaml` — update PV section comment to state that
  `power_limit` and `on_off` are mutually exclusive; replace the combined-mode
  comment with a clear per-mode description.
- `README.md` and `IMPLEMENTATION_DETAILS.md` — update any references to the
  combined mode.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §3 Device constraint API
- §5 Config schema conventions and validation
- §6 Module boundary rules

---

## Files to create/edit

- `mimirheim/config/schema.py` — add `model_validator` to `PvCapabilitiesConfig`
- `mimirheim/devices/pv.py` — remove the combined `power_limit and on_off` branch
- `mimirheim/config/example.yaml` — update PV section comments
- `README.md` — update if it documents the combined mode
- `IMPLEMENTATION_DETAILS.md` — update if it documents the combined mode
- `tests/unit/test_config_schema.py` — new rejection test; update any fixture
  that uses both flags
- `tests/unit/test_pv_constraints.py` — remove any test that exercises the
  combined branch; add test confirming the validation error

---

## Tests to write first

All new tests must fail before the implementation is written.

1. `test_pv_power_limit_and_on_off_together_raises` — `PvCapabilitiesConfig`
   with `power_limit=True, on_off=True` raises `ValidationError`.

2. `test_pv_power_limit_alone_valid` — accepted without error.

3. `test_pv_on_off_alone_valid` — accepted without error.

4. `test_pv_power_limit_mode_adds_continuous_variable` — after `add_constraints`,
   `_pv_kw` is populated, `_pv_on` is empty.

5. `test_pv_on_off_mode_adds_binary_variable` — after `add_constraints`,
   `_pv_on` is populated, `_pv_kw` is empty.

6. `test_pv_neither_capability_is_fixed_forecast` — no variables added;
   `net_power` values equal the clipped forecast.

---

## Acceptance criteria

- All 6 new tests pass.
- `uv run pytest` — no regressions.
- No combined-branch code remains in `pv.py`.
- `PvCapabilitiesConfig` rejects `power_limit=True, on_off=True` with a clear
  `ValidationError`.

---

## Sequencing

This plan has no dependency on Plan 38. It can be implemented before, after, or
in parallel with Plan 38 parts. It must be completed before any future plan that
extends PV control (e.g. staged-mode HA autodiscovery or PV output enrichment).
