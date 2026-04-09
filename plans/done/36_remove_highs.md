# Step 36 — Remove HiGHS and clean up

## References

- `SOLVER_REWRITE.md` §6
- `mimirheim/core/solver_backend.py` — both `HiGHSSolverBackend` and `CBCSolverBackend`
- `pyproject.toml`
- `tests/benchmarks/` — diagnostic helper scripts that import `highspy`

---

## Files to modify

- `pyproject.toml`
- `mimirheim/core/solver_backend.py`
- `tests/unit/test_solver_backend.py`
- `tests/benchmarks/_compare_threads.py` (delete)
- `tests/benchmarks/_mps_reload_test.py` (delete)
- `tests/benchmarks/_model_stats.py` (delete)
- `tests/benchmarks/_gap_sweep.py` (delete if it exists)

---

## Background

This is the final cleanup step. At the end of step 35, the full test suite
passes and all golden files are regenerated. HiGHS is no longer used by any
production or test code except the cross-check tests in `test_solver_backend.py`
and the private `HiGHSSolverBackend` class that powers them.

This step removes HiGHS entirely and deletes the development scripts that
were written during performance investigation. The rewrite is complete when
this step's acceptance criteria are met.

---

## Changes

### 1. Remove `highspy` from pyproject.toml

In `[project] dependencies`, remove the line:

```toml
"highspy>=1.7",
```

Run `uv sync` to update the lockfile and uninstall `highspy`.

Verify that `highspy` is no longer importable:

```bash
uv run python -c "import highspy" 2>&1
```

Expected output: `ModuleNotFoundError: No module named 'highspy'`.

### 2. Remove `HiGHSSolverBackend` from solver_backend.py

Remove:

- The `import highspy` line.
- The `from highspy import HighsModelStatus, HighsVarType` line.
- The entire `HiGHSSolverBackend` class body (from its class definition to
  the end of the file — it is the last class in the module).

Update the module docstring to remove all references to `HiGHSSolverBackend`
and `highspy`. The module now contains only the `SolverBackend` Protocol and
`CBCSolverBackend`.

### 3. Update test_solver_backend.py

Remove:

- The `HiGHSSolverBackend` import.
- All `test_highs_*` test functions.
- Any `test_solver_backend_protocol_satisfied` test that checks
  `HiGHSSolverBackend`. Replace with a check for `CBCSolverBackend`:

  ```python
  def test_cbc_solver_backend_protocol_satisfied() -> None:
      assert isinstance(CBCSolverBackend(), SolverBackend)
  ```

  (This test was already added in step 33, so remove the HiGHS version only.)

Update the module docstring to remove the reference to `HiGHSSolverBackend`.

### 4. Delete benchmark diagnostic scripts

The following scripts were written during the performance investigation phase.
They import `highspy` directly and have no use after the migration:

- `tests/benchmarks/_compare_threads.py` — compared HiGHS thread configurations
- `tests/benchmarks/_mps_reload_test.py` — prototyped the MPS round-trip
- `tests/benchmarks/_model_stats.py` — reported variable/constraint counts

Delete these files. If any additional `_*.py` files in `tests/benchmarks/`
import `highspy`, delete those too. Use:

```bash
grep -l "highspy\|HiGHSSolverBackend" tests/benchmarks/
```

to find them. The test benchmark files `test_benchmarks.py` and the data
generator `_generate_data.py` do not import `highspy` and must be retained.

The MPS export file `tests/benchmarks/prosumer_ev_48h.mps` is kept. It is a
useful reference artefact for manual solver experiments with external tools
(Gurobi, Xpress, etc.).

### 5. Verify no remaining highspy references

```bash
grep -r "highspy\|HiGHSSolverBackend\|HighsModelStatus\|HighsVarType" \
    mimirheim/ tests/ --include="*.py"
```

This must return no results. If any remain, resolve them before continuing.

---

## Final verification procedure

### 1. Full clean reinstall

```bash
uv sync
```

Confirm `highspy` is not in the environment:

```bash
uv run pip list | grep -i highs
```

Must return no output.

### 2. Full test suite

```bash
uv run pytest -q
```

All tests must pass. No errors, no warnings from missing `highspy`.

### 3. Application smoke test

If a config.yaml and broker are available in the development environment:

```bash
uv run python -m mimirheim --config develop.yaml
```

Confirm the service starts, connects to MQTT, and produces a `solve_status:
optimal` output on the first solve cycle.

If no broker is available, run the benchmark instead:

```bash
uv run pytest tests/benchmarks/test_benchmarks.py::test_bench_prosumer_ev_48h -v \
    --benchmark-columns=min,mean,max
```

Expected: `solve_status: optimal`, solve time well under 5 seconds.

---

## Acceptance criteria

- `import highspy` in a fresh `uv run python` session raises `ModuleNotFoundError`.
- `grep -r "highspy\|HiGHSSolverBackend" mimirheim/ tests/ --include="*.py"` returns
  no results.
- `uv run pytest -q` exits 0.
- `mimirheim/core/solver_backend.py` contains `SolverBackend` Protocol and
  `CBCSolverBackend` only.
- The deleted diagnostic scripts are no longer present.
- `tests/benchmarks/prosumer_ev_48h.mps` is still present.
