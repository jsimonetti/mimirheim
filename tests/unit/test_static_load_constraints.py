"""Unit tests for mimirheim/devices/static_load.py — Static load device.

All tests must fail before the implementation exists (TDD). Tests use T=4, dt=0.25.
"""

from mimirheim.config.schema import StaticLoadConfig
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.static_load import StaticLoad, StaticLoadInputs


def _make_ctx(horizon: int = 4) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _config() -> StaticLoadConfig:
    return StaticLoadConfig(topic_forecast="mimir/load/base/forecast")


def test_static_load_net_power_negative() -> None:
    """net_power(t) must be -forecast[t] at each step (load consumes power)."""
    ctx = _make_ctx()
    load = StaticLoad(name="base", config=_config())
    load.add_variables(ctx)
    load.add_constraints(ctx, inputs=StaticLoadInputs(forecast_kw=[1.0, 2.0, 0.5, 1.5]))
    assert load.net_power(0) == -1.0
    assert load.net_power(1) == -2.0
    assert load.net_power(2) == -0.5
    assert load.net_power(3) == -1.5


def test_static_load_adds_no_variables() -> None:
    """Static load has no decision variables — solver variable count must not change."""
    ctx = _make_ctx()
    load = StaticLoad(name="base", config=_config())
    before = ctx.solver._m.num_cols
    load.add_variables(ctx)
    load.add_constraints(ctx, inputs=StaticLoadInputs(forecast_kw=[1.0, 1.0, 1.0, 1.0]))
    after = ctx.solver._m.num_cols
    assert before == after


def test_static_load_objective_terms_zero() -> None:
    """objective_terms must always return 0."""
    ctx = _make_ctx()
    load = StaticLoad(name="base", config=_config())
    load.add_variables(ctx)
    load.add_constraints(ctx, inputs=StaticLoadInputs(forecast_kw=[1.0, 1.0, 1.0, 1.0]))
    assert load.objective_terms(0) == 0


def test_static_load_zero_forecast_step() -> None:
    """net_power must be 0.0 when forecast is 0.0 at a step."""
    ctx = _make_ctx()
    load = StaticLoad(name="base", config=_config())
    load.add_variables(ctx)
    load.add_constraints(ctx, inputs=StaticLoadInputs(forecast_kw=[1.0, 0.0, 1.0, 1.0]))
    assert load.net_power(1) == 0.0
