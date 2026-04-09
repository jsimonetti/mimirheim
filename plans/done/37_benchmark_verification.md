# Step 37 — Benchmark verification and performance baseline

## References

- `tests/benchmarks/test_benchmarks.py`
- `tests/benchmarks/minimal_home_24h/`, `prosumer_ev_48h/`, `worst_case_7d/`
- `SOLVER_REWRITE.md` §1 (original HiGHS measurements)

---

## Files to modify

- `tests/benchmarks/test_benchmarks.py` (potentially — if worst_case_7d
  now consistently solves to optimality rather than hitting the time limit,
  relax the `"feasible"` fallback in the assertion)

---

## Background

After completing steps 32–36, the solver backend is fully migrated to CBC.
This step runs the benchmark scenarios, records the new timing baseline,
and confirms that the worst-case 7-day scenario completes within the 59-
second time limit. The benchmark data files (config.yaml, input.json) do
not change.

---

## Benchmark scenarios

| Scenario | Steps | Horizon | Expected status |
|---|---|---|---|
| `minimal_home_24h` | 96 | 24 h | `"optimal"` |
| `prosumer_ev_48h` | 192 | 48 h | `"optimal"` |
| `worst_case_7d` | 672 | 7 days | `"optimal"` or `"feasible"` |

The `worst_case_7d` benchmark has 2 hybrid inverters, 3 standalone batteries,
4 PV arrays, 2 EV chargers, a combi heat pump, a space heating HP, a thermal
boiler, and 3 deferrable loads. It was consistently killed under HiGHS. The
expectation with CBC is that it completes well within the time limit, but the
assertion retains `"feasible"` as an acceptable result in case the 7-day
model is near the edge of the 59-second budget.

---

## Procedure

### 1. Run all three benchmarks

```bash
uv run pytest tests/benchmarks/test_benchmarks.py -v \
    --benchmark-columns=min,mean,max
```

Record the wall-clock time for each benchmark. Compare against the HiGHS
baseline from `SOLVER_REWRITE.md` §1.

### 2. Evaluate worst_case_7d completion

If `worst_case_7d` completes with `solve_status: "optimal"` consistently:

Update the assertion in `test_bench_worst_case_7d` from:

```python
assert result.solve_status in ("optimal", "feasible")
```

to:

```python
assert result.solve_status == "optimal"
```

This tightens the test, making any regression immediately visible.

If `worst_case_7d` still times out (returns `"feasible"` after 59 seconds):

Leave the assertion as-is. Record the incumbent quality (objective value) and
note whether the gap is acceptable. Consider raising the time limit for this
specific benchmark or implementing the `mip_rel_gap` equivalent for python-mip:

```python
self._m.max_mip_gap = 0.001  # accept 0.1 % gap
```

This can be added to `CBCSolverBackend.__init__` alongside `verbose=0`. The
rationale is identical to the HiGHS `mip_rel_gap` setting: a 0.1 % cost error
on a real schedule is under 5 cents per day and imperceptible in practice.

### 3. Run the full test suite one final time

```bash
uv run pytest -q
```

This is the acceptance gate. All tests — unit, scenario, benchmark — must pass.

### 4. Record the new performance baseline

Update `SOLVER_REWRITE.md` §1 (the timing table) to add a CBC row with the
measured values:

| Method | Build time | Solve time | Total |
|---|---|---|---|
| ... (existing rows) ... | | | |
| `build_and_solve()` via CBCSolverBackend (prosumer_ev_48h) | TBD | TBD | TBD |
| `build_and_solve()` via CBCSolverBackend (worst_case_7d) | TBD | TBD | TBD |

### 5. Commit

```bash
git add -A
git commit -m "perf: migrate to CBC (python-mip); rewrite complete

- Replace HiGHSSolverBackend with CBCSolverBackend throughout
- Remove highspy dependency; add mip>=1.14
- Regenerate golden files for CBC output
- Benchmark: prosumer_ev_48h solves in Xs (was 21s under HiGHS)
- Benchmark: worst_case_7d now [completes / times out with feasible]

See SOLVER_REWRITE.md for full rationale and measurement methodology."
```

Fill in the actual timing in the commit message.

---

## If worst_case_7d is slower than expected

The main factors affecting CBC performance on this problem:

1. **Number of binary variables per step.** The worst_case_7d scenario
   includes 5+ binary-per-step decisions (boiler, combi HP, space heating HP,
   battery modes, EV modes). More binaries = exponentially harder MILP.

2. **Coupling across time steps.** Thermal mass creates coupling across all
   672 steps. CBC's Gomory cuts are effective for short-range coupling; very
   long chains may still be expensive.

3. **`max_mip_gap` setting.** Applying `self._m.max_mip_gap = 0.001` to
   `CBCSolverBackend.__init__` allows CBC to stop when within 0.1 % of the
   optimum. This is already cost-negligible and is the recommended first
   tuning step if timing is a concern. Add it in this step rather than waiting
   for a user complaint.

4. **`max_nodes` or `max_seconds` per scenario.** For the benchmark test only
   (not production), the `pedantic(rounds=1, iterations=1)` call already
   enforces a single solve. The `time_limit_seconds` in production defaults to
   59 s. The benchmark test does not pass a custom time limit, so it uses the
   59 s default. If worst_case_7d takes longer than 59 s and needs a relaxed
   limit for benchmarking, pass an explicit `time_limit_seconds=300` in the
   test's `build_and_solve` call.

---

## Acceptance criteria

- All three benchmarks run without exceptions or crashes.
- `minimal_home_24h` and `prosumer_ev_48h` return `solve_status: "optimal"`.
- `worst_case_7d` returns `solve_status` in `("optimal", "feasible")` within
  the time limit — or the assertion has been tightened to `"optimal"` if it
  consistently solves to optimality.
- The timing for `prosumer_ev_48h` is substantially lower than the 21 s HiGHS
  baseline (target: under 5 s total).
- `uv run pytest -q` exits 0.
- The new timing baseline is recorded in `SOLVER_REWRITE.md`.
- The rewrite is committed.
