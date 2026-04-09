# Step 13 — build_and_solve() and golden file scenarios

## References

- IMPLEMENTATION_DETAILS §4 (golden file format, `--update-golden` workflow, float tolerance)
- IMPLEMENTATION_DETAILS §5 (`_maybe_dump`, debug dump pattern)
- IMPLEMENTATION_DETAILS §8, subsection "Power balance constraint"

---

## Files to create

- `mimirheim/core/model_builder.py`
- `tests/unit/test_horizon.py`
- `tests/conftest.py` (update with scenario runner and `--update-golden` flag)
- `tests/scenarios/high_price_spread/input.json`
- `tests/scenarios/high_price_spread/config.yaml`
- `tests/scenarios/flat_price/input.json`
- `tests/scenarios/flat_price/config.yaml`
- `tests/scenarios/ev_not_plugged/input.json`
- `tests/scenarios/ev_not_plugged/config.yaml`

Golden files (`golden.json`) are generated — do not create them by hand. See workflow below.

---

## Tests first

### tests/unit/test_horizon.py

- `test_quarter_hourly_horizon_dt` — 96-step horizon produces `dt=0.25`
- `test_hourly_horizon_dt` — 24-step horizon produces `dt=1.0`
- `test_horizon_length_matches_prices` — `len(bundle.horizon_prices)` determines `T`

### Scenario tests

Write the parameterised test in `tests/conftest.py` that:
1. Discovers all `tests/scenarios/*/` directories.
2. For each scenario, loads `input.json` → `SolveBundle` and `config.yaml` → `MimirheimConfig`.
3. Calls `build_and_solve(bundle, config)` → `SolveResult`.
4. If `--update-golden` flag is set, writes `golden.json` with the result.
5. Otherwise, loads `golden.json` and asserts field-by-field with `pytest.approx(abs=1e-4)` for floats.

Write the three scenario input files (structure below). The tests will fail because `golden.json` does not yet exist. That is expected — see workflow.

Run `uv run pytest tests/unit/test_horizon.py` — must pass before proceeding.
Run `uv run pytest tests/scenarios/` — must fail (missing golden files) before proceeding to implementation.

---

## Implementation

### mimirheim/core/model_builder.py

```python
def build_and_solve(bundle: SolveBundle, config: MimirheimConfig) -> SolveResult:
    """Build and solve the MILP optimisation model for the current time horizon."""
```

Steps inside `build_and_solve`:

1. Derive `horizon = len(bundle.horizon_prices)` and `dt = _dt_from_horizon(horizon)`.
2. Create `HiGHSSolverBackend()` and `ModelContext(solver, horizon, dt)`.
3. Instantiate device objects from config:
   - One `Grid(config.grid)`
   - `[Battery(name, cfg) for name, cfg in config.batteries.items()]`
   - `[PvDevice(name, cfg) for name, cfg in config.pv_arrays.items()]`
   - `[EvDevice(name, cfg) for name, cfg in config.ev_chargers.items()]`
   - `[DeferrableLoad(name, cfg) for name, cfg in config.deferrable_loads.items()]`
   - `[StaticLoad(name, cfg) for name, cfg in config.static_loads.items()]`
4. Call `device.add_variables(ctx)` for all devices and grid.
5. Call `device.add_constraints(ctx, inputs)` for all devices, passing the relevant slice of `bundle`. Grid receives `inputs=None`.
6. Add power balance constraint for each `t` in `ctx.T`:
   ```python
   ctx.solver.add_constraint(
       sum(d.net_power(t) for d in all_devices) + grid.net_power(t) == 0
   )
   ```
7. Call `ObjectiveBuilder().build(ctx, all_devices, grid, bundle, config)`.
8. Call `ctx.solver.solve(time_limit_seconds=59.0)`.
9. If status is `"infeasible"`, return `SolveResult(strategy=..., objective_value=0.0, solve_status="infeasible", schedule=[])`.
10. Extract variable values and assemble `SolveResult`.

`_maybe_dump(bundle, result, dump_dir, max_dumps)` is defined in this module but called from the solve loop in `__main__`, not from inside `build_and_solve`.

`build_and_solve` must remain a pure function. No logging, no file I/O, no MQTT. See §4 and boundary rules in AGENTS.md.

### Scenario input files

Each scenario must have a `solve_time_utc` in `input.json` and use the `SolveBundle` structure from step 03.

**high_price_spread** — 96 steps, large difference between peak and off-peak import prices, one battery. Expected: battery charges at off-peak, discharges at peak.

**flat_price** — 96 steps, uniform price throughout. Expected: battery does not cycle (wear cost prevents pointless cycling at equal prices). PV self-consumption optimised if present.

**ev_not_plugged** — 96 steps, one EV charger with `available=False`. Expected: zero EV charge/discharge at all steps, solve succeeds.

### Golden file workflow

After `uv run pytest tests/unit/test_horizon.py` and the full unit test suite pass, run:

```bash
uv run pytest tests/scenarios/ --update-golden
```

This generates `golden.json` for each scenario. Review each file:
- Check that high_price_spread shows charge at cheap steps and discharge at expensive steps.
- Check that flat_price shows near-zero battery throughput.
- Check that ev_not_plugged shows zero EV power at all steps.

Commit all three `golden.json` files. From this point they are locked.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_horizon.py
uv run pytest tests/scenarios/
uv run pytest  # full suite
```

All green (after golden files are generated and committed).

---

## Done

```bash
mv plans/13_model_builder.md plans/done/
```
