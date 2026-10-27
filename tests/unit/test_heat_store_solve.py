"""Solve-level tests for the heat store device.

The heat store is tested through ``build_and_solve`` only: what the plan does
for given prices, forecasts and store temperature. Device internals (variable
names, constraint counts) are deliberately not asserted.

Reference store used throughout: 200 L of water (0.2326 kWh/K), normal setpoint
50 °C, ambient 18 °C, idle drift 0.8 K/h, a 3 kW resistive boost up to 65 °C.
At 15-minute steps a full-power boost adds 0.75 kWh of heat, about 3.2 K.
"""

from datetime import datetime, timezone
from typing import Any

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import HeatStoreInputs, SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve
from mimirheim.core.solver_backend import CBCSolverBackend

_DT = 0.25
_THERMAL_CAP_KWH_PER_K = 200.0 * 4186 / 3600 / 1000


def _config(
    *,
    normal_cop: float = 2.5,
    boost_cop: float = 1.0,
    on_threshold_pct: float = 50.0,
    with_pv: bool = True,
    strategy_weights: dict[str, float] | None = None,
) -> MimirheimConfig:
    raw: dict[str, Any] = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
        "static_loads": {"base": {}},
        "heat_stores": {
            "dhw": {
                "volume_liters": 200.0,
                "normal_cop": normal_cop,
                "normal_setpoint_c": 50.0,
                "ambient_c": 18.0,
                "drift_k_per_hour": 0.8,
                "boost": {
                    "power_kw": 3.0,
                    "max_temp_c": 65.0,
                    "cop": boost_cop,
                    "on_threshold_pct": on_threshold_pct,
                },
            }
        },
    }
    if with_pv:
        raw["pv_arrays"] = {"roof": {"max_power_kw": 10.0}}
    if strategy_weights is not None:
        raw["objectives"] = {"balanced_weights": strategy_weights}
    return MimirheimConfig.model_validate(raw)


def _bundle(
    *,
    import_prices: list[float],
    export_prices: list[float] | None = None,
    pv_kw: list[float] | None = None,
    load_kw: float = 0.5,
    temp_c: float = 50.0,
    confidence: list[float] | None = None,
    strategy: str = "minimize_cost",
) -> SolveBundle:
    horizon = len(import_prices)
    return SolveBundle(
        strategy=strategy,
        solve_time_utc=datetime(2026, 6, 1, 10, tzinfo=timezone.utc),
        horizon_prices=import_prices,
        horizon_export_prices=export_prices if export_prices is not None else [0.0] * horizon,
        horizon_confidence=confidence if confidence is not None else [1.0] * horizon,
        pv_forecast=pv_kw if pv_kw is not None else [0.0] * horizon,
        base_load_forecast=[load_kw] * horizon,
        heat_store_inputs={"dhw": HeatStoreInputs(current_temp_c=temp_c)},
    )


def _boost_kw(result: SolveResult) -> list[float]:
    """Planned boost power per step, positive kW."""
    return [-step.devices["dhw"].kw for step in result.schedule]


def _temps(result: SolveResult) -> list[float]:
    temps = [step.devices["dhw"].planned_temp_c for step in result.schedule]
    assert all(t is not None for t in temps)
    return temps  # type: ignore[return-value]


def _surplus_day(**overrides: Any) -> SolveBundle:
    """Eight steps of 4 kW PV against a 0.5 kW load, export nearly worthless."""
    args: dict[str, Any] = {
        "import_prices": [0.25] * 8,
        "export_prices": [0.02] * 8,
        "pv_kw": [4.0] * 8,
    }
    args.update(overrides)
    return _bundle(**args)


def test_pv_surplus_with_low_export_price_is_boosted() -> None:
    result = build_and_solve(_surplus_day(), _config())

    assert result.solve_status == "optimal"
    assert sum(_boost_kw(result)) * _DT > 2.0
    assert max(_temps(result)) > 55.0


def test_schedule_carries_boost_power_and_planned_temperature() -> None:
    result = build_and_solve(_surplus_day(), _config())

    for step in result.schedule:
        setpoint = step.devices["dhw"]
        assert setpoint.type == "heat_store"
        assert setpoint.kw <= 1e-9
        assert setpoint.planned_temp_c is not None


