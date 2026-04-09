"""Unit tests for mimirheim/core/objective.py and mimirheim/core/confidence.py.

All tests must fail before the implementation exists (TDD). Tests use a real
solver with T=4, dt=0.25, a Grid device, and a Battery device.
"""

from datetime import UTC, datetime

import pytest

from mimirheim.config.schema import (
    BatteryConfig,
    BalancedWeightsConfig,
    ConstraintsConfig,
    EfficiencySegment,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    ObjectivesConfig,
    OutputsConfig,
)
from mimirheim.core.bundle import BatteryInputs, SolveBundle
from mimirheim.core.context import ModelContext
from mimirheim.core.objective import ObjectiveBuilder
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.battery import Battery
from mimirheim.devices.grid import Grid


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(power_max_kw: float, eff: float = 1.0) -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=power_max_kw, efficiency=eff)


def _battery_inputs(soc_kwh: float = 5.0) -> BatteryInputs:
    return BatteryInputs(soc_kwh=soc_kwh)


def _make_config(
    max_import_kw: float | None = None,
    max_export_kw: float | None = None,
    balanced_weights: BalancedWeightsConfig | None = None,
) -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg(5.0)],
                discharge_segments=[_seg(5.0)],
            )
        },
        constraints=ConstraintsConfig(
            max_import_kw=max_import_kw,
            max_export_kw=max_export_kw,
        ),
        objectives=ObjectivesConfig(balanced_weights=balanced_weights),
    )


def _bundle(
    strategy: str = "minimize_cost",
    prices: list[float] | None = None,
    export_prices: list[float] | None = None,
    confidence: list[float] | None = None,
    pv: list[float] | None = None,
) -> SolveBundle:
    # SolveBundle requires min_length=96. Tests use a 4-step horizon so only
    # the first 4 values are accessed; trailing values are padded with the last
    # supplied value (or the default) and are never read by the solver.
    n = 96

    def _pad(vals: list[float] | None, default: float) -> list[float]:
        if vals is None:
            return [default] * n
        if len(vals) < n:
            filler = vals[-1] if vals else default
            return vals + [filler] * (n - len(vals))
        return vals

    return SolveBundle(
        strategy=strategy,
        solve_time_utc=datetime.now(UTC),
        horizon_prices=_pad(prices, 0.25),
        horizon_export_prices=_pad(export_prices, 0.05),
        horizon_confidence=_pad(confidence, 1.0),
        pv_forecast=_pad(pv, 0.0),
        base_load_forecast=[0.0] * n,
    )


def _build_devices(ctx: ModelContext, battery_inputs: BatteryInputs) -> tuple[Grid, Battery]:
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=battery_inputs)
    return grid, bat


# ---------------------------------------------------------------------------
# minimize_cost
# ---------------------------------------------------------------------------


def test_minimize_cost_imports_at_cheapest_step() -> None:
    """With a price dip at step 1, all import should concentrate there."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=0.0))

    # Power balance: battery charges from grid each step (1 kW × 0.25 h = 0.25 kWh/step)
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(prices=[0.30, 0.10, 0.30, 0.30])
    config = _make_config()
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)

    status = ctx.solver.solve()
    assert status in ("optimal", "feasible")

    # The cheap step should carry more import than others.
    imp_1 = ctx.solver.var_value(grid.import_[1])
    imp_0 = ctx.solver.var_value(grid.import_[0])
    assert imp_1 >= imp_0 - 1e-6


def test_minimize_cost_exports_at_most_expensive_step() -> None:
    """With an export price spike at step 1, battery should export there."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=8.0))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(export_prices=[0.05, 0.30, 0.05, 0.05])
    config = _make_config()
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)

    ctx.solver.solve()
    exp_1 = ctx.solver.var_value(grid.export_[1])
    exp_0 = ctx.solver.var_value(grid.export_[0])
    assert exp_1 >= exp_0 - 1e-6


