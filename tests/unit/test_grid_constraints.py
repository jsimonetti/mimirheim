"""Unit tests for mimirheim/devices/grid.py — Grid device variables and constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with a short horizon (T=2, dt=0.25).
"""

from mimirheim.config.schema import GridConfig
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.grid import Grid


def _make_ctx(horizon: int = 2) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _make_grid(import_limit_kw: float = 10.0, export_limit_kw: float = 10.0) -> Grid:
    return Grid(config=GridConfig(import_limit_kw=import_limit_kw, export_limit_kw=export_limit_kw))


def test_grid_import_bounded_by_config() -> None:
    """Solver must not set import above the configured limit."""
    ctx = _make_ctx()
    grid = _make_grid(import_limit_kw=5.0)
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    # Incentive to import as much as possible; bound must stop it at 5.0.
    ctx.solver.set_objective_minimize(-grid.import_[0])
    ctx.solver.solve()
    assert ctx.solver.var_value(grid.import_[0]) <= 5.0 + 1e-6


def test_grid_export_bounded_by_config() -> None:
    """Solver must not set export above the configured limit."""
    ctx = _make_ctx()
    grid = _make_grid(export_limit_kw=3.0)
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    ctx.solver.set_objective_minimize(-grid.export_[0])
    ctx.solver.solve()
    assert ctx.solver.var_value(grid.export_[0]) <= 3.0 + 1e-6


def test_grid_net_power_is_import_minus_export() -> None:
    """net_power(t) equals import_[t] - export_[t] at the solution."""
    ctx = _make_ctx()
    grid = _make_grid()
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    # Fix net power to 2.0 kW via a constraint, then read back import and export.
    ctx.solver.add_constraint(grid.net_power(0) == 2.0)
    ctx.solver.set_objective_minimize(grid.import_[0])
    ctx.solver.solve()
    imp = ctx.solver.var_value(grid.import_[0])
    exp = ctx.solver.var_value(grid.export_[0])
    assert abs((imp - exp) - 2.0) < 1e-6


def test_grid_objective_terms_returns_zero() -> None:
    """objective_terms(t) must always be zero — economics live in ObjectiveBuilder."""
    ctx = _make_ctx()
    grid = _make_grid()
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    assert grid.objective_terms(0) == 0


def test_grid_add_constraints_accepts_none_inputs() -> None:
    """add_constraints must not raise when called with inputs=None."""
    ctx = _make_ctx()
    grid = _make_grid()
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)  # must not raise


# ---------------------------------------------------------------------------
# Simultaneous import/export binary guard (plan 19)
# ---------------------------------------------------------------------------


def test_grid_no_simultaneous_binaries_by_default() -> None:
    """add_variables must create exactly 3*T variables: 2 continuous + 1 binary per step."""
    ctx = _make_ctx(horizon=4)
    grid = Grid(config=GridConfig(import_limit_kw=10.0, export_limit_kw=10.0))
    before = ctx.solver._m.num_cols
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    after = ctx.solver._m.num_cols
    # import_[t], export_[t], _grid_dir[t] per step.
    assert after - before == 3 * 4


def test_grid_prevent_simultaneous_blocks_concurrent_flows() -> None:
    """The solver must not assign nonzero import and export at the same step.

    The test maximises import + export to create pressure for simultaneous flows.
    Without the binary guard, the optimum is import=10, export=10 at each step.
    With the guard, at most one direction can be nonzero per step.
    """
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=10.0, export_limit_kw=10.0))
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)

    # Maximise total throughput — this creates maximum incentive for simultaneous flows.
    obj = sum(-(grid.import_[t] + grid.export_[t]) for t in ctx.T)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    for t in ctx.T:
        imp = ctx.solver.var_value(grid.import_[t])
        exp = ctx.solver.var_value(grid.export_[t])
        assert min(imp, exp) < 1e-6, (
            f"Simultaneous flows at step {t}: import={imp:.4f}, export={exp:.4f}"
        )


def test_grid_prevent_simultaneous_allows_import_only() -> None:
    """A load-only scenario (net demand > 0) must still solve with nonzero import."""
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=10.0, export_limit_kw=10.0))
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    # Static load of 3 kW at every step. The grid must import to cover it.
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) == 3.0)
    ctx.solver.set_objective_minimize(grid.import_[0])
    ctx.solver.solve()

    assert ctx.solver.var_value(grid.import_[0]) > 3.0 - 1e-6
    assert ctx.solver.var_value(grid.export_[0]) < 1e-6


def test_grid_prevent_simultaneous_allows_export_only() -> None:
    """A surplus scenario (net demand < 0) must still solve with nonzero export."""
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=10.0, export_limit_kw=10.0))
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    # PV surplus of 2 kW at every step — net grid power must be −2 (export).
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) == -2.0)
    ctx.solver.set_objective_minimize(grid.export_[0])
    ctx.solver.solve()

    assert ctx.solver.var_value(grid.export_[0]) > 2.0 - 1e-6
    assert ctx.solver.var_value(grid.import_[0]) < 1e-6


def test_grid_prevent_simultaneous_respects_connection_limits() -> None:
    """Import and export must still be bounded by the configured limits."""
    ctx = _make_ctx(horizon=4)
    grid = Grid(config=GridConfig(import_limit_kw=5.0, export_limit_kw=3.0))
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    # Drive import and export toward their limits with a maximise throughput objective.
    obj = sum(-(grid.import_[t] + grid.export_[t]) for t in ctx.T)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(grid.import_[t]) <= 5.0 + 1e-6
        assert ctx.solver.var_value(grid.export_[t]) <= 3.0 + 1e-6
