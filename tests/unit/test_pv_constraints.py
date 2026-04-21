"""Unit tests for mimirheim/devices/pv.py — PV device (forecast parameter, no variables).

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=4, dt=0.25.
"""

import pytest

from mimirheim.config.schema import GridConfig, PvCapabilitiesConfig, PvConfig, PvOutputsConfig
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.grid import Grid
from mimirheim.devices.pv import PvDevice, PvInputs


def _make_ctx(horizon: int = 4) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _config() -> PvConfig:
    return PvConfig(max_power_kw=5.0, topic_forecast="mimir/pv/forecast")


def test_pv_net_power_equals_forecast() -> None:
    """net_power(t) must return the forecast value for each step."""
    ctx = _make_ctx()
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[2.0, 1.5, 0.0, 3.0]))
    assert pv.net_power(0) == 2.0
    assert pv.net_power(1) == 1.5
    assert pv.net_power(2) == 0.0
    assert pv.net_power(3) == 3.0


def test_pv_negative_forecast_clipped_to_zero() -> None:
    """Negative forecast values (sensor noise) must be clipped to 0.0."""
    ctx = _make_ctx()
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[2.0, -0.5, 1.0, 0.0]))
    assert pv.net_power(1) == 0.0


def test_pv_net_power_positive() -> None:
    """PV produces power — net_power must be non-negative."""
    ctx = _make_ctx()
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 2.0, 1.0, 0.0]))
    for t in ctx.T:
        assert pv.net_power(t) >= 0.0


def test_pv_adds_no_variables() -> None:
    """PV has no decision variables — solver variable count must not change."""
    ctx = _make_ctx()
    pv = PvDevice(name="pv", config=_config())
    before = ctx.solver._m.num_cols
    pv.add_variables(ctx)
    after = ctx.solver._m.num_cols
    assert before == after


def test_pv_objective_terms_zero() -> None:
    """objective_terms must always return 0."""
    ctx = _make_ctx()
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[1.0, 1.0, 1.0, 1.0]))
    assert pv.objective_terms(0) == 0


# ---------------------------------------------------------------------------
# Variable PV production (plan 18)
# ---------------------------------------------------------------------------


def _config_power_limit(forecast_kw: float = 5.0) -> PvConfig:
    return PvConfig(
        max_power_kw=forecast_kw,
        topic_forecast="mimir/pv/forecast",
        capabilities=PvCapabilitiesConfig(power_limit=True),
    )


def _config_on_off(forecast_kw: float = 4.0) -> PvConfig:
    return PvConfig(
        max_power_kw=forecast_kw,
        topic_forecast="mimir/pv/forecast",
        capabilities=PvCapabilitiesConfig(on_off=True),
        outputs=PvOutputsConfig(on_off_mode="mimir/pv/on_off_mode"),
    )


def test_pv_fixed_forecast_net_power_is_float() -> None:
    """With both capabilities disabled, net_power returns a plain Python float."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 2.0]))
    assert isinstance(pv.net_power(0), float)
    assert isinstance(pv.net_power(1), float)


def test_pv_power_limit_variable_bounded_by_forecast() -> None:
    """power_limit mode: maximising pv_kw[t] must not exceed the forecast."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config_power_limit(forecast_kw=5.0))
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[5.0, 5.0]))

    # Maximise pv_kw[0] by minimising its negative.
    ctx.solver.set_objective_minimize(-pv.net_power(0))
    ctx.solver.solve()

    val = ctx.solver.var_value(pv.net_power(0))
    assert val <= 5.0 + 1e-6, f"pv_kw[0]={val:.4f} exceeded forecast of 5.0"