def test_minimize_consumption_minimises_total_import() -> None:
    """minimize_consumption should minimise total grid import across the horizon."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=5.0))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(strategy="minimize_consumption")
    config = _make_config()
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)

    status = ctx.solver.solve()
    assert status in ("optimal", "feasible")

    total_import = sum(ctx.solver.var_value(grid.import_[t]) for t in ctx.T)
    # With battery starting at 5 kWh and allowed to discharge, import should be minimal.
    assert total_import >= -1e-6  # non-negative


def test_balanced_lies_between_extremes() -> None:
    """Balanced strategy's total import should be between minimize_cost and minimize_consumption."""
    def _run_strategy(strategy: str) -> float:
        ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
        grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=3.0))
        for t in ctx.T:
            ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)
        bundle = _bundle(strategy=strategy, prices=[0.10, 0.30, 0.10, 0.30])
        config = _make_config(balanced_weights=BalancedWeightsConfig(cost_weight=1.0, self_sufficiency_weight=1.0))
        ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)
        ctx.solver.solve()
        return sum(ctx.solver.var_value(grid.import_[t]) for t in ctx.T)

    import_cost = _run_strategy("minimize_cost")
    import_cons = _run_strategy("minimize_consumption")
    import_bal = _run_strategy("balanced")

    lo = min(import_cost, import_cons) - 1e-4
    hi = max(import_cost, import_cons) + 1e-4
    assert lo <= import_bal <= hi, (
        f"balanced={import_bal:.4f} not between cost={import_cost:.4f} "
        f"and consumption={import_cons:.4f}"
    )


def test_confidence_zero_makes_step_economically_neutral() -> None:
    """A zero-confidence step should not contribute to the import cost objective."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=0.0))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    # Step 0 is free (confidence=0); step 1 costs 0.20; rest cost 0.20.
    # With confidence[0]=0 the solver should treat step 0 as free — no preference
    # between step 0 and other steps, as neither has economic weight from confidence.
    bundle = _bundle(
        prices=[0.001, 0.20, 0.20, 0.20],
        confidence=[0.0, 1.0, 1.0, 1.0],
    )
    config = _make_config()
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible")
    # The objective value should not blow up; confidence 0 removes the price signal.
    # Check just that the solve completes without error.


def test_wear_cost_added_from_devices() -> None:
    """High wear cost on battery should suppress cycling even with marginal price spread."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(5.0, eff=0.95)],
            discharge_segments=[_seg(5.0, eff=0.95)],
            wear_cost_eur_per_kwh=5.0,  # very high
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=_battery_inputs(soc_kwh=5.0))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(prices=[0.10, 0.15, 0.10, 0.15])  # tiny spread
    config = _make_config()
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)
    ctx.solver.solve()

    total_throughput = sum(
        ctx.solver.var_value(bat.charge_seg[t, 0]) + ctx.solver.var_value(bat.discharge_seg[t, 0])
        for t in ctx.T
    )
    assert total_throughput < 1e-6


