"""Regression tests: multiple static_loads must not multiply the load forecast.

Bug history: ``readiness`` summed all static load forecasts into the single
``SolveBundle.base_load_forecast`` series, and ``build_and_solve`` handed that
summed series to every ``StaticLoad``. With N configured static loads the
power balance therefore contained N x the real household load, and every
published per-device setpoint equalled the total instead of the load's own
draw. Fixed by ``SolveBundle.static_load_forecasts`` (per-load series), the
same way ``pv_forecasts`` fixed the PV case.

The scenario below has a fixed PV array whose forecast exactly matches the
combined load at every step. The only correct schedule is zero grid
exchange. These tests cover the solver side; readiness and ``_clip_bundle``
are covered in their own test modules.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve

_HOUSE_KW = [1.0, 1.0, 1.0, 1.0]
_HEATING_KW = [0.5, 0.7, 0.3, 0.2]
_TOTAL_KW = [h + p for h, p in zip(_HOUSE_KW, _HEATING_KW, strict=True)]


def _config(loads: dict | None = None) -> MimirheimConfig:
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0},
            "pv_arrays": {"roof": {"max_power_kw": 5.0}},
            "static_loads": loads if loads is not None else {"house": {}, "heating": {}},
        }
    )


def _bundle(*, per_load: bool = True, per_load_series: dict | None = None) -> SolveBundle:
    horizon = len(_TOTAL_KW)
    series = per_load_series if per_load_series is not None else {"house": _HOUSE_KW, "heating": _HEATING_KW}
    return SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=_TOTAL_KW,
        base_load_forecast=_TOTAL_KW,
        static_load_forecasts=series if per_load else {},
    )


def test_two_loads_forecast_enters_balance_once() -> None:
    """PV covers the combined load exactly, so grid exchange is zero.

    Under the double-count bug each load drew the total, the balance saw
    twice the load, and the grid covered the difference.
    """
    result = build_and_solve(_bundle(), _config())
    assert result.solve_status in ("optimal", "feasible")
    for step in result.schedule:
        load_sum = sum(-sp.kw for sp in step.devices.values() if sp.type == "static_load")
        assert load_sum == pytest.approx(_TOTAL_KW[step.t], abs=1e-6)
        assert step.grid_import_kw == pytest.approx(0.0, abs=1e-6)
        assert step.grid_export_kw == pytest.approx(0.0, abs=1e-6)


def test_two_loads_each_setpoint_is_own_forecast() -> None:
    """Each static load's published kw is its own forecast, not the total."""
    result = build_and_solve(_bundle(), _config())
    for step in result.schedule:
        assert -step.devices["house"].kw == pytest.approx(_HOUSE_KW[step.t], abs=1e-6)
        assert -step.devices["heating"].kw == pytest.approx(_HEATING_KW[step.t], abs=1e-6)


def test_multi_load_without_per_load_forecasts_is_rejected() -> None:
    """A summed-only bundle cannot be attributed to more than one load."""
    with pytest.raises(ValueError, match="static_load_forecasts"):
        build_and_solve(_bundle(per_load=False), _config())


def test_single_load_falls_back_to_summed_series() -> None:
    """With one static load the summed series is that load: older dumps stay valid."""
    result = build_and_solve(_bundle(per_load=False), _config(loads={"base": {}}))
    assert result.solve_status in ("optimal", "feasible")
    for step in result.schedule:
        assert -step.devices["base"].kw == pytest.approx(_TOTAL_KW[step.t], abs=1e-6)


def test_inconsistent_summed_and_per_load_series_is_rejected() -> None:
    """base_load_forecast must equal the per-step sum of static_load_forecasts."""
    with pytest.raises(ValueError, match="must equal"):
        _bundle(per_load_series={"house": _HOUSE_KW, "heating": [9.0] * len(_TOTAL_KW)})


def test_per_load_series_of_the_wrong_length_is_rejected() -> None:
    """A per-load series must cover exactly the steps of base_load_forecast."""
    with pytest.raises(ValueError, match="steps"):
        _bundle(per_load_series={"house": _HOUSE_KW, "heating": _HEATING_KW[:2]})


def test_forecast_for_unconfigured_load_is_rejected() -> None:
    """A stale key passes the sum check but has no device to enter the balance."""
    with pytest.raises(ValueError, match="unknown static load"):
        build_and_solve(_bundle(), _config(loads={"house": {}}))


def test_partially_populated_per_load_map_is_rejected() -> None:
    """Two loads but only one series: the sum check passes, the device check must not."""
    horizon = len(_TOTAL_KW)
    bundle = SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=_TOTAL_KW,
        base_load_forecast=_HOUSE_KW,
        static_load_forecasts={"house": _HOUSE_KW},
    )
    with pytest.raises(ValueError, match="heating"):
        build_and_solve(bundle, _config())


def test_rounding_within_tolerance_is_accepted() -> None:
    """Per-load values rounded independently of the sum are accepted within 0.005 kW."""
    bundle = _bundle(per_load_series={"house": _HOUSE_KW, "heating": [v + 0.004 for v in _HEATING_KW]})
    assert bundle.static_load_forecasts["heating"][0] == pytest.approx(0.504)


def test_per_load_series_survive_a_json_round_trip() -> None:
    """Debug dumps serialise the bundle to JSON; the per-load map must come back intact."""
    bundle = _bundle()
    restored = SolveBundle.model_validate_json(bundle.model_dump_json())
    assert restored.static_load_forecasts == bundle.static_load_forecasts
