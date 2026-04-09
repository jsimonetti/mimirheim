"""Unit tests for mimirheim/core/model_builder.py — naive and optimised cost helpers.

Tests call _compute_naive_cost and _compute_optimised_cost directly without
a solver. All tests must fail before the implementation exists (TDD).
"""

from datetime import datetime, timezone

from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import _compute_naive_cost


def _bundle(
    *,
    import_prices: list[float],
    export_prices: list[float],
    pv_forecast: list[float],
    base_load_forecast: list[float],
) -> SolveBundle:
    horizon = len(import_prices)
    return SolveBundle(
        solve_time_utc=datetime(2024, 1, 1, 12, tzinfo=timezone.utc),
        horizon_prices=import_prices,
        horizon_export_prices=export_prices,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=pv_forecast,
        base_load_forecast=base_load_forecast,
    )


def test_naive_cost_no_pv() -> None:
    """base_load=4 kW for 4 steps, no PV, import_price=0.25, dt=0.25. Expected 1.0 EUR.

    Per step: 4 kW × 0.25 h × 0.25 EUR/kWh = 0.25 EUR. Four steps = 1.0 EUR.
    """
    bundle = _bundle(
        import_prices=[0.25, 0.25, 0.25, 0.25],
        export_prices=[0.0, 0.0, 0.0, 0.0],
        pv_forecast=[0.0, 0.0, 0.0, 0.0],
        base_load_forecast=[4.0, 4.0, 4.0, 4.0],
    )
    result = _compute_naive_cost(bundle, horizon=4, dt=0.25)
    assert abs(result - 1.0) < 1e-9


def test_naive_cost_pv_exactly_covers_load() -> None:
    """When PV equals load at every step, no import or export — cost must be 0."""
    bundle = _bundle(
        import_prices=[0.25, 0.25, 0.25, 0.25],
        export_prices=[0.10, 0.10, 0.10, 0.10],
        pv_forecast=[3.0, 3.0, 3.0, 3.0],
        base_load_forecast=[3.0, 3.0, 3.0, 3.0],
    )
    result = _compute_naive_cost(bundle, horizon=4, dt=0.25)
    assert result == 0.0


def test_naive_cost_pv_surplus_credits_export_revenue() -> None:
    """Step 0: base_load=1, pv=3, export_price=0.08. Expected −0.04 EUR (revenue)."""
    bundle = _bundle(
        import_prices=[0.25],
        export_prices=[0.08],
        pv_forecast=[3.0],
        base_load_forecast=[1.0],
    )
    # net = 1 - 3 = -2 kW (exporting). contribution = -2 * 0.08 * 0.25 = -0.04 EUR.
    result = _compute_naive_cost(bundle, horizon=1, dt=0.25)
    assert result < 0, f"Expected negative cost (export revenue), got {result}"
    assert abs(result - (-0.04)) < 1e-9


def test_naive_cost_negative_export_price_adds_to_cost() -> None:
    """Step 0: base_load=0, pv=4, export_price=-0.02. Expected +0.02 EUR (cost to export)."""
    bundle = _bundle(
        import_prices=[0.25],
        export_prices=[-0.02],
        pv_forecast=[4.0],
        base_load_forecast=[0.0],
    )
    # net = 0 - 4 = -4 kW. contribution = -4 * -0.02 * 0.25 = +0.02 EUR.
    result = _compute_naive_cost(bundle, horizon=1, dt=0.25)
    assert result > 0, f"Expected positive cost (negative export price), got {result}"
    assert abs(result - 0.02) < 1e-9


def test_naive_cost_mixed_steps() -> None:
    """Step 0: surplus (pv > load). Step 1: deficit (load > pv). Total equals sum."""
    bundle = _bundle(
        import_prices=[0.25, 0.30],
        export_prices=[0.08, 0.08],
        pv_forecast=[5.0, 1.0],
        base_load_forecast=[2.0, 4.0],
    )
    # Step 0: net = 2 - 5 = -3 kW, export. contribution = -3 * 0.08 * 0.25 = -0.06 EUR.
    # Step 1: net = 4 - 1 = +3 kW, import. contribution = 3 * 0.30 * 0.25 = +0.225 EUR.
    expected = -0.06 + 0.225
    result = _compute_naive_cost(bundle, horizon=2, dt=0.25)
    assert abs(result - expected) < 1e-9


def test_naive_cost_does_not_use_old_max_zero_clip() -> None:
    """The old formula max(0, base_load - pv) clips surplus to zero (no export credit).

    With the corrected formula, a surplus scenario produces a negative cost.
    The old formula would produce 0.0 for this input. Assert the result differs.
    """
    bundle = _bundle(
        import_prices=[0.25],
        export_prices=[0.10],
        pv_forecast=[6.0],
        base_load_forecast=[2.0],
    )
    result = _compute_naive_cost(bundle, horizon=1, dt=0.25)
    old_formula_result = 0.0  # max(0, 2 - 6) * 0.25 * 0.25 = 0
    assert result != old_formula_result, (
        "naive_cost used the old max(0, ...) clip: result is 0 but should be negative"
    )
    assert result < 0
