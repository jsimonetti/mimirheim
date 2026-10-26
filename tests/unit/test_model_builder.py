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
from mimirheim.core.bundle import (
    BatteryInputs,
    DeviceSetpoint,
    EvInputs,
    HybridInverterInputs,
    ScheduleStep,
    SolveBundle,
    SolveResult,
)
from mimirheim.core.model_builder import (
    _baseline_import_kwh,
    _compute_naive_cost,
    _compute_soc_credit,
    build_and_solve,
)


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


def _hybrid_naive_cost(*, hybrid_pv_kw: float, roof_pv_kw: float | None = None) -> float:
    """Solve four steps of a 3 kW load with one hybrid inverter and return
    the naive baseline. Import 0.25 EUR/kWh, export 0.10 EUR/kWh.

    The hybrid's panels peak at 4 kW behind a 0.96 inverter. A plain 1 kW
    roof array is added when ``roof_pv_kw`` is given. The hybrid's forecast
    runs two steps past the horizon, as a published one may: only the
    horizon's steps may count.
    """
    horizon = 4
    raw = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
        "hybrid_inverters": {
            "hi": {
                "capacity_kwh": 10.0,
                "max_charge_kw": 4.0,
                "max_discharge_kw": 4.0,
                "max_pv_kw": 4.0,
                "inverter_efficiency": 0.96,
            },
        },
        "static_loads": {"base": {}},
    }
    pv_forecasts = {}
    if roof_pv_kw is not None:
        raw["pv_arrays"] = {"roof": {"max_power_kw": 1.0}}
        pv_forecasts = {"roof": [roof_pv_kw] * horizon}
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[roof_pv_kw or 0.0] * horizon,
        base_load_forecast=[3.0] * horizon,
        pv_forecasts=pv_forecasts,
        hybrid_inverter_inputs={
            "hi": HybridInverterInputs(soc_kwh=5.0, pv_forecast_kw=[hybrid_pv_kw] * (horizon + 2)),
        },
    )
    return build_and_solve(bundle, MimirheimConfig.model_validate(raw)).naive_cost_eur


def test_naive_cost_counts_a_hybrid_inverters_own_panels() -> None:
    """Without storage dispatch a hybrid's panels still feed the house.

    2.0 kW of panels through the 0.96 inverter cover 1.92 kW of the 3.0 kW
    load, leaving 1.08 kW to import each step:
    1.08 kW x 0.25 h x 0.25 EUR/kWh x 4 steps = 0.27 EUR.

    Leaving the panels out would price the whole load, 0.75 EUR, and credit
    the plan with saving what the panels produce on their own.
    """
    assert _hybrid_naive_cost(hybrid_pv_kw=2.0) == pytest.approx(0.27, abs=1e-9)


def test_naive_cost_clips_a_hybrid_inverters_panels_to_max_pv_kw() -> None:
    """A forecast above max_pv_kw delivers only max_pv_kw, after the inverter.

    6.0 kW forecast against a 4.0 kW MPPT: 3.84 kW AC against a 3.0 kW load
    exports 0.84 kW each step at 0.10 EUR/kWh:
    -0.84 kW x 0.25 h x 0.10 EUR/kWh x 4 steps = -0.084 EUR.
    """
    assert _hybrid_naive_cost(hybrid_pv_kw=6.0) == pytest.approx(-0.084, abs=1e-9)


def test_naive_cost_adds_a_hybrid_inverters_panels_to_the_arrays() -> None:
    """Array and hybrid panels add up, each clipped to its own ceiling.

    Roof forecast 1.5 kW on a 1.0 kW array, hybrid 1.0 kW through 0.96:
    3.0 - 1.0 - 0.96 = 1.04 kW import, x 0.25 h x 0.25 EUR/kWh x 4 = 0.26 EUR.
    """
    assert _hybrid_naive_cost(hybrid_pv_kw=1.0, roof_pv_kw=1.5) == pytest.approx(0.26, abs=1e-9)


