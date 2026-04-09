# Step 35 — Regenerate golden files with CBC

## References

- `IMPLEMENTATION_DETAILS.md` §4, subsection "Golden file discipline"
- `tests/scenarios/` — golden file directories
- `SOLVER_REWRITE.md` §8, risk R3

---

## Files to modify

- `tests/scenarios/high_price_spread/golden.json`
- `tests/scenarios/flat_price/golden.json`
- `tests/scenarios/ev_not_plugged/golden.json`

---

## Background

Golden files record the complete expected solver output for each scenario.
They were generated under HiGHS. CBC is now the default backend.

CBC and HiGHS produce equally correct optimal solutions, but when multiple
schedules tie at the same objective value, they differ in which schedule
they return. An example: if charging at step 2 or step 3 costs the same
amount, HiGHS may choose step 2 and CBC may choose step 3. Both are optimal;
only the per-step values differ. The golden file comparisons (which use
`pytest.approx(abs=1e-4)`) will therefore fail on any step where the tie
has been broken differently.

Regenerating the golden files makes them accurate for CBC and locks the new
values as the baseline. Every change to the solver output from this point
forward requires a deliberate `--update-golden` re-run and a code review of
the diff.

---

## Procedure

### 1. Confirm all unit tests still pass before touching golden files

```bash
uv run pytest tests/unit/ -q
```

If any unit test fails, stop and resolve it before proceeding. A golden file
regenerated while a constraint is broken records a wrong baseline.

### 2. Observe the current golden file failures

```bash
uv run pytest tests/scenarios/ -v --tb=short
```

Read the output carefully. For each failing test, check:

- Does it fail because of differing schedule step values (the expected CBC/HiGHS
  tie-breaking difference)? This is normal.
- Does it fail with `AssertionError: solve_status == "infeasible"`? This indicates
  a real constraint error introduced in the migration. Stop here and diagnose.
- Does it fail with a Python exception? This also indicates a migration error.

Only proceed to regeneration if all failures are value diffs, not status or
exception failures.

### 3. Regenerate

```bash
uv run pytest tests/scenarios/ --update-golden -v
```

This overwrites each `golden.json` with the current CBC output. The flag
`--update-golden` is handled by the scenario test runner in
`tests/scenarios/test_scenarios.py`.

### 4. Review each diff

Run:

```bash
git diff tests/scenarios/
```

For each changed golden file, verify:

**a. solve_status is "optimal"**

Every scenario must still solve to optimality. If any scenario now shows
`"feasible"`, CBC hit the time limit — the model or the time limit may need
adjustment.

**b. Objective value is equal or better**

CBC must not produce a worse (higher-cost) objective than HiGHS. Compare the
`"objective_value"` field in the old and new golden files. A small improvement
(CBC finds a marginally better solution) is acceptable and expected. A
degradation would indicate a constraint regression.

Reference: if the old objective value was $X$ and the new is $Y$, then:
- $Y \le X$ (lower cost = better for minimisation): acceptable.
- $Y > X$ by more than 0.1 %: indicates a regression. Investigate.

**c. All device power values are physically plausible**

Check the schedule visually. Battery SOC should stay within bounds across all
steps. EV schedule should show charging only when `plugged_in: true`. Grid
import and export should not both be positive at the same step (if the mode
guard is working correctly). Any power flow that looks physically wrong is
evidence of a constraint error, not a cosmetic diff.

**d. Power balance at each step**

For each step, verify that `grid_import + sum(producing devices) ≈ grid_export
+ sum(consuming devices)`. The solver enforces this as a hard constraint, but
a visual check of one or two steps in each scenario confirms the model is not
hallucinating values.

### 5. Commit the regenerated golden files

```bash
git add tests/scenarios/
git commit -m "regen: golden files updated for CBC backend

CBC and HiGHS produce equivalent-cost schedules but may differ in tie-
breaking when multiple optimal allocations exist. Objective values are
equal or fractionally lower (CBC finds marginally better solutions in
some cases). All scenarios remain optimal.

See SOLVER_REWRITE.md for the full rationale for the backend change."
```

### 6. Run the full test suite to confirm a clean baseline

```bash
uv run pytest -q
```

All tests must pass. The `-m 'not integration'` filter in `pyproject.toml`
excludes integration tests from the standard run, so this covers unit +
scenario + any non-integration tests in other directories.

---

## If a scenario solve_status is "infeasible" in the new golden file

This must not happen. An infeasible scenario means a constraint is too tight
or a bound is wrong for the CBC model. Possible causes:

1. **Constraint direction**: python-mip's `LinConstr` has the same Python
   operator overloading as highspy, but verify that `>=` and `<=` are not
   accidentally reversed anywhere in device code after the migration.

2. **Bounds at 1e30**: highspy uses `1e30` as "effectively unbounded". python-
   mip also accepts `1e30` for `ub`. Verify that the default `ub=1e30` in
   `CBCSolverBackend.add_var` is behaving as unbounded rather than as a
   literal constraint at 1e30.

3. **Binary variable bounds**: `add_var(lb=0.0, ub=1.0, integer=True)` in
   `CBCSolverBackend` creates `mip.Model.add_var(lb=0.0, ub=1.0, var_type=INTEGER)`.
   This is the correct CBC binary variable. Verify via `assert backend.var_value(b) in (0.0, 1.0)` in a trivial test if suspicious.

4. **SOS2 constraint tightness**: The SOS2 emulation adds `sum(b_i) == 1`.
   If `n_seg = 0` (only one variable), `n < 2` is caught. But if only one
   breakpoint variable is created, `add_constraint(variables[0] <= binaries[0])`
   requires `binaries[0]` to exist — it does (n_seg = n - 1 >= 1 when n >= 2).

---

## Acceptance criteria

- All scenario tests pass: `uv run pytest tests/scenarios/ -q` exits 0.
- Every golden file has `solve_status: "optimal"`.
- No golden file has an objective value worse than the previous HiGHS golden
  (a higher value for a minimisation problem would indicate a regression).
- `uv run pytest -q` (full suite excluding integration) exits 0.
- The diff is committed with an explanatory commit message.
