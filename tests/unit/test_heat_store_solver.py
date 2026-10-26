"""Solver-level tests for heat stores.

These tests build real models through ``build_and_solve`` and assert on the
published schedule: when the store boosts, how much it draws, and how the
planned temperature evolves. They never inspect variable names or constraint
counts, so they hold for any formulation that produces the specified plan.

The default store is a 200 L DHW heat pump boiler with one boost stage that
lets the heat pump heat 5 K above its normal setpoint:

    C = 0.233 kWh/K, normal_cop = 2.5, normal_setpoint_c = 50, ambient_c = 20,
    drift = 0.4 K/h, stage 1: 0.5 kW at COP 2.5 up to 55 degrees C.

Its full boost stores 0.233 * 5 = 1.165 kWh of heat above the normal setpoint,
which the normal program's maintenance loss of 0.233 * 0.4 * 0.25 = 0.0233 kWh
per step takes 50 steps to use up.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve

_T0 = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
_HORIZON = 96
_C = 0.233
_SETPOINT = 50.0
_CAP = 55.0
_STAGE_KW = 0.5


def _store(**overrides: object) -> dict:
    store: dict = {
        "heat_capacity_kwh_per_k": _C,
        "normal_cop": 2.5,
        "normal_setpoint_c": _SETPOINT,
        "ambient_c": 20.0,
        "drift_k_per_hour": 0.4,
        "boost_stages": [{"power_kw": _STAGE_KW, "cop": 2.5, "max_temp_c": _CAP}],
    }
    store.update(overrides)
    return store


def _config(*, pv: bool = False, hybrid: bool = False, **store: object) -> MimirheimConfig:
    raw: dict = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
        "static_loads": {"base": {}},
        "heat_stores": {"boiler": _store(**store)},
    }
    if pv:
        raw["pv_arrays"] = {"roof": {"max_power_kw": 10.0}}
    if hybrid:
        raw["hybrid_inverters"] = {
            "hybrid": {
                "capacity_kwh": 5.0,
                "max_charge_kw": 0.0001,
                "max_discharge_kw": 0.0001,
                "max_pv_kw": 10.0,
            }
        }
    return MimirheimConfig.model_validate(raw)


def _bundle(
    *,
    prices: list[float],
    export_prices: list[float] | None = None,
    temp_c: float = _SETPOINT,
    load_kw: float = 0.5,
    pv_kw: list[float] | None = None,
    hybrid_pv_kw: list[float] | None = None,
    strategy: str = "minimize_cost",
) -> SolveBundle:
    horizon = len(prices)
    pv = pv_kw if pv_kw is not None else [0.0] * horizon
    data: dict = {
        "strategy": strategy,
        "solve_time_utc": _T0,
        "horizon_prices": prices,
        "horizon_export_prices": (
            export_prices if export_prices is not None else [p - 0.05 for p in prices]
        ),
        "horizon_confidence": [1.0] * horizon,
        "pv_forecast": pv,
        "base_load_forecast": [load_kw] * horizon,
        "heat_store_inputs": {"boiler": {"current_temp_c": temp_c}},
    }
    if pv_kw is not None:
        data["pv_forecasts"] = {"roof": pv}
    if hybrid_pv_kw is not None:
        data["hybrid_inverter_inputs"] = {
            "hybrid": {"soc_kwh": 2.5, "pv_forecast_kw": hybrid_pv_kw}
        }
    return SolveBundle.model_validate(data)


def _stages(result: SolveResult) -> list[int]:
    return [step.devices["boiler"].boost_stage for step in result.schedule]


def _draw_kw(result: SolveResult) -> list[float]:
    """Electrical draw per step in kW. net_power is negative for a consumer."""
    return [-step.devices["boiler"].kw for step in result.schedule]


def _temps(result: SolveResult) -> list[float]:
    return [step.devices["boiler"].temp_c for step in result.schedule]


# ---------------------------------------------------------------------------
# Flat prices never boost
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("strategy", ["minimize_cost", "balanced"])
def test_flat_prices_never_boost(strategy: str) -> None:
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, strategy=strategy), _config()
    )

    assert result.solve_status in ("optimal", "feasible")
    assert _stages(result) == [0] * _HORIZON
    assert _draw_kw(result) == pytest.approx([0.0] * _HORIZON, abs=1e-6)


# ---------------------------------------------------------------------------
# Price spread
# ---------------------------------------------------------------------------


def _spread_prices() -> list[float]:
    """Eight cheap steps, then expensive for the rest of the horizon."""
    return [0.05] * 8 + [0.40] * (_HORIZON - 8)


def test_price_spread_boosts_before_the_expensive_period() -> None:
    result = build_and_solve(_bundle(prices=_spread_prices()), _config())

    stages = _stages(result)
    assert any(stage == 1 for stage in stages[:8])
    assert all(stage == 0 for stage in stages[8:])
    # The store is filled to its stage cap by the end of the cheap period.
    assert _temps(result)[7] == pytest.approx(_CAP, abs=1e-4)


def test_draw_is_full_stage_power_except_where_the_store_ends_at_its_cap() -> None:
    """A store close to its cap has room for less than one full step.

    At 54 degrees C the store holds 0.932 kWh and has room for 0.233 kWh plus
    one step's losses, less than the 0.3125 kWh one full-power step delivers.
    A boost before the expensive period therefore reaches the cap in a partial
    step.
    """
    result = build_and_solve(_bundle(prices=_spread_prices(), temp_c=54.0), _config())

    partial_steps = 0
    for stage, draw, temp in zip(
        _stages(result), _draw_kw(result), _temps(result), strict=True
    ):
        if stage == 0:
            assert draw == pytest.approx(0.0, abs=1e-6)
        elif draw < _STAGE_KW - 1e-6:
            partial_steps += 1
            assert temp == pytest.approx(_CAP, abs=1e-4)
        else:
            assert draw == pytest.approx(_STAGE_KW, abs=1e-6)
    assert partial_steps >= 1


def test_planned_temperature_starts_from_the_measurement_and_decays() -> None:
    """With no boost, a store above its setpoint cools back towards it."""
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, temp_c=53.0), _config()
    )

    temps = _temps(result)
    assert temps[0] < 53.0
    assert all(later <= earlier + 1e-9 for earlier, later in zip(temps, temps[1:], strict=False))
    assert temps[-1] >= _SETPOINT - 1e-9


def test_temperature_below_setpoint_counts_as_no_stored_heat() -> None:
    result = build_and_solve(
        _bundle(prices=[0.25] * _HORIZON, temp_c=42.0), _config()
    )

    assert _temps(result) == pytest.approx([_SETPOINT] * _HORIZON, abs=1e-6)


def test_temperature_above_the_cap_is_clamped() -> None:
    """A reading above the highest cap counts as a full store, not more."""
    hot = build_and_solve(_bundle(prices=[0.25] * _HORIZON, temp_c=70.0), _config())
    full = build_and_solve(_bundle(prices=[0.25] * _HORIZON, temp_c=_CAP), _config())

    assert _temps(hot) == pytest.approx(_temps(full), abs=1e-6)
    assert _temps(hot)[0] <= _CAP


# ---------------------------------------------------------------------------
# Negative prices
# ---------------------------------------------------------------------------


def test_negative_prices_fill_the_store_then_hold_at_loss_replacement_draw() -> None:
    negative = 16
    prices = [-0.10] * negative + [0.30] * (_HORIZON - negative)
    result = build_and_solve(
        _bundle(prices=prices, export_prices=[p - 0.05 for p in prices]), _config()
    )

    temps = _temps(result)
    draws = _draw_kw(result)
    first_full = next(t for t, temp in enumerate(temps) if temp >= _CAP - 1e-4)
    assert first_full < negative

    for t in range(first_full + 1, negative):
        # Held at the cap: boosting, but only replacing what the store loses.
        assert temps[t] == pytest.approx(_CAP, abs=1e-4)
        assert _stages(result)[t] == 1
        assert 0.0 < draws[t] < 0.2 * _STAGE_KW


# ---------------------------------------------------------------------------
# The credit for displaced heating
# ---------------------------------------------------------------------------


def test_stored_heat_is_used_at_its_physical_rate_at_negative_export_prices() -> None:
    """Heat already in the store is used as it physically would be.

    For the first 16 steps PV exactly covers the base load and export prices are
    negative, so the heating stored heat displaces would have run on PV worth
    less than nothing and the clamped credit is zero. The plan must still use
    the stored heat in those steps: holding it for the evening, when the credit
    is positive, would pretend the store keeps heat it is losing.
    """
    surplus = 16
    pv = [0.5] * surplus + [0.0] * (_HORIZON - surplus)
    exports = [-0.05] * surplus + [0.10] * (_HORIZON - surplus)
    cfg = _config(pv=True)
    full = build_and_solve(
        _bundle(prices=[0.30] * _HORIZON, export_prices=exports, temp_c=_CAP, pv_kw=pv),
        cfg,
    )
    assert all(stage == 0 for stage in _stages(full))

    # Physical cooldown with no boost: S[t] = (1 - d) * S[t-1] - maint, so the
    # temperature drops by at least drift * dt = 0.1 K per step.
    temps = _temps(full)
    previous = _CAP
    for t in range(surplus):
        assert temps[t] <= previous - 0.1 + 1e-6
        previous = temps[t]


def test_displaced_heating_on_pv_surplus_is_valued_at_export_price() -> None:
    """A midday boost on surplus PV is worth less than one that displaces import.

    Two solves differ only in whether PV covers the base load in the evening,
    when the stored heat is used. When it does, the displaced heating is worth
    the export price, too little to pay for the boost; when it does not, it is
    worth the import price, which does pay.
    """
    cheap = 8
    prices = [0.10] * cheap + [0.40] * (_HORIZON - cheap)
    exports = [0.0] * _HORIZON

    covered = build_and_solve(
        _bundle(prices=prices, export_prices=exports, pv_kw=[5.0] * _HORIZON),
        _config(pv=True),
    )
    uncovered = build_and_solve(
        _bundle(prices=prices, export_prices=exports, pv_kw=[0.0] * _HORIZON),
        _config(pv=True),
    )

    assert all(stage == 0 for stage in _stages(covered))
    assert any(stage == 1 for stage in _stages(uncovered))


def test_hybrid_inverter_pv_counts_towards_covering_base_load() -> None:
    cheap = 8
    prices = [0.10] * cheap + [0.40] * (_HORIZON - cheap)
    exports = [0.0] * _HORIZON

    result = build_and_solve(
        _bundle(
            prices=prices, export_prices=exports, hybrid_pv_kw=[5.0] * _HORIZON
        ),
        _config(hybrid=True),
    )

    assert all(stage == 0 for stage in _stages(result))


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_missing_bundle_entry_is_rejected() -> None:
    bundle = _bundle(prices=[0.25] * _HORIZON).model_copy(
        update={"heat_store_inputs": {}}
    )
    with pytest.raises(ValueError, match="boiler"):
        build_and_solve(bundle, _config())


def test_schedule_entries_carry_type_stage_and_temperature() -> None:
    result = build_and_solve(_bundle(prices=_spread_prices()), _config())

    entry = result.schedule[0].devices["boiler"]
    assert entry.type == "heat_store"
    assert entry.boost_stage in (0, 1)
    assert entry.temp_c is not None
    assert entry.kw <= 0.0