def test_a_degraded_objective_is_reported_on_the_result() -> None:
    """build_and_solve must copy the builder's flag onto the SolveResult.

    ObjectiveBuilder records the degradation; the result is what gets
    published. A flag that stays on the builder is never seen by anyone.
    """
    from unittest.mock import patch

    from mimirheim.core import model_builder
    from mimirheim.core.objective import ObjectiveBuilder

    class _Degrading(ObjectiveBuilder):
        def build(self, *args, **kwargs):
            budget = super().build(*args, **kwargs)
            self.strategy_degraded = True
            return budget

    horizon = 4
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "static_loads": {"base": {}},
        }
    )
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[1.0] * horizon,
    )

    assert build_and_solve(bundle, config).strategy_degraded is False
    with patch.object(model_builder, "ObjectiveBuilder", _Degrading):
        assert build_and_solve(bundle, config).strategy_degraded is True


def _hybrid_solve(*, start_soc_kwh: float, pv_kw: float, load_kw: float) -> SolveResult:
    """Solve four flat-priced steps with one hybrid inverter and a static load."""
    horizon = 4
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "hybrid_inverters": {
                "hi": {
                    "capacity_kwh": 10.0,
                    "max_charge_kw": 4.0,
                    "max_discharge_kw": 4.0,
                    "max_pv_kw": 4.0,
                    "battery_charge_efficiency": 0.95,
                    "battery_discharge_efficiency": 0.9,
                    "inverter_efficiency": 0.96,
                },
            },
            "static_loads": {"base": {}},
        }
    )
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.0] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[load_kw] * horizon,
        hybrid_inverter_inputs={
            "hi": HybridInverterInputs(soc_kwh=start_soc_kwh, pv_forecast_kw=[pv_kw] * horizon),
        },
    )
    return build_and_solve(bundle, config)


def test_soc_credit_counts_a_hybrid_inverter_charging_from_its_own_panels() -> None:
    """Energy a hybrid inverter stores over the horizon is credited, like a battery's.

    Export pays nothing and there is no load, so the panels' 4 kW can only go
    into the cell: the SOC rises and the credit must be positive. It is valued
    on the way out, cell to AC: avg import price x SOC change x discharge
    efficiency x inverter efficiency.
    """
    result = _hybrid_solve(start_soc_kwh=2.0, pv_kw=4.0, load_kw=0.0)

    end_soc = result.schedule[-1].devices["hi"].soc_kwh
    assert end_soc > 2.0
    assert result.soc_credit_eur > 0.0
    assert result.soc_credit_eur == pytest.approx(0.25 * (end_soc - 2.0) * 0.9 * 0.96)


def test_soc_credit_of_a_hybrid_inverter_follows_the_cell_not_the_ac_side() -> None:
    """Solar passed straight through to the house is not energy stored.

    With the cell full, the panels feed the load through the inverter: the
    hybrid's AC kW is positive at every step while its SOC does not move. A
    credit summed from AC kW, as for a plain battery, would come out strongly
    negative; reading the SOC it must stay at the value of the SOC change.
    """
    result = _hybrid_solve(start_soc_kwh=10.0, pv_kw=2.0, load_kw=1.5)

    assert all(step.devices["hi"].kw > 0.0 for step in result.schedule)
    end_soc = result.schedule[-1].devices["hi"].soc_kwh
    assert result.soc_credit_eur == pytest.approx(0.25 * (end_soc - 10.0) * 0.9 * 0.96)