def test_pv_power_limit_curtails_at_negative_export_price() -> None:
    """power_limit mode: solver turns off PV at steps where export price is negative.

    Step 0: export price −0.05 EUR/kWh (paying to export). Solver must set pv_kw[0]=0.
    Step 1: export price +0.10 EUR/kWh (earning). Solver keeps pv_kw[1] at forecast.
    """
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_power_limit(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 3.0]))

    # Power balance: no base load; any PV surplus must be exported or curtailed.
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    # Objective: minimise import cost minus export revenue.
    # Step 0 export_price = -0.05 → exporting adds 0.05*export[0] to cost.
    # Step 1 export_price = +0.10 → exporting subtracts 0.10*export[1] from cost.
    import_prices = [0.25, 0.25]
    export_prices = [-0.05, 0.10]
    obj = sum(
        import_prices[t] * grid.import_[t] - export_prices[t] * grid.export_[t]
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    pv_0 = ctx.solver.var_value(pv.net_power(0))
    pv_1 = ctx.solver.var_value(pv.net_power(1))
    assert pv_0 < 1e-4, f"Expected PV curtailed at step 0 (negative export price), got {pv_0:.4f}"
    assert pv_1 > 3.0 - 1e-4, f"Expected PV at full forecast at step 1, got {pv_1:.4f}"


def test_pv_on_off_binary_produces_full_or_zero() -> None:
    """on_off mode: each step's production is either 0 or the full forecast, nothing in between."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config_on_off(forecast_kw=4.0))
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[4.0, 4.0]))

    # Force exactly one step to be on: total production = 4 kW × 1 step × 0.25 h = 1 kWh.
    # Keep the other step off by constraining sum to at most 1 kWh.
    dt = 0.25
    total_production = sum(pv.net_power(t) * dt for t in ctx.T)
    ctx.solver.add_constraint(total_production >= 1.0 - 1e-6)
    ctx.solver.add_constraint(total_production <= 1.0 + 1e-6)
    ctx.solver.set_objective_minimize(0)
    ctx.solver.solve()

    for t in ctx.T:
        val = ctx.solver.var_value(pv.net_power(t))
        assert val < 1e-4 or val > 4.0 - 1e-4, (
            f"Expected 0 or 4.0 at step {t}, got {val:.4f} (binary must produce full or zero)"
        )


def test_pv_on_off_is_on_returns_true_when_not_curtailed() -> None:
    """is_on() returns True when the solver has no reason to curtail.

    With a near-zero forecast pv_curtailed[t] has no effect on the power
    balance, so the only objective term that references it is the 1e-6 penalty
    from objective_terms(). Minimisation prefers pv_curtailed=0 (not curtailed),
    so is_on() returns True.
    """
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_on_off(forecast_kw=5.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[0.01]))

    ctx.solver.add_constraint(grid.net_power(0) + pv.net_power(0) == 0)
    # Include the curtailment penalty so the solver prefers pv_curtailed=0.
    ctx.solver.set_objective_minimize(pv.objective_terms(0))
    ctx.solver.solve()

    assert pv.is_on(0) is True


def test_pv_on_off_curtails_at_negative_export_price() -> None:
    """on_off mode: solver switches off PV at steps where export price is negative."""
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_on_off(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    import_prices = [0.25, 0.25]
    export_prices = [-0.05, 0.10]
    obj = sum(
        import_prices[t] * grid.import_[t] - export_prices[t] * grid.export_[t]
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    pv_curtailed_0 = ctx.solver.var_value(pv._pv_curtailed[0])
    assert pv_curtailed_0 > 1 - 1e-4, (
        f"Expected pv_curtailed[0]=1 (negative export price, array off), got {pv_curtailed_0:.4f}"
    )


def test_pv_zero_export_capability_flag_unchanged() -> None:
    """on_off and power_limit capabilities do not affect zero_export."""
    caps_plain = PvCapabilitiesConfig()
    caps_on_off = PvCapabilitiesConfig(on_off=True)
    caps_power_limit = PvCapabilitiesConfig(power_limit=True)

    assert caps_plain.zero_export is False
    assert caps_on_off.zero_export is False
    assert caps_power_limit.zero_export is False

    # Explicitly setting zero_export must still work alongside on_off.
    caps_ze = PvCapabilitiesConfig(on_off=True, zero_export=True)
    assert caps_ze.zero_export is True
    assert caps_ze.on_off is True


# ---------------------------------------------------------------------------
# PV mode mutual exclusion validation (Plan 39)
# ---------------------------------------------------------------------------


def test_pv_power_limit_and_on_off_together_raises() -> None:
    """PvCapabilitiesConfig rejects power_limit=True and on_off=True simultaneously.

    The two capabilities are mutually exclusive: a continuous inverter uses a
    power limit setpoint; a binary on/off inverter is either on or off. No
    real hardware drives both registers at the same time.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="mutually exclusive"):
        PvCapabilitiesConfig(power_limit=True, on_off=True)


def test_pv_power_limit_alone_valid() -> None:
    """PvCapabilitiesConfig with power_limit=True only is accepted without error."""
    caps = PvCapabilitiesConfig(power_limit=True)
    assert caps.power_limit is True
    assert caps.on_off is False


def test_pv_on_off_alone_valid() -> None:
    """PvCapabilitiesConfig with on_off=True only is accepted without error."""
    caps = PvCapabilitiesConfig(on_off=True)
    assert caps.on_off is True
    assert caps.power_limit is False