def test_schedule_carries_on_flag_matching_the_planned_power() -> None:
    result = build_and_solve(_surplus_day(), _config())

    for step in result.schedule:
        setpoint = step.devices["dhw"]
        assert setpoint.on is (-setpoint.kw >= 1.5)


@pytest.mark.parametrize(
    ("on_threshold_pct", "surplus_kw", "expected_on"),
    [
        (50.0, 1.49, False),
        (50.0, 1.5, True),
        (25.0, 0.74, False),
        (25.0, 0.75, True),
    ],
)
def test_on_flag_turns_on_at_the_configured_threshold(
    on_threshold_pct: float, surplus_kw: float, expected_on: bool
) -> None:
    """A one-step surplus is boosted exactly; on flips at threshold * power_kw.

    Grid power at 0.25 is worth more than the heat it buys, so the plan boosts
    exactly the PV surplus and no more.
    """
    bundle = _bundle(import_prices=[0.25], pv_kw=[0.5 + surplus_kw])
    result = build_and_solve(bundle, _config(on_threshold_pct=on_threshold_pct))

    assert _boost_kw(result)[0] == pytest.approx(surplus_kw, abs=1e-6)
    assert result.schedule[0].devices["dhw"].on is expected_on


def test_no_grid_boost_at_positive_prices_ahead_of_an_evening_peak() -> None:
    """Heat bought at 0.20 cannot be banked for the 0.45 peak (ADR 0001).

    With normal_cop 1.2, heat valued at the peak price would be worth
    0.45 / 1.2 = 0.375 per kWh, well above the 0.20 it costs, and the plan
    would boost from the grid. At the running minimum it is worth at most
    0.20 / 1.2, below cost.
    """
    bundle = _bundle(
        import_prices=[0.20] * 4 + [0.45] * 4,
        export_prices=[0.0] * 8,
        pv_kw=[0.0] * 8,
    )
    result = build_and_solve(bundle, _config(normal_cop=1.2))

    assert result.solve_status == "optimal"
    assert max(_boost_kw(result)) < 1e-6


def test_negative_import_price_is_boosted_from_the_grid() -> None:
    bundle = _bundle(
        import_prices=[0.25, 0.25, -0.05, -0.05, 0.25, 0.25],
        pv_kw=[0.0] * 6,
    )
    result = build_and_solve(bundle, _config())

    boost = _boost_kw(result)
    assert boost[2] == pytest.approx(3.0, abs=1e-6)
    assert boost[3] == pytest.approx(3.0, abs=1e-6)
    assert boost[0] < 1e-6 and boost[1] < 1e-6


def test_store_above_boost_maximum_is_feasible_and_waits_until_it_cools() -> None:
    """66 °C start, 65 °C limit: no boost until the no-boost path is below 65 °C."""
    bundle = _surplus_day(
        import_prices=[0.25] * 16, export_prices=[0.02] * 16, pv_kw=[4.0] * 16, temp_c=66.0
    )
    result = build_and_solve(bundle, _config())

    assert result.solve_status == "optimal"
    boost = _boost_kw(result)
    temps = _temps(result)
    # Without boost the store loses about 0.3 K per step at 66 °C, so it is
    # above the limit for the first three steps.
    for t in range(3):
        assert temps[t] > 65.0
        assert boost[t] < 1e-6
    assert any(b > 1e-3 for b in boost[3:])
    assert max(temps[3:]) <= 65.0 + 1e-6


def test_store_starting_far_above_the_limit_solves_without_boosting() -> None:
    result = build_and_solve(_surplus_day(temp_c=80.0), _config())

    assert result.solve_status == "optimal"
    assert max(_boost_kw(result)) < 1e-6
    temps = _temps(result)
    assert all(a > b for a, b in zip(temps, temps[1:], strict=False))


def test_store_below_normal_setpoint_takes_pv_surplus_to_close_the_gap() -> None:
    """A 40 °C store holds a 10 K gap (2.3 kWh) the normal program would buy."""
    result = build_and_solve(_surplus_day(temp_c=40.0), _config())

    gap_kwh = 10.0 * _THERMAL_CAP_KWH_PER_K
    assert sum(_boost_kw(result)) * _DT >= gap_kwh - 1e-6
    assert _temps(result)[-1] >= 50.0 - 1e-6