def test_soc_credit_skips_a_hybrid_inverter_it_cannot_value() -> None:
    """No terminal SOC, no step for the device, or no config for the inputs:
    no credit, and no error.

    ``soc_kwh`` is optional on ``DeviceSetpoint`` and schedules from older
    builds do not carry it for hybrid inverters.
    """
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "hybrid_inverters": {
                name: {
                    "capacity_kwh": 10.0,
                    "max_charge_kw": 4.0,
                    "max_discharge_kw": 4.0,
                    "max_pv_kw": 4.0,
                }
                for name in ("hi", "absent")
            },
        }
    )
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25],
        horizon_export_prices=[0.0],
        horizon_confidence=[1.0],
        pv_forecast=[0.0],
        base_load_forecast=[0.0],
        hybrid_inverter_inputs={
            "hi": HybridInverterInputs(soc_kwh=2.0, pv_forecast_kw=[0.0]),
            "absent": HybridInverterInputs(soc_kwh=2.0, pv_forecast_kw=[0.0]),
            "unconfigured": HybridInverterInputs(soc_kwh=2.0, pv_forecast_kw=[0.0]),
        },
    )
    schedule = [
        ScheduleStep(
            t=0,
            grid_import_kw=0.0,
            grid_export_kw=0.0,
            devices={
                "hi": DeviceSetpoint(kw=-2.0, type="hybrid_inverter"),
                "unconfigured": DeviceSetpoint(kw=-2.0, type="hybrid_inverter", soc_kwh=2.5),
            },
        )
    ]

    assert _compute_soc_credit(bundle, schedule, config) == 0.0


# ---------------------------------------------------------------------------
# soc_credit_eur is measured at the cell for every storage device
# ---------------------------------------------------------------------------


def _storage_config() -> MimirheimConfig:
    """One battery and one V2H EV charger, both charging at 95%.

    The two discharge efficiencies differ so that a loop reading the wrong
    device's config, or the two being swapped, changes the result.
    """
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "bat": {
                    "capacity_kwh": 10.0,
                    "charge_segments": [{"power_max_kw": 4.0, "efficiency": 0.95}],
                    "discharge_segments": [{"power_max_kw": 4.0, "efficiency": 0.9}],
                }
            },
            "ev_chargers": {
                "ev": {
                    "capacity_kwh": 40.0,
                    "charge_segments": [{"power_max_kw": 4.0, "efficiency": 0.95}],
                    "discharge_segments": [{"power_max_kw": 4.0, "efficiency": 0.85}],
                }
            },
        }
    )


# Discharge efficiency per device under test, matching _storage_config.
_STORAGE_CASES = [("bat", "battery", 0.9), ("ev", "ev_charger", 0.85)]


def _storage_bundle() -> SolveBundle:
    """A single step at 0.25 EUR/kWh with both storage devices at 2 kWh."""
    return SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25],
        horizon_export_prices=[0.0],
        horizon_confidence=[1.0],
        pv_forecast=[0.0],
        base_load_forecast=[0.0],
        battery_inputs={"bat": BatteryInputs(soc_kwh=2.0)},
        ev_inputs={"ev": EvInputs(soc_kwh=2.0, available=True)},
    )


def _one_step(name: str, device_type: str, *, kw: float, soc_kwh: float) -> list[ScheduleStep]:
    """A one-step schedule carrying a single storage device's AC power and SOC."""
    return [
        ScheduleStep(
            t=0,
            # Grid flows are irrelevant to the credit, which reads only SOC.
            grid_import_kw=max(-kw, 0.0),
            grid_export_kw=0.0,
            devices={name: DeviceSetpoint(kw=kw, type=device_type, soc_kwh=soc_kwh)},
        )
    ]


@pytest.mark.parametrize(("name", "device_type", "discharge_eff"), _STORAGE_CASES)
def test_soc_credit_of_absorbed_energy_is_measured_at_the_cell(
    name: str, device_type: str, discharge_eff: float
) -> None:
    """Charging losses must not be credited as stored energy.

    The device absorbs 4 kW for a quarter hour, so 1.0 kWh crosses its AC
    terminals but only 0.95 kWh reaches the cell. Valuing the AC figure
    credits energy that was lost as heat in the charger and never existed in
    the cell to be discharged later.
    """
    credit = _compute_soc_credit(
        _storage_bundle(),
        _one_step(name, device_type, kw=-4.0, soc_kwh=2.95),
        _storage_config(),
    )

    # 0.95 kWh gained at the cell, worth discharge_eff x that at the AC
    # terminals on the way back out, valued at the average import price.
    assert credit == pytest.approx(0.25 * 0.95 * discharge_eff)


