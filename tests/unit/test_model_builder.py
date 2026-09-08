"""Unit tests for mimirheim/core/model_builder.py — naive and optimised cost helpers.

Tests call _compute_naive_cost and _compute_optimised_cost directly without
a solver. All tests must fail before the implementation exists (TDD).

The exception is the staged-array baseline test at the end, which goes through
build_and_solve: what it guards is the ceiling model_builder picks per array,
which is not visible from _compute_naive_cost alone.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeviceSetpoint, SolveBundle
from mimirheim.core.model_builder import _compute_naive_cost, build_and_solve


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


# ---------------------------------------------------------------------------
# DeviceSetpoint.soc_kwh field
# ---------------------------------------------------------------------------


def test_device_setpoint_soc_kwh_defaults_to_none() -> None:
    """DeviceSetpoint.soc_kwh is None when not provided (non-storage devices)."""
    sp = DeviceSetpoint(kw=1.0, type="static_load")
    assert sp.soc_kwh is None


def test_device_setpoint_soc_kwh_accepts_float() -> None:
    """DeviceSetpoint.soc_kwh stores the provided float for storage devices."""
    sp = DeviceSetpoint(kw=-1.5, type="battery", soc_kwh=5.5)
    assert sp.soc_kwh == 5.5


def test_naive_cost_uses_the_clipped_pv_series_when_given_one() -> None:
    """The baseline must not be credited with PV the arrays cannot produce.

    build_and_solve hands the devices the raw per-array series, which they
    clip to max_power_kw themselves, and separately clips its own copy to
    each array's max_deliverable_kw before calling this function. Both paths
    therefore work under the same physical limits even though they arrive
    there by different routes. Comparing an optimised plan built on 5 kW
    against a baseline built on the raw 8 kW forecast would understate the
    saving the optimiser found.
    """
    bundle = _bundle(
        import_prices=[0.25, 0.25],
        export_prices=[0.0, 0.0],
        pv_forecast=[8.0, 8.0],
        base_load_forecast=[8.0, 8.0],
    )
    # Unclipped: PV covers the load exactly, so the naive cost is zero.
    assert _compute_naive_cost(bundle, horizon=2, dt=0.25) == 0.0

    # Clipped to a 5 kW array: 3 kW is imported each step.
    # 3 kW x 0.25 h x 0.25 EUR/kWh = 0.1875 EUR per step.
    clipped = _compute_naive_cost(bundle, horizon=2, dt=0.25, pv_forecast_kw=[5.0, 5.0])
    assert abs(clipped - 0.375) < 1e-9


def test_naive_cost_falls_back_to_the_bundle_series() -> None:
    """A caller with no configured PV array passes None and gets the old behaviour."""
    bundle = _bundle(
        import_prices=[0.25, 0.25],
        export_prices=[0.0, 0.0],
        pv_forecast=[2.0, 2.0],
        base_load_forecast=[4.0, 4.0],
    )
    assert _compute_naive_cost(bundle, horizon=2, dt=0.25) == _compute_naive_cost(
        bundle, horizon=2, dt=0.25, pv_forecast_kw=None
    )


def test_naive_cost_of_a_staged_array_stops_at_the_highest_register() -> None:
    """End to end: the baseline may not exceed what a staged inverter can deliver.

    max_power_kw is 10.0 but the highest register is 5.0, which the schema
    permits. The forecast is 10.0 kW against a 8.0 kW load, so the deliverable
    5.0 kW leaves 3.0 kW to import each step:
    3.0 kW x 0.25 h x 0.25 EUR/kWh x 4 steps = 0.75 EUR.

    Clipping the baseline to max_power_kw instead would have PV cover the load
    outright and report an export credit. This asserts through build_and_solve
    so that swapping max_deliverable_kw back for max_power_kw fails here.
    """
    horizon = 4
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "pv_arrays": {
                "roof": {"max_power_kw": 10.0, "production_stages": [0.0, 5.0]},
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[10.0] * horizon,
        base_load_forecast=[8.0] * horizon,
        pv_forecasts={"roof": [10.0] * horizon},
    )

    result = build_and_solve(bundle, config)
    assert result.naive_cost_eur == pytest.approx(0.75, abs=1e-9)
