# Step 33 — Implement CBCSolverBackend (TDD)

## References

- `SOLVER_REWRITE.md` §6, API mapping table
- `IMPLEMENTATION_DETAILS.md` §2 (updated in step 32)
- `mimirheim/core/solver_backend.py` — current `HiGHSSolverBackend`
- `tests/unit/test_solver_backend.py` — existing backend tests to be adapted
- `pyproject.toml` — dependency declarations

---

## Files to modify

- `pyproject.toml`
- `mimirheim/core/solver_backend.py`
- `tests/unit/test_solver_backend.py`

---

## Background

This step implements the `CBCSolverBackend` and verifies it against the
`SolverBackend` Protocol using the same LP/MIP tests currently written for
`HiGHSSolverBackend`. The implementation uses `python-mip` (`import mip`),
which bundles CBC as a compiled shared library.

`HiGHSSolverBackend` remains in the file during this step — it is removed
in step 36, after all downstream callers have been migrated.

---

## TDD workflow

### 1. Write the new tests first (must fail because CBCSolverBackend does not exist)

In `tests/unit/test_solver_backend.py`, add a parallel test class or a set
of standalone functions that mirror every existing `HiGHSSolverBackend` test
but instantiate `CBCSolverBackend` instead. The tests must fail with
`ImportError` or `AttributeError` until the implementation exists.

The new tests to add (naming pattern: replace `highs` with `cbc` in test
function names):

```python
from mimirheim.core.solver_backend import CBCSolverBackend

def test_cbc_backend_solves_trivial_lp() -> None:
    """Minimise x subject to x >= 1.0; expect x == 1.0 at optimality."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=1.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 1.0) < 1e-6

def test_cbc_backend_infeasible() -> None:
    """Constraints x >= 2.0 and x <= 1.0; expect infeasible."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=2.0)
    backend.add_constraint(x <= 1.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "infeasible"

def test_cbc_backend_integer_var() -> None:
    """Integer variable [0, 3], minimise -x; expect x == 3."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=0.0, ub=3.0, integer=True)
    backend.set_objective_minimize(-1.0 * x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 3.0) < 1e-6

def test_cbc_backend_time_limit() -> None:
    """A very tight time limit must not hang; result is optimal or feasible."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=1.0)
    backend.set_objective_minimize(x)
    status = backend.solve(time_limit_seconds=0.001)
    assert status in ("optimal", "feasible")

def test_cbc_solver_backend_protocol_satisfied() -> None:
    """CBCSolverBackend must satisfy the SolverBackend Protocol at runtime."""
    assert isinstance(CBCSolverBackend(), SolverBackend)

def test_cbc_backend_add_sos2_accepted() -> None:
    """add_sos2 must not raise on a freshly built model."""
    backend = CBCSolverBackend()
    w0 = backend.add_var(lb=0.0, ub=1.0)
    w1 = backend.add_var(lb=0.0, ub=1.0)
    w2 = backend.add_var(lb=0.0, ub=1.0)
    backend.add_sos2([w0, w1, w2], [0.0, 3.0, 6.0])
    backend.add_constraint(w0 + w1 + w2 == 1.0)
    backend.set_objective_minimize(w0 + w1 + w2)
    status = backend.solve()
    assert status == "optimal"

def test_cbc_backend_sos2_enforces_at_most_two_adjacent_nonzero() -> None:
    """SOS2 must prevent non-adjacent weights from both being nonzero simultaneously."""
    backend = CBCSolverBackend()
    w0 = backend.add_var(lb=0.0, ub=1.0)
    w1 = backend.add_var(lb=0.0, ub=1.0)
    w2 = backend.add_var(lb=0.0, ub=1.0)
    backend.add_sos2([w0, w1, w2], [0.0, 3.0, 6.0])
    backend.add_constraint(w0 + w1 + w2 == 1.0)
    # Minimise -(w0 + w2). Without SOS2, both w0=1 and w2=1 would be optimal
    # (with w1=0). With SOS2, only adjacent pairs may be nonzero, so the
    # solver must choose w0=1 (and w2=0) or w2=1 (and w0=0).
    backend.set_objective_minimize(-1.0 * w0 + -1.0 * w2)
    status = backend.solve()
    assert status == "optimal"
    v0 = backend.var_value(w0)
    v1 = backend.var_value(w1)
    v2 = backend.var_value(w2)
    # At most two adjacent weights active: (w0 + w1) or (w1 + w2).
    # Non-adjacent pair (w0, w2) must not both be nonzero.
    assert not (v0 > 0.5 and v2 > 0.5), (
        f"SOS2 violated: w0={v0:.3f}, w1={v1:.3f}, w2={v2:.3f}"
    )

def test_cbc_backend_objective_value() -> None:
    """objective_value() must return the value of the most recent solve."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=0.0, ub=5.0)
    backend.add_constraint(x >= 3.0)
    backend.set_objective_minimize(2.0 * x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.objective_value() - 6.0) < 1e-4

def test_cbc_backend_maximize() -> None:
    """set_objective_maximize must work; solver maximises the expression."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=0.0, ub=10.0)
    backend.set_objective_maximize(x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 10.0) < 1e-6

def test_cbc_backend_add_constraint_equality() -> None:
    """Equality constraint x == 4.0 must fix the variable at exactly 4."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=0.0, ub=10.0)
    backend.add_constraint(x == 4.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 4.0) < 1e-6
```

