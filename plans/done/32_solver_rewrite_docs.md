# Step 32 — Update IMPLEMENTATION_DETAILS and README for CBC backend

## References

- `SOLVER_REWRITE.md` — situation document and decision record
- `IMPLEMENTATION_DETAILS.md` §2 — solver backend
- `README.md` §2 — mathematical model (solver mention at end of section)

---

## Files to modify

- `IMPLEMENTATION_DETAILS.md`
- `README.md`

---

## Background

The solver is changing from HiGHS (`highspy`) to CBC (`python-mip`). All
documentation that describes the solver choice, its rationale, and its
interface must be updated before any code is touched. The documentation is
the specification; changing docs first ensures the implementation follows
a recorded decision rather than the decision following an implementation.

No code changes occur in this step. The acceptance criterion is purely
documentary.

---

## Changes to IMPLEMENTATION_DETAILS.md

### Section 2 — Solver backend

Replace the entire section with content that reflects the CBC decision.
The new section must cover:

**Decision statement:**
> mimirheim uses CBC (COIN-OR Branch and Cut, free, Eclipse Public License 2.0)
> via the `python-mip` Python bindings as its default MILP solver.

**Rationale (from measurements, all documented in `SOLVER_REWRITE.md`):**

- The prosumer_ev_48h benchmark (192 steps, 768 binary variables) takes
  approximately 21 seconds under HiGHS via the Python API. CBC solves the
  same model in approximately 0.2 seconds — roughly 100 times faster.
- The cause is CBC's aggressive Gomory cut generation, which is
  particularly effective on the temperature-coupled binary chains produced
  by thermal device constraints (boiler, combi heat pump, space heating HP).
- CBC is bundled as a compiled extension inside `python-mip`; no separately
  installed binary is required.

**Why not HiGHS:**
- HiGHS is competitive on larger, sparser MIP problems. For mimirheim's densely
  time-coupled thermal binaries it converges slowly.
- The highspy Python bindings add ~8 seconds of model-build overhead at
  192 steps via one-at-a-time addVariable/addConstr FFI calls.

**Configurable time limit:** unchanged — 59 seconds default.

**SolverBackend interface:** unchanged — the Protocol definition does not
change. Only the concrete implementation changes.

**add_sos2 implementation:**
Note that the Big-M binary variable emulation of SOS2 (currently documented
as an HiGHS-specific workaround) is now the canonical implementation and no
longer a workaround — python-mip uses the same approach. The note about
"the installed highspy version does not expose a native SOS2 API" must be
updated to describe the emulation as a deliberate, solver-agnostic choice
(it avoids dependency on native SOS2 support in any backend).

**python-mip API mapping:** Include a table documenting how the Protocol
methods map to python-mip calls:

| SolverBackend method | python-mip equivalent |
|---|---|
| `add_var(lb, ub, integer)` | `model.add_var(lb=lb, ub=ub, var_type=INTEGER\|CONTINUOUS)` |
| `add_constraint(expr)` | `model += expr` |
| `set_objective_minimize(expr)` | `model.objective = mip.minimize(expr)` |
| `set_objective_maximize(expr)` | `model.objective = mip.maximize(expr)` |
| `solve(t)` | `model.optimize(max_seconds=t)` → map `OptimizationStatus` |
| `var_value(var)` | `var.x` |
| `objective_value()` | `model.objective_value` |
| `add_sos2(vars, weights)` | Big-M binary emulation (same as before) |

**Testing architecture note in §4:** Update the statement "Each device is
tested with a minimal horizon (T=4) by constructing the LP directly via
`pulp` or `highspy` primitives" — remove the reference to `pulp` and
`highspy`; the correct wording is "via `CBCSolverBackend` directly".

---

## Changes to README.md

### Section 2 — Mathematical model, Solver paragraph

Update the final paragraph of §2 that reads:

> **HiGHS** (free, MIT licence, embedded via `highspy`). The solver
> interface is abstracted behind a `SolverBackend` protocol; Gurobi or
> CBC can be substituted. A 30-second time limit prevents blocking.

Change to:

> **CBC** (COIN-OR Branch and Cut, free, EPL 2.0, embedded via
> `python-mip`). The solver interface is abstracted behind a
> `SolverBackend` protocol; any solver satisfying the protocol can be
> substituted. A 59-second time limit prevents blocking.

---

## Acceptance criteria

- `IMPLEMENTATION_DETAILS.md` §2 describes CBC as the solver, with the
  API mapping table present.
- `IMPLEMENTATION_DETAILS.md` §4 does not mention `highspy` or `pulp` in
  the unit test description.
- `README.md` §2 solver paragraph names CBC and `python-mip`.
- No code has been changed.
- `uv run pytest` still passes (no regressions — this step is docs only).
