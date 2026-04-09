"""Unit tests for mimirheim/core/context.py — ModelContext.

All tests must fail before the implementation exists (TDD).
"""

from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend


def test_model_context_T_is_range() -> None:
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    assert ctx.T == range(4)


def test_model_context_dt_stored() -> None:
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=96, dt=0.25)
    assert ctx.dt == 0.25


def test_model_context_solver_stored() -> None:
    backend = CBCSolverBackend()
    ctx = ModelContext(solver=backend, horizon=4, dt=0.25)
    assert ctx.solver is backend


def test_model_context_no_bundle_attribute() -> None:
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    assert not hasattr(ctx, "bundle")
    assert not hasattr(ctx, "config")