Run `uv run pytest tests/unit/test_solver_backend.py -k cbc` and confirm
all new tests fail with `ImportError`.

### 2. Add `mip` to pyproject.toml

In `pyproject.toml`, add `mip>=1.14` to `[project] dependencies`. Keep
`highspy>=1.7` for now — it is removed in step 36.

Run `uv sync` to install `python-mip`.

### 3. Implement CBCSolverBackend in solver_backend.py

Add the implementation after the existing `HiGHSSolverBackend` class. Do
not modify `HiGHSSolverBackend`. Do not remove any imports.

**Module-level imports to add at the top of the file (alongside highspy imports):**

```python
import mip
from mip import OptimizationStatus as MipStatus
```

**Class implementation:**

```python
class CBCSolverBackend:
    """CBC-backed implementation of SolverBackend via python-mip.

    Wraps mip.Model (CBC solver) and translates the SolverBackend interface
    into python-mip API calls. Variable handles returned by add_var are
    mip.Var objects; constraint and objective expressions are mip.LinExpr
    or mip.LinConstr objects produced via the overloaded comparison and
    arithmetic operators on those variables.

    CBC (COIN-OR Branch and Cut) is the default solver for mimirheim. It is
    bundled inside the mip package as a compiled shared library; no external
    installation is required. See https://www.python-mip.com/.
    """

    def __init__(self) -> None:
        # Create a new CBC model instance. verbose=0 suppresses all CBC
        # console output — mimirheim owns its own logging.
        self._m = mip.Model(solver_name=mip.CBC)
        self._m.verbose = 0

    def add_var(self, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any:
        var_type = mip.INTEGER if integer else mip.CONTINUOUS
        return self._m.add_var(lb=lb, ub=ub, var_type=var_type)

    def add_constraint(self, expr: Any) -> None:
        self._m += expr

    def set_objective_minimize(self, expr: Any) -> None:
        if isinstance(expr, (int, float)):
            return
        self._m.objective = mip.minimize(expr)

    def set_objective_maximize(self, expr: Any) -> None:
        if isinstance(expr, (int, float)):
            return
        self._m.objective = mip.maximize(expr)

    def solve(self, time_limit_seconds: float = 59.0) -> str:
        status = self._m.optimize(max_seconds=time_limit_seconds)
        if status == MipStatus.OPTIMAL:
            return "optimal"
        if status == MipStatus.FEASIBLE:
            return "feasible"
        if status in (MipStatus.INFEASIBLE, MipStatus.INT_INFEASIBLE):
            return "infeasible"
        # NO_SOLUTION_FOUND, UNBOUNDED, LOADED, or any other status.
        # Treat as infeasible: no schedule can be extracted.
        return "infeasible"

    def var_value(self, var: Any) -> float:
        return float(var.x)

    def add_sos2(self, variables: list[Any], weights: list[float]) -> None:
        # Identical Big-M binary emulation as HiGHSSolverBackend.add_sos2.
        # See that method's docstring for the mathematical derivation.
        n = len(variables)
        if n < 2:
            raise ValueError(
                f"add_sos2 requires at least 2 variables, got {n}."
            )
        n_seg = n - 1
        binaries = [self.add_var(lb=0.0, ub=1.0, integer=True) for _ in range(n_seg)]

        total_b: Any = binaries[0]
        for b in binaries[1:]:
            total_b = total_b + b
        self.add_constraint(total_b == 1)

        self.add_constraint(variables[0] <= binaries[0])
        for i in range(1, n - 1):
            self.add_constraint(variables[i] <= binaries[i - 1] + binaries[i])
        self.add_constraint(variables[-1] <= binaries[-1])

    def objective_value(self) -> float:
        val = self._m.objective_value
        return float(val) if val is not None else 0.0
```