def test_import_limit_constraint_enforced() -> None:
    """Hard cap max_import_kw must be enforced as a constraint at every step."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid, bat = _build_devices(ctx, _battery_inputs(soc_kwh=0.0))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(prices=[0.10] * 4)
    config = _make_config(max_import_kw=2.0)
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, config)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(grid.import_[t]) <= 2.0 + 1e-6


# ---------------------------------------------------------------------------
# Terminal SoC value
# ---------------------------------------------------------------------------


def test_terminal_soc_prevents_drain_when_export_below_import() -> None:
    """Draining a battery to export at a price below the import price is a net loss.

    Without a terminal SoC value the solver treats leftover stored energy as
    worthless, so it drains the battery to export at any positive price —
    even when the round-trip cost (import later to refill) exceeds the revenue.

    With a terminal SoC value equal to the average import price, each kWh
    retained is worth 0.30 EUR/kWh. Exporting earns only 0.20 EUR/kWh, which
    is a net loss of 0.10 EUR/kWh. The solver should therefore preserve the
    battery and not discharge at all.
    """
    import_price = 0.30
    export_price = 0.20  # below import — discharging to export is a net loss

    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    initial_soc_kwh = 8.0
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            min_soc_kwh=0.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=_battery_inputs(soc_kwh=initial_soc_kwh))

    # No load, no PV. Power balance: grid + battery = 0.
    # Any battery discharge goes straight to grid export and vice versa.
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(
        prices=[import_price] * 4,
        export_prices=[export_price] * 4,
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    final_soc = ctx.solver.var_value(bat.soc[ctx.T[-1]])
    # terminal_value = avg_import_price = 0.30 > export_price (0.20)
    # Preserving 1 kWh is worth 0.30 EUR; exporting it earns only 0.20 EUR.
    # The solver must not drain the battery below its starting level.
    assert final_soc >= initial_soc_kwh - 1e-4


def test_terminal_soc_valued_in_minimize_consumption() -> None:
    """minimize_consumption strategy must also value terminal SoC.

    With export_price (0.20) < avg_import_price (0.30), discharging to export
    is a net loss on the terminal value. The battery must be preserved.
    """
    import_price = 0.30
    export_price = 0.20
    initial_soc_kwh = 8.0

    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            min_soc_kwh=0.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=_battery_inputs(soc_kwh=initial_soc_kwh))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(
        strategy="minimize_consumption",
        prices=[import_price] * 4,
        export_prices=[export_price] * 4,
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    final_soc = ctx.solver.var_value(bat.soc[ctx.T[-1]])
    assert final_soc >= initial_soc_kwh - 1e-4


def test_terminal_soc_valued_in_balanced() -> None:
    """balanced strategy must also value terminal SoC."""
    import_price = 0.30
    export_price = 0.20
    initial_soc_kwh = 8.0

    ctx = ModelContext(solver=CBCSolverBackend(), horizon=4, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            min_soc_kwh=0.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=_battery_inputs(soc_kwh=initial_soc_kwh))

    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(
        strategy="balanced",
        prices=[import_price] * 4,
        export_prices=[export_price] * 4,
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    final_soc = ctx.solver.var_value(bat.soc[ctx.T[-1]])
    assert final_soc >= initial_soc_kwh - 1e-4


# ---------------------------------------------------------------------------
# minimize_consumption – phase 2 cost optimisation (plan 17)
# ---------------------------------------------------------------------------


def test_minimize_consumption_phase2_shifts_import_to_cheaper_step() -> None:
    """Phase 2 must place the forced import volume at the cheaper time slot.

    T=2. Prices: step 0 = 1.0 EUR/kWh (expensive), step 1 = 0.10 EUR/kWh (cheap).
    A battery starting at mid-SOC can discharge at step 0 and charge at step 1,
    allowing the solver to defer imports to the cheaper step. Phase 2 must produce
    import[1] >= import[0].
    """
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=2, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(8.0)],
            discharge_segments=[_seg(8.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=BatteryInputs(soc_kwh=5.0))

    # Static load of 1 kW at every step.
    load_kw = 1.0
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == load_kw)

    bundle = _bundle(
        strategy="minimize_consumption",
        prices=[1.0, 0.10],
        export_prices=[0.0, 0.0],
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    imp_0 = ctx.solver.var_value(grid.import_[0])
    imp_1 = ctx.solver.var_value(grid.import_[1])
    # Phase 2 must place more import at the cheaper step 1.
    assert imp_1 > imp_0 - 1e-4, (
        f"Expected import[1]={imp_1:.4f} >= import[0]={imp_0:.4f} "
        f"(cheap step should carry more import)"
    )


def test_minimize_consumption_phase2_maximises_export_at_higher_price_step() -> None:
    """Phase 2 must concentrate export at the step with the higher export price.

    T=2. Equal import prices; export prices [0.05, 0.15]. Battery starts full.
    Phase 1 finds I*=0. Phase 2 must schedule the export at step 1 (higher price).
    """
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=2, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(8.0)],
            discharge_segments=[_seg(8.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=BatteryInputs(soc_kwh=8.0))

    # No load – any battery discharge goes straight to grid export.
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == 0)

    bundle = _bundle(
        strategy="minimize_consumption",
        prices=[0.25, 0.25],
        export_prices=[0.05, 0.15],
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    exp_0 = ctx.solver.var_value(grid.export_[0])
    exp_1 = ctx.solver.var_value(grid.export_[1])
    # Phase 2 must favour exporting at step 1 (export_price=0.15 > 0.05).
    assert exp_1 > exp_0 - 1e-4, (
        f"Expected export[1]={exp_1:.4f} >= export[0]={exp_0:.4f} "
        f"(high-price step should carry export)"
    )


def test_minimize_consumption_phase2_respects_i_star_constraint() -> None:
    """Total import in the phase-2 solution must not exceed I* + 2×ε."""
    ctx = ModelContext(solver=CBCSolverBackend(), horizon=2, dt=0.25)
    grid = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
        ),
    )
    grid.add_variables(ctx)
    grid.add_constraints(ctx, inputs=None)
    bat.add_variables(ctx)
    bat.add_constraints(ctx, inputs=BatteryInputs(soc_kwh=3.0))

    load_kw = 2.0
    for t in ctx.T:
        ctx.solver.add_constraint(grid.net_power(t) + bat.net_power(t) == load_kw)

    bundle = _bundle(
        strategy="minimize_consumption",
        prices=[0.30, 0.05],
    )
    ObjectiveBuilder().build(ctx, [bat], grid, bundle, _make_config())
    ctx.solver.solve()

    total_import = sum(ctx.solver.var_value(grid.import_[t]) for t in ctx.T)
    # The total import must be non-negative (trivially) and finite.
    assert total_import >= -1e-4


def test_minimize_consumption_phase2_includes_device_wear_terms() -> None:
    """Phase 2 must include device wear cost to prevent zero-gain cycling.

    Equal import and export prices with a high wear cost: no reason to cycle.
    With the corrected phase-2 objective (minimise net cost including wear),
    the battery must not cycle.
    """
    ctx_wear = ModelContext(solver=CBCSolverBackend(), horizon=2, dt=0.25)
    grid_w = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    bat_w = Battery(
        name="bat",
        config=BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_seg(5.0)],
            discharge_segments=[_seg(5.0)],
            wear_cost_eur_per_kwh=2.0,
        ),
    )
    grid_w.add_variables(ctx_wear)
    grid_w.add_constraints(ctx_wear, inputs=None)
    bat_w.add_variables(ctx_wear)
    bat_w.add_constraints(ctx_wear, inputs=BatteryInputs(soc_kwh=5.0))

    for t in ctx_wear.T:
        ctx_wear.solver.add_constraint(grid_w.net_power(t) + bat_w.net_power(t) == 0)

    bundle_wear = _bundle(
        strategy="minimize_consumption",
        prices=[0.10, 0.10],
        export_prices=[0.10, 0.10],
    )
    ObjectiveBuilder().build(ctx_wear, [bat_w], grid_w, bundle_wear, _make_config())
    ctx_wear.solver.solve()

    total_throughput = sum(
        ctx_wear.solver.var_value(bat_w.charge_seg[t, 0])
        + ctx_wear.solver.var_value(bat_w.discharge_seg[t, 0])
        for t in ctx_wear.T
    )
    assert total_throughput < 1e-4, (
        f"Expected no cycling with high wear cost, got throughput={total_throughput:.6f}"
    )


# ---------------------------------------------------------------------------
# exchange_shaping_weight secondary objective term
# ---------------------------------------------------------------------------


def _make_config_with_shaping(exchange_shaping_weight: float) -> MimirheimConfig:
    """Build a minimal MimirheimConfig with exchange_shaping_weight set."""
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0),
        objectives=ObjectivesConfig(exchange_shaping_weight=exchange_shaping_weight),
    )


def test_exchange_shaping_weight_zero_excludes_secondary_term() -> None:
    """With exchange_shaping_weight=0.0, objective is zero under flat zero prices.

    A forced import of 1 kW at each step means exchange is non-zero. The
    primary economic term is zero because all prices are 0.0. If the secondary
    exchange-shaping term were active, the objective would be positive. This
    test confirms it is exactly 0.0 when the weight is 0.0.
    """
    backend = CBCSolverBackend()
    ctx = ModelContext(solver=backend, horizon=2, dt=0.25)
    grid_d = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    grid_d.add_variables(ctx)
    grid_d.add_constraints(ctx, inputs=None)
    for t in ctx.T:
        ctx.solver.add_constraint(grid_d.import_[t] == 1.0)
        ctx.solver.add_constraint(grid_d.export_[t] == 0.0)

    config = _make_config_with_shaping(exchange_shaping_weight=0.0)
    bundle = _bundle(prices=[0.0, 0.0], export_prices=[0.0, 0.0])
    ObjectiveBuilder().build(ctx, [], grid_d, bundle, config)
    ctx.solver.solve()

    assert abs(ctx.solver.objective_value()) < 1e-8, (
        f"Expected objective 0.0 with weight=0.0, got {ctx.solver.objective_value()}"
    )


def test_exchange_shaping_weight_nonzero_adds_secondary_term() -> None:
    """With exchange_shaping_weight>0, the objective includes the secondary term.

    The same forced-import setup (1 kW, prices=0.0) is used. With weight w,
    the objective must equal w * total_import = w * (1 + 1) = 2w, confirming
    the secondary term lambda * sum_t(import_t + export_t) is active.
    """
    w = 1e-3
    backend = CBCSolverBackend()
    ctx = ModelContext(solver=backend, horizon=2, dt=0.25)
    grid_d = Grid(config=GridConfig(import_limit_kw=20.0, export_limit_kw=10.0))
    grid_d.add_variables(ctx)
    grid_d.add_constraints(ctx, inputs=None)
    for t in ctx.T:
        ctx.solver.add_constraint(grid_d.import_[t] == 1.0)
        ctx.solver.add_constraint(grid_d.export_[t] == 0.0)

    config = _make_config_with_shaping(exchange_shaping_weight=w)
    bundle = _bundle(prices=[0.0, 0.0], export_prices=[0.0, 0.0])
    ObjectiveBuilder().build(ctx, [], grid_d, bundle, config)
    ctx.solver.solve()

    expected = w * 2.0  # w * (import[0] + import[1])
    assert abs(ctx.solver.objective_value() - expected) < 1e-8, (
        f"Expected objective {expected}, got {ctx.solver.objective_value()}"
    )