@pytest.mark.parametrize(("name", "device_type", "discharge_eff"), _STORAGE_CASES)
def test_soc_credit_of_delivered_energy_is_measured_at_the_cell(
    name: str, device_type: str, discharge_eff: float
) -> None:
    """Energy spent from the cell is debited in full, not net of discharge loss.

    The device delivers 3.6 kW for a quarter hour, which is 0.9 kWh of AC. The
    cell gave up 1.0 kWh to produce it. Charging the AC figure understates the
    loss, so the horizon looks cheaper than it was.
    """
    credit = _compute_soc_credit(
        _storage_bundle(),
        _one_step(name, device_type, kw=3.6, soc_kwh=1.0),
        _storage_config(),
    )

    # 1.0 kWh drained from the cell, worth discharge_eff kWh of AC.
    assert credit == pytest.approx(-0.25 * 1.0 * discharge_eff)


def test_soc_credit_skips_an_unavailable_ev() -> None:
    """An unplugged EV's soc[t] is a free variable, not its real SOC.

    The availability gate forces the power variables to zero and returns
    without adding a SOC balance constraint, so the schedule reports the
    variable's lower bound. Valuing that against the real initial SOC would
    debit the whole pack as though it had been emptied. Under the superseded
    AC-side formula the guard was redundant, because the AC power was pinned
    to zero; it is now the only thing preventing a large wrong figure.
    """
    bundle = _storage_bundle().model_copy(
        update={"ev_inputs": {"ev": EvInputs(soc_kwh=20.0, available=False)}}
    )

    credit = _compute_soc_credit(
        bundle,
        _one_step("ev", "ev_charger", kw=0.0, soc_kwh=0.0),
        _storage_config(),
    )

    assert credit == 0.0


def test_soc_credit_of_a_battery_follows_the_solver_soc() -> None:
    """End to end: the credit is derived from the SOC the solver reported.

    Prices fall across the horizon and nothing consumes power, so the battery
    charges in the cheap steps and ends fuller than it started. Asserting
    through build_and_solve proves soc_kwh is populated on the real path, not
    only in hand-built schedules.
    """
    horizon = 4
    config = MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
            "batteries": {
                "bat": {
                    "capacity_kwh": 10.0,
                    "charge_segments": [{"power_max_kw": 4.0, "efficiency": 0.95}],
                    "discharge_segments": [{"power_max_kw": 4.0, "efficiency": 0.9}],
                }
            },
        }
    )
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.05, 0.05, 0.40, 0.40],
        horizon_export_prices=[0.0] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=[0.0] * horizon,
        base_load_forecast=[2.0] * horizon,
        battery_inputs={"bat": BatteryInputs(soc_kwh=2.0)},
    )

    result = build_and_solve(bundle, config)

    end_soc = result.schedule[-1].devices["bat"].soc_kwh
    assert end_soc is not None
    # Without this the solver leaving the battery alone would make both sides
    # of the assertion below zero, passing green while proving nothing.
    assert end_soc > 2.0
    avg_price = sum(bundle.horizon_prices) / horizon
    assert result.soc_credit_eur == pytest.approx(avg_price * (end_soc - 2.0) * 0.9)



# ---------------------------------------------------------------------------
# Baseline import
# ---------------------------------------------------------------------------


def test_baseline_import_is_the_load_shortfall_in_kwh() -> None:
    """Baseline import is max(0, load - clipped PV) * dt per step.

    Steps where PV covers the load contribute zero, not a negative import.
    """
    baseline = _baseline_import_kwh(
        base_load_kw=[2.0, 1.0, 0.5, 0.0],
        clipped_pv_kw=[0.0, 1.0, 1.5, 0.0],
        dt=0.25,
    )

    assert baseline == pytest.approx([0.5, 0.0, 0.0, 0.0])