**Docstrings:** All public methods must have full Google-style docstrings
matching the level of detail in `HiGHSSolverBackend`. Copy and adapt the
existing docstrings — the mathematical and operational descriptions are the
same; only the implementation-specific details change.

**Module docstring:** Update the module docstring to mention `CBCSolverBackend`
alongside `HiGHSSolverBackend` (the old one is still present at this stage).

### 4. Run the new tests

```bash
uv run pytest tests/unit/test_solver_backend.py -k cbc -v
```

All new tests must pass. If any test fails, fix the implementation before
continuing. Do not proceed to step 4 with failing tests.

### 5. Run the full unit test suite to confirm no regression

```bash
uv run pytest tests/unit/ -q
```

All tests must pass. The existing `HiGHSSolverBackend` tests must still
pass because that class is unchanged.

---

## Critical implementation notes

### `mip.minimize` and `mip.maximize` require an expression, not a scalar

python-mip's `minimize()` and `maximize()` functions accept `mip.LinExpr`
or `mip.Var` objects. Passing a plain `int` or `float` raises `TypeError`.
The guard `if isinstance(expr, (int, float)): return` in
`set_objective_minimize` and `set_objective_maximize` handles this. A model
with no objective set returns a feasible solution (any solution), which is
the correct behaviour for the scalar-zero case.

### `var.x` returns `None` before the first solve

If `var_value()` is called before `solve()` or after `solve()` returns
`"infeasible"`, `var.x` is `None`. The caller contract states
"calling this before solve() or after an infeasible solve returns an
undefined value". Do not add defensive None-handling in `var_value()`
— it would hide bugs. The existing contract is sufficient.

### `mip.INTEGER` with lb=0.0, ub=1.0 is equivalent to `mip.BINARY`

Using `mip.INTEGER` with explicit lb/ub bounds is preferred over `mip.BINARY`
because it keeps `add_var`'s interface uniform — the caller always passes
`integer=True` and two bounds. Using `BINARY` silently ignores the lb/ub
arguments, which could mask a caller error.

### The SOS2 emulation is identical to HiGHSSolverBackend

The Big-M binary emulation in `add_sos2` calls only `self.add_var()` and
`self.add_constraint()` — methods of the same class. There is no
solver-specific code. The emulation is correct for any backend.

---

## Acceptance criteria

- `uv add mip` installs successfully.
- All new `test_cbc_*` tests in `test_solver_backend.py` pass.
- `isinstance(CBCSolverBackend(), SolverBackend)` is True.
- All existing `HiGHSSolverBackend` tests still pass.
- `uv run pytest tests/unit/ -q` exits 0.
