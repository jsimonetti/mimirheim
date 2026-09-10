# 2. Solver backend

**Decision: CBC (COIN-OR Branch and Cut), abstracted interface**

mimirheim uses [CBC](https://github.com/coin-or/Cbc) (free, Eclipse Public Licence 2.0) via the
[`python-mip`](https://www.python-mip.com/) package for MILP solving. CBC is bundled as a
compiled shared library inside `python-mip`; no external binary installation is required.

## Rationale

The benchmark scenario `prosumer_ev_48h` (192 time steps, 768 binary variables) was measured
under both solvers on the same model:

| Solver | Method | Total time |
|---|---|---|
| HiGHS via `highspy` | Python API (addVar/addConstr) | ~21 s |
| HiGHS | CLI from MPS file | ~6 s |
| CBC | CLI from MPS file | **~0.2 s** |
| CBC via `python-mip` | Python API | **~1 s** |

The dominant cause is cut generation. CBC's aggressive Gomory cuts are highly effective on the
temperature-coupled binary chains that thermal device constraints (boiler, combi heat pump, space
heating HP) produce. At the root node, CBC tightens the LP relaxation enough to prove optimality
with very few branch-and-bound nodes. HiGHS converges slowly on the same structure.

A secondary cause is model-build overhead: `highspy` adds variables and constraints one at a time
via FFI calls, producing approximately 8 seconds of pure Python→C++ overhead at 192 steps before
the solver even starts. `python-mip` via CBC has similar call-by-call overhead but the solver
itself is so much faster that it dominates less.

CBC is free, redistributable, and well-established (it is the default solver in PuLP and many
other open-source optimisation tools). No licence management is required.

The measurements above are the record of that decision. HiGHS was removed from the codebase once
CBC had replaced it; the `SolverBackend` Protocol remains so that another backend can be
substituted without touching model-building code.

## Configurable time limit

A `time_limit_seconds` cap prevents the solver from blocking the re-solve loop. It is set by
`solver.time_limit_seconds` in the config (default: 59 s) and is threaded from `build_and_solve`
into `SolverBackend.solve`. If the limit is hit, CBC returns the best incumbent found so far. This
is acceptable for a rolling-horizon strategy — a slightly suboptimal schedule is better than no
schedule.

The cap covers the whole solve cycle, not one solver invocation. `minimize_consumption` is
lexicographic and solves twice: `ObjectiveBuilder.build` runs the phase-1 volume minimisation
internally and returns the unspent half of the budget, which `build_and_solve` passes to the
phase-2 solve. The total therefore stays inside the configured limit.

## SolverBackend interface

The solver is not called directly from device or objective code. All interactions go through a
thin `SolverBackend` Protocol so any compliant backend can be substituted without touching
model-building code:

```python
# mimirheim/core/solver_backend.py
from typing import Any, Protocol

class SolverBackend(Protocol):
    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any: ...
    def add_constraint(self, expr) -> None: ...
    def set_objective_minimize(self, expr) -> None: ...
    def set_objective_maximize(self, expr) -> None: ...
    def solve(self, time_limit_seconds: float) -> str: ...   # returns "optimal" | "feasible" | "infeasible"
    def var_value(self, var: Any) -> float: ...
    def add_sos2(self, variables: list[Any], weights: list[float]) -> None: ...
    def objective_value(self) -> float: ...
    def model_stats(self) -> tuple[int, int, int, int]: ...
```

`ModelContext.solver` is typed as `SolverBackend`. The concrete implementation is
`CBCSolverBackend`, which wraps `mip.Model`. Device classes never import `mip` directly.

## python-mip API mapping

| `SolverBackend` method | `python-mip` equivalent |
|---|---|
| `add_var(lb, ub, integer)` | `model.add_var(lb=lb, ub=ub, var_type=INTEGER\|CONTINUOUS)` |
| `add_constraint(expr)` | `model += expr` |
| `set_objective_minimize(expr)` | `model.objective = mip.minimize(expr)` |
| `set_objective_maximize(expr)` | `model.objective = mip.maximize(expr)` |
| `solve(t)` | `model.optimize(max_seconds=t)`, then map `OptimizationStatus` |
| `var_value(var)` | `var.x` |
| `objective_value()` | `model.objective_value` |
| `add_sos2(vars, weights)` | Binary emulation (see below) |

## SOS2 implementation

`python-mip` does not expose a native SOS2 constraint API that maps cleanly to the
`SolverBackend` Protocol. The `add_sos2` method is implemented via a binary emulation that relies
only on `add_var` and `add_constraint`, so it is portable across any backend:

```
For N weight variables w[0..N-1], create N-1 binary variables b[0..N-2]:
    sum(b_i) == 1                       (exactly one segment active)
    w[0]   <= b[0]
    w[i]   <= b[i-1] + b[i]             (interior variables)
    w[N-1] <= b[N-2]
```

When `b[i] = 1`, only `w[i]` and `w[i+1]` can be nonzero; all others are forced to zero by
their upper-bound constraints. This correctly models piecewise-linear interpolation along a
single segment at a time.

## CBC tuning parameters

`CBCSolverBackend.__init__` (`mimirheim/core/solver_backend.py`) sets several CBC parameters beyond the defaults, each justified by a specific benchmark observation:

- **`max_mip_gap = 5e-3`** (accept a solution within 0.5% of the true optimum). For a residential energy schedule this is imperceptible in practice — on a EUR 50/day schedule the error is at most 25 cents. The threshold must be at least as large as the model's natural integrality gap, the gap that branch-and-bound cannot close within the 59-second wall-clock limit: the `prosumer_ev_48h` benchmark (192 steps, 768 binary variables) has an integrality gap of approximately 0.18%. 0.5% gives comfortable margin above the observed gap across all benchmark scenarios, so CBC exits as soon as it has a good solution rather than spending the remaining budget proving negligible improvements.
- **`emphasis = mip.SearchEmphasis.FEASIBILITY`** (prioritise finding a first feasible integer solution). On the 672-step `worst_case_7d` scenario, the default heuristic budget is too small to find any feasible solution within 59 seconds. This setting runs 50 feasibility pump passes and enables proximity search before branching, bringing time-to-first-feasible from over 59 seconds to a few seconds on that scenario. It has no effect on the gap acceptance threshold above.
- **`threads = -1`** by default (use all available CPU cores) — appropriate for a dedicated home server otherwise idle between solve cycles.
- **`INT_PARAM_ROUND_INT_VARS` (LP-rounding heuristic), set via the low-level `cbclib` C binding** since `python-mip` exposes no high-level property for it. After solving the LP relaxation at each branch-and-bound node, CBC rounds fractional binary variables to the nearest integer and checks feasibility. Near-zero cost, and a fast fallback when the feasibility pump does not converge immediately — without it, some models with many near-integer LP solutions spend extra time in the tree before a first feasible incumbent appears.