def test_pv_power_limit_mode_adds_continuous_variable() -> None:
    """power_limit mode adds _pv_kw variables; _pv_on must be empty."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config_power_limit())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[5.0, 5.0]))

    assert len(pv._pv_kw) > 0
    assert len(pv._pv_curtailed) == 0


def test_pv_on_off_mode_adds_binary_variable() -> None:
    """on_off mode adds _pv_curtailed binaries; _pv_kw must be empty."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config_on_off())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[4.0, 4.0]))

    assert len(pv._pv_curtailed) > 0
    assert len(pv._pv_kw) == 0


def test_pv_neither_capability_is_fixed_forecast() -> None:
    """With no capabilities, net_power returns plain floats (no solver variables)."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 2.0]))

    assert len(pv._pv_kw) == 0
    assert len(pv._pv_curtailed) == 0
    assert pv.net_power(0) == 3.0
    assert pv.net_power(1) == 2.0


# ---------------------------------------------------------------------------
# Staged power output (Plan 30)
# ---------------------------------------------------------------------------

# Staged-mode helper
_STAGES = [0.0, 1.5, 3.0, 4.5]


def _config_staged(
    stages: list[float] | None = None,
    max_power_kw: float = 4.5,
) -> PvConfig:
    return PvConfig(
        max_power_kw=max_power_kw,
        topic_forecast="mimir/pv/forecast",
        production_stages=stages if stages is not None else _STAGES,
    )


def test_staged_pv_selects_exactly_one_stage_per_step() -> None:
    """Exactly one stage_active binary must equal 1 at every time step."""
    ctx = _make_ctx(horizon=2)
    pv = PvDevice(name="pv", config=_config_staged())
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.8, 3.8]))
    ctx.solver.set_objective_minimize(0)
    ctx.solver.solve()

    for t in ctx.T:
        active_count = sum(
            round(ctx.solver.var_value(pv._stage_active[(t, s)]))
            for s in range(len(_STAGES))
        )
        assert active_count == 1, (
            f"Expected exactly one active stage at t={t}, got {active_count}"
        )


def test_staged_pv_effective_output_capped_by_forecast() -> None:
    """Effective output must not exceed the forecast, even when a higher stage is chosen.

    Forecast is 1.0 kW, stages are [0.0, 1.5, 3.0]. With a positive export
    price the solver wants maximum production. It selects stage 1.5 or above,
    but effective output is min(1.0, stage_kw) = 1.0.
    """
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0], max_power_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[1.0, 1.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    export_prices = [0.10, 0.10]
    import_prices = [0.25, 0.25]
    obj = sum(
        import_prices[t] * grid.import_[t] - export_prices[t] * grid.export_[t]
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    for t in ctx.T:
        eff = ctx.solver.var_value(pv.net_power(t))
        assert eff <= 1.0 + 1e-6, f"Effective output {eff:.4f} exceeded forecast 1.0 at t={t}"
        assert eff > 1.0 - 1e-4, f"Expected effective output ≈ 1.0 at t={t}, got {eff:.4f}"


def test_staged_pv_curtails_at_negative_export_price() -> None:
    """Staged PV: solver selects stage 0 (off) when export price is negative.

    Step 0: export price −0.05 → solver switches off array.
    Step 1: export price +0.10 → solver selects the highest productive stage.
    """
    ctx = _make_ctx(horizon=2)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0], max_power_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0, 3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    import_prices = [0.25, 0.25]
    export_prices = [-0.05, 0.10]
    obj = sum(
        import_prices[t] * grid.import_[t] - export_prices[t] * grid.export_[t]
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    pv_0 = ctx.solver.var_value(pv.net_power(0))
    pv_1 = ctx.solver.var_value(pv.net_power(1))
    assert pv_0 < 1e-4, f"Expected stage 0 (off) at step 0, got effective output {pv_0:.4f}"
    assert pv_1 > 3.0 - 1e-4, (
        f"Expected full production at step 1 (positive export price), got {pv_1:.4f}"
    )


def test_staged_pv_chosen_stage_kw_is_stage_not_effective_output() -> None:
    """chosen_stage_kw must return the stage's registered kW, not the effective output.

    Forecast is 2.2 kW. Stages are [0.0, 1.5, 3.0]. With a positive export
    price the solver maximises output: effective output = min(2.2, 3.0) = 2.2,
    but the stage register value is 3.0. The hardware must receive 3.0.
    """
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0], max_power_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[2.2]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    export_prices = [0.10]
    import_prices = [0.25]
    obj = sum(
        import_prices[t] * grid.import_[t] - export_prices[t] * grid.export_[t]
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    stage_kw = pv.chosen_stage_kw(0)
    effective_output = ctx.solver.var_value(pv.net_power(0))

    # The chosen stage is 3.0 (the highest productive stage), but effective
    # output is 2.2 (capped by the forecast). They must differ.
    assert abs(stage_kw - 3.0) < 1e-4, (
        f"Expected chosen_stage_kw=3.0 (the stage register), got {stage_kw:.4f}"
    )
    assert abs(effective_output - 2.2) < 1e-4, (
        f"Expected effective output=2.2 (forecast cap), got {effective_output:.4f}"
    )


# ---------------------------------------------------------------------------
# Staged mode tie-breaking: prefer highest equivalent stage (Plan fix)
# ---------------------------------------------------------------------------


def test_staged_pv_tie_break_prefers_highest_stage_when_indifferent() -> None:
    """When multiple stages produce identical effective output, the solver must
    choose the highest stage register value.

    With stages [0.0, 1.5, 3.0, 4.5] and a forecast of 0.5 kW, every stage
    with kW >= 0.5 produces the same effective output (0.5 kW). Without a
    tie-breaking objective term, the solver may pick any of them (1.5, 3.0,
    4.5) arbitrarily. With tie-breaking, it must pick 4.5, matching the
    semantics of "free-running at max capacity".
    """
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0, 4.5], max_power_kw=4.5))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[0.5]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    # Include tie-breaking term. Export price is positive so the solver wants
    # the full 0.5 kW, but is indifferent among stages 1.5, 3.0, 4.5.
    export_price = 0.10
    import_price = 0.25
    obj = import_price * grid.import_[0] - export_price * grid.export_[0]
    obj = obj + pv.objective_terms(0)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    stage = pv.chosen_stage_kw(0)
    assert abs(stage - 4.5) < 1e-4, (
        f"Expected tie-breaking to select highest stage (4.5 kW), got {stage:.4f}"
    )


# ---------------------------------------------------------------------------
# is_curtailed() — mode-agnostic curtailment signal (Plan fix)
# ---------------------------------------------------------------------------


def test_pv_staged_is_curtailed_false_when_free_running() -> None:
    """is_curtailed() returns False when the solver picks a stage >= forecast.

    With a positive export price the solver has no reason to curtail; it selects
    the highest stage, which is above the forecast. is_curtailed must return False.
    """
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0, 4.5], max_power_kw=4.5))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[2.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    obj = 0.25 * grid.import_[0] - 0.10 * grid.export_[0] + pv.objective_terms(0)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is False


def test_pv_staged_is_curtailed_true_when_stage_below_forecast() -> None:
    """is_curtailed() returns True when solver selects stage 0 (off) to avoid
    exporting at a negative price.

    The chosen stage kW (0.0) is below the forecast (2.0 kW), so the inverter
    register is limiting output below what the sun could deliver.
    """
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_staged(stages=[0.0, 1.5, 3.0, 4.5], max_power_kw=4.5))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[2.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    # Strongly negative export price: exporting costs money, so solver turns off PV.
    obj = 0.25 * grid.import_[0] - (-0.20) * grid.export_[0] + pv.objective_terms(0)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is True


def test_pv_power_limit_is_curtailed_false_when_at_forecast() -> None:
    """is_curtailed() returns False in power_limit mode when solver produces the full forecast."""
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_power_limit(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    obj = 0.25 * grid.import_[0] - 0.10 * grid.export_[0]
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is False


def test_pv_power_limit_is_curtailed_true_when_curtailed() -> None:
    """is_curtailed() returns True in power_limit mode when solver chose below forecast."""
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_power_limit(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    # Strongly negative export price: solver drives pv_kw to 0.
    obj = 0.25 * grid.import_[0] - (-0.20) * grid.export_[0]
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is True


def test_pv_on_off_is_curtailed_false_when_on() -> None:
    """is_curtailed() returns False in on_off mode when the array is switched on."""
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_on_off(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    obj = 0.25 * grid.import_[0] - 0.10 * grid.export_[0] + pv.objective_terms(0)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is False


def test_pv_on_off_is_curtailed_true_when_off() -> None:
    """is_curtailed() returns True in on_off mode when the array is switched off."""
    ctx = _make_ctx(horizon=1)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    pv = PvDevice(name="pv", config=_config_on_off(forecast_kw=3.0))

    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    pv.add_variables(ctx)
    pv.add_constraints(ctx, inputs=PvInputs(forecast_kw=[3.0]))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + pv.net_power(t) == 0)

    # Strongly negative export price: solver switches off PV.
    obj = 0.25 * grid.import_[0] - (-0.20) * grid.export_[0] + pv.objective_terms(0)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    assert pv.is_curtailed(0) is True