def test_boost_cop_above_one_yields_more_heat_per_kwh() -> None:
    """The same surplus raises the store about three times as much at COP 3."""
    # 1 kW of surplus for two steps. normal_cop 4 keeps a grid boost unprofitable
    # at either boost COP, so both plans take exactly the surplus.
    pv = [1.5, 1.5, 0.0, 0.0]
    kwargs: dict[str, Any] = {
        "import_prices": [0.40] * 4, "export_prices": [0.02] * 4, "pv_kw": pv,
    }
    low = build_and_solve(_bundle(**kwargs), _config(normal_cop=4.0, boost_cop=1.0))
    high = build_and_solve(_bundle(**kwargs), _config(normal_cop=4.0, boost_cop=3.0))

    assert sum(_boost_kw(low)) == pytest.approx(2.0, abs=1e-6)
    assert sum(_boost_kw(high)) == pytest.approx(2.0, abs=1e-6)
    rise_low = _temps(low)[1] - 50.0
    rise_high = _temps(high)[1] - 50.0
    assert rise_low > 0.0
    assert rise_high > 2.5 * rise_low


def test_low_confidence_step_does_not_make_a_grid_boost_look_cheap() -> None:
    """At confidence 0.1 a 0.25 import costs 0.025 in the objective.

    The saved-electricity price follows the same weighting, so the boost heat
    is worth at most 0.025 / 2.5 per kWh and the grid boost does not pay.
    Valuing it at the raw 0.25 would make it look four times cheaper than the
    saving.
    """
    bundle = _bundle(
        import_prices=[0.25] * 6,
        pv_kw=[0.0] * 6,
        confidence=[1.0, 1.0, 0.1, 1.0, 1.0, 1.0],
    )
    result = build_and_solve(bundle, _config())

    assert max(_boost_kw(result)) < 1e-6


def test_boost_value_is_on_the_same_scale_as_import_and_export() -> None:
    """Saved kWh are divided by dt before pricing, like terminal SoC.

    With normal_cop 1.0 a kWh of boost heat saves 0.25 EUR of import, well
    above the 0.08 export price, so the surplus goes into the store. Priced
    without the dt scaling the saving would be worth 0.0625 per step-kWh, below
    the export price, and the plan would export instead.
    """
    bundle = _surplus_day(export_prices=[0.08] * 8)
    result = build_and_solve(bundle, _config(normal_cop=1.0))

    assert sum(_boost_kw(result)) * _DT > 2.0


@pytest.mark.parametrize(
    ("strategy", "weights"),
    [("balanced", {"cost_weight": 1.0, "self_sufficiency_weight": 1.0}), ("minimize_consumption", None)],
)
def test_boost_value_reaches_the_other_cost_objectives(
    strategy: str, weights: dict[str, float] | None
) -> None:
    """The cost part of balanced and pass 2 of minimize_consumption price boost heat."""
    result = build_and_solve(
        _surplus_day(strategy=strategy), _config(strategy_weights=weights)
    )

    assert sum(_boost_kw(result)) * _DT > 2.0


def test_heat_store_adds_no_binary_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    def count_binaries(config: MimirheimConfig, bundle: SolveBundle) -> int:
        calls: list[bool] = []
        original = CBCSolverBackend.add_var

        def spy(self: CBCSolverBackend, lb: float = 0.0, ub: float = 1e30, integer: bool = False) -> Any:
            calls.append(integer)
            return original(self, lb=lb, ub=ub, integer=integer)

        monkeypatch.setattr(CBCSolverBackend, "add_var", spy)
        build_and_solve(bundle, config)
        monkeypatch.setattr(CBCSolverBackend, "add_var", original)
        return sum(calls)

    with_store = count_binaries(_config(), _surplus_day())
    raw = _config().model_dump()
    raw["heat_stores"] = {}
    without_store = count_binaries(
        MimirheimConfig.model_validate(raw),
        _surplus_day().model_copy(update={"heat_store_inputs": {}}),
    )

    assert with_store == without_store


def test_missing_heat_store_input_is_rejected() -> None:
    bundle = _surplus_day().model_copy(update={"heat_store_inputs": {}})

    with pytest.raises(ValueError, match="dhw"):
        build_and_solve(bundle, _config())
