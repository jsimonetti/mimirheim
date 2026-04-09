# Step 10 — Deferrable load device

## References

- IMPLEMENTATION_DETAILS §8 (Device method contract)
- README.md (deferrable load MQTT input topics, window semantics)

---

## Files to create

- `mimirheim/devices/deferrable_load.py`
- `tests/unit/test_deferrable_load_constraints.py`

---

## Tests first

Create `tests/unit/test_deferrable_load_constraints.py`. Use `T=8`, `dt=0.25`. Tests must fail before any implementation exists.

- `test_deferrable_load_runs_exactly_once` — window covers the full 8-step horizon; assert `Σ start[t] == 1` in the solution
- `test_deferrable_load_completes_within_window` — `duration_steps=2`; assert the load finishes before `window_latest` step (no running steps outside the window)
- `test_deferrable_load_power_correct_when_running` — `power_kw=1.5`; assert `net_power(t) == -1.5` at steps where running, `0.0` elsewhere
- `test_deferrable_load_no_window_skips_all_constraints` — `DeferrableWindow` is absent (None) for this device; assert no variables added and `net_power(t)` returns 0 for all t
- `test_deferrable_load_runs_at_cheapest_time` — window covers full horizon; low price at steps 4-5; incentive to minimise cost; assert load is scheduled at steps 4-5 (duration_steps=2)
- `test_deferrable_load_net_power_negative` — load consumes power; net_power is negative (sign convention)

Run `uv run pytest tests/unit/test_deferrable_load_constraints.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/devices/deferrable_load.py` — a load that must run for exactly `config.duration_steps` consecutive time steps, somewhere within a time window.

### Variables (declared in `add_variables`)

- `start[t]` — binary variable; 1 if the load starts at step t, 0 otherwise

`start[t]` is the only decision variable. All other quantities are derived from it. Whether the load is "running" at step t is the sum of recent starts within a duration window.

This is a classic "lot sizing" or "fixed-duration scheduling" formulation. The binary nature of `start[t]` is the key insight: the solver picks exactly one start time, and the running status follows deterministically.

### Constraints (added in `add_constraints`, receives `DeferrableWindow | None`)

If the window is None, add no variables and no constraints. `net_power(t)` must return 0 in this case.

If a window is provided:

1. Convert `window_earliest` and `window_latest` to step indices using `solve_time_utc` and `ctx.dt`.

2. **Exactly one start within the window:**
   ```
   Σ_{t in window} start[t] = 1
   ```
   `start[t] = 0` for all t outside the window (set upper bound to 0 on those variables, or simply do not create them).

3. **Running status:** At each step t, define the running indicator as:
   ```
   running[t] = Σ_{k = max(0, t - duration + 1)}^{t} start[k]
   ```
   This is not a separate variable — it is a linear expression in the `start` variables. The load is running at step t if any start within the past `duration_steps` steps is active.

4. `net_power(t)` returns `-config.power_kw × running[t]`.

Comment `start[t]` at the required depth: explain what a "binary variable" means to a developer unfamiliar with MIP, why the sum-to-one constraint enforces exactly one run, and what would happen if the constraint were relaxed (the solver might start the load multiple times or not at all).

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_deferrable_load_constraints.py
```

All tests green.

---

## Done

```bash
mv plans/10_device_deferrable_load.md plans/done/
```
