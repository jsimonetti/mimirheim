# Step 04 — Solver backend

## References

- IMPLEMENTATION_DETAILS §2 (HiGHS rationale, `SolverBackend` Protocol)

---

## Files to create

- `mimirheim/core/solver_backend.py`
- `tests/unit/test_solver_backend.py`

Note: `tests/unit/test_solver_backend.py` is not in the canonical test structure. Create it.

---

## Tests first

Create `tests/unit/test_solver_backend.py`. Tests must fail before any implementation exists.

- `test_highs_backend_solves_trivial_lp` — minimise `x` subject to `x >= 1.0`; assert `var_value` returns approximately 1.0 and `solve()` returns `"optimal"`
- `test_highs_backend_infeasible` — add constraints `x >= 2.0` and `x <= 1.0`; assert `solve()` returns `"infeasible"`
- `test_highs_backend_integer_var` — `add_var(lb=0, ub=3, integer=True)` with objective minimise `-x`; assert `var_value` is 3.0 (integer, not 3.0000001)
- `test_highs_backend_time_limit` — `solve(time_limit_seconds=0.001)` on a trivial problem returns without hanging; result is `"optimal"` or `"feasible"`
- `test_solver_backend_protocol_satisfied` — `isinstance(HiGHSSolverBackend(), SolverBackend)` is True (or use `runtime_checkable` Protocol)

Run `uv run pytest tests/unit/test_solver_backend.py` — all tests must fail before proceeding.

---

## Implementation

`mimirheim/core/solver_backend.py` contains two things:

### 1. SolverBackend Protocol

```python
from typing import Any, Protocol, runtime_checkable

@runtime_checkable
class SolverBackend(Protocol):
    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any: ...
    def add_constraint(self, expr: Any) -> None: ...
    def set_objective_minimize(self, expr: Any) -> None: ...
    def set_objective_maximize(self, expr: Any) -> None: ...
    def solve(self, time_limit_seconds: float = 59.0) -> str: ...
    def var_value(self, var: Any) -> float: ...
```

`solve()` returns one of the strings `"optimal"`, `"feasible"` (time-limited incumbent), or `"infeasible"`.

### 2. HiGHSSolverBackend

Wraps `highspy.Highs`. Consult the `highspy` API for the exact calls to add columns (variables), add rows (constraints), set objective sense and coefficients, and retrieve solution values.

Device classes must never import `highspy` directly. All solver interactions go through `SolverBackend`.

---

## Acceptance criteria

```bash
uv run pytest tests/unit/test_solver_backend.py
```

All tests green.

---

## Done

```bash
mv plans/04_solver_backend.md plans/done/
```
