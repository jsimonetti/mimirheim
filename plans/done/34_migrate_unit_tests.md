# Step 34 — Migrate unit test fixtures and model_builder to CBCSolverBackend

## References

- `mimirheim/core/model_builder.py` — imports and instantiates `HiGHSSolverBackend`
- All `tests/unit/test_*.py` files that import `HiGHSSolverBackend`
- `SOLVER_REWRITE.md` §6

---

## Files to modify

### Production code (1 line each)

- `mimirheim/core/model_builder.py`

### Unit test files (14 files)

- `tests/unit/test_battery_constraints.py`
- `tests/unit/test_building_thermal_model.py`
- `tests/unit/test_combi_heat_pump_constraints.py`
- `tests/unit/test_deferrable_load_constraints.py`
- `tests/unit/test_ev_constraints.py`
- `tests/unit/test_grid_constraints.py`
- `tests/unit/test_hybrid_inverter_constraints.py`
- `tests/unit/test_model_context.py`
- `tests/unit/test_objective_builder.py`
- `tests/unit/test_pv_constraints.py`
- `tests/unit/test_solver_backend.py` (already updated in step 33)
- `tests/unit/test_space_heating_constraints.py`
- `tests/unit/test_static_load_constraints.py`
- `tests/unit/test_thermal_boiler_constraints.py`

---

## Background

After step 33, `CBCSolverBackend` exists and its unit tests pass. Every
other unit test still constructs `HiGHSSolverBackend()` in its fixtures.
After this step, all fixtures use `CBCSolverBackend`. The test logic is
unchanged — only the backend that runs inside each test changes.

This step also updates `model_builder.py` so that `build_and_solve()` uses
CBC. After this change, the scenario golden-file tests will likely fail
because CBC may produce different (but equally or more optimal) schedules
than HiGHS. That is expected and will be addressed in step 35.

---

## Changes

### In every affected test file

Locate the line:

```python
from mimirheim.core.solver_backend import HiGHSSolverBackend
```

Replace with:

```python
from mimirheim.core.solver_backend import CBCSolverBackend
```

Locate every occurrence of `HiGHSSolverBackend()` in that file and replace
with `CBCSolverBackend()`.

The change is purely mechanical. Do not alter test names, assertions, helper
functions, or any other code. A useful shell command for verification:

```bash
grep -r "HiGHSSolverBackend" tests/unit/
```

After all replacements, this command must return no results (except in
`test_solver_backend.py`, where the HiGHS tests are deliberately kept for
now — see note below).

### In `mimirheim/core/model_builder.py`

Locate:

```python
from mimirheim.core.solver_backend import HiGHSSolverBackend
```

Replace with:

```python
from mimirheim.core.solver_backend import CBCSolverBackend
```

Locate:

```python
solver = HiGHSSolverBackend()
```

Replace with:

```python
solver = CBCSolverBackend()
```

---

## Note on test_solver_backend.py

`test_solver_backend.py` was updated in step 33 to add `test_cbc_*` tests.
The existing `test_highs_*` tests and the `HiGHSSolverBackend` import in
that file are intentionally left in place until step 36. They serve as a
live cross-check that `HiGHSSolverBackend` still works during the migration
period. In step 36 they will be removed together with the `HiGHSSolverBackend`
class itself.

---

## Verification procedure

### 1. Run unit tests only (no scenario tests)

```bash
uv run pytest tests/unit/ -q
```

All unit tests must pass. Test behaviour must be identical to the pre-
migration baseline — the same constraints are enforced, the same objective
values are achieved, the same infeasible problems are rejected. Only the
solver executing them has changed.

### 2. Run scenario tests to observe expected failures

```bash
uv run pytest tests/scenarios/ -q
```

Expect failures on golden file assertions. This is correct — CBC finds valid
but potentially different optimal schedules. The test runner output will show
which scenarios have changed. Record these diffs as input for step 35.

If any scenario test fails with `solve_status: infeasible` or a Python
exception, that is a genuine error and must be diagnosed before continuing.
A constraint formulation error would surface here.

### 3. Run benchmark tests

```bash
uv run pytest tests/benchmarks/ -q
```

Expect the benchmarks to run substantially faster than under HiGHS. Record
the new timings. If any benchmark fails with `solve_status: infeasible`,
diagnose the failure — the benchmark scenarios exercise the full device set
and any regression in constraint logic will appear here.

---

## Critical checks before signing off this step

- `grep -r "HiGHSSolverBackend" mimirheim/` returns only
  `mimirheim/core/solver_backend.py` (the class definition, which still exists
  until step 36).
- `grep -r "HiGHSSolverBackend" tests/unit/` returns only
  `tests/unit/test_solver_backend.py` (the cross-check tests kept until step 36).
- `uv run pytest tests/unit/ -q` exits 0.

---

## Acceptance criteria

- All 14 unit test fixture files import `CBCSolverBackend` and instantiate
  it in their `ModelContext(solver=CBCSolverBackend(), ...)` fixtures.
- `mimirheim/core/model_builder.py` instantiates `CBCSolverBackend`.
- All unit tests pass with CBC.
- Scenario golden-file failures are observed but not yet fixed (they are
  addressed in step 35).
