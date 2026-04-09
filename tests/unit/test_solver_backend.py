"""Unit tests for mimirheim/core/solver_backend.py — SolverBackend Protocol and CBCSolverBackend."""

import pytest

from mimirheim.core.solver_backend import CBCSolverBackend, SolverBackend


# ---------------------------------------------------------------------------
# CBCSolverBackend tests (step 33)
# ---------------------------------------------------------------------------


def test_cbc_backend_solves_trivial_lp() -> None:
    """Minimise x subject to x >= 1.0; expect x == 1.0 at optimality."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=1.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 1.0) < 1e-6


def test_cbc_backend_infeasible() -> None:
    """Constraints x >= 2.0 and x <= 1.0 are mutually exclusive — expect infeasible."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=2.0)
    backend.add_constraint(x <= 1.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "infeasible"


def test_cbc_backend_integer_var() -> None:
    """Integer variable bounded to [0, 3]; minimise -x yields x == 3."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=0.0, ub=3.0, integer=True)
    backend.set_objective_minimize(-1.0 * x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 3.0) < 1e-6


def test_cbc_backend_time_limit() -> None:
    """A very tight time limit should not hang; result is optimal or feasible."""
    backend = CBCSolverBackend()
    x = backend.add_var(lb=1.0)
    backend.set_objective_minimize(x)
    status = backend.solve(time_limit_seconds=0.001)
    assert status in ("optimal", "feasible")


def test_cbc_solver_backend_protocol_satisfied() -> None:
    """CBCSolverBackend must satisfy the SolverBackend Protocol at runtime."""
    assert isinstance(CBCSolverBackend(), SolverBackend)


def test_cbc_backend_add_sos2_accepted() -> None:
    """Calling add_sos2 on a freshly built model must not raise."""
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
    """SOS2 must prevent non-adjacent weights from both being nonzero.

    Without SOS2, minimising -(w0 + w2) would set w0=1 and w2=1 while w1=0.
    With SOS2, only adjacent pairs are allowed, so the optimal solution is
    either (w0=1, w2=0) or (w0=0, w2=1).
    """
    backend = CBCSolverBackend()
    w0 = backend.add_var(lb=0.0, ub=1.0)
    w1 = backend.add_var(lb=0.0, ub=1.0)
    w2 = backend.add_var(lb=0.0, ub=1.0)
    backend.add_sos2([w0, w1, w2], [0.0, 3.0, 6.0])
    backend.add_constraint(w0 + w1 + w2 == 1.0)
    backend.set_objective_minimize(-(w0 + w2))
    status = backend.solve()
    assert status == "optimal"

    v0 = backend.var_value(w0)
    v1 = backend.var_value(w1)
    v2 = backend.var_value(w2)
    assert not (v0 > 1e-6 and v2 > 1e-6), (
        f"SOS2 violated: w0={v0:.4f}, w1={v1:.4f}, w2={v2:.4f}"
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


# ---------------------------------------------------------------------------
# threads parameter
# ---------------------------------------------------------------------------


def test_cbc_backend_accepts_explicit_thread_count() -> None:
    """CBCSolverBackend(threads=1) builds a working backend that solves correctly."""
    backend = CBCSolverBackend(threads=1)
    x = backend.add_var(lb=2.0)
    backend.set_objective_minimize(x)
    status = backend.solve()
    assert status == "optimal"
    assert abs(backend.var_value(x) - 2.0) < 1e-6


def test_cbc_backend_all_cores_setting_does_not_raise() -> None:
    """CBCSolverBackend(threads=-1) (all CPU cores) initialises without error."""
    backend = CBCSolverBackend(threads=-1)
    assert isinstance(backend, CBCSolverBackend)

