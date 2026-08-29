"""Regression tests: multiple pv_arrays must not multiply the PV forecast.

Bug history: ``readiness`` summed all PV array forecasts into the single
``SolveBundle.pv_forecast`` series, and ``build_and_solve`` handed that
*summed* series to every ``PvDevice``. With N configured arrays the power
balance therefore contained N x the real PV power, and every published
per-device setpoint equalled the fleet total instead of the array's own
output. Fixed by ``SolveBundle.pv_forecasts`` (per-array series).

The scenario below has two fixed (non-controllable) arrays whose combined
forecast exactly matches the base load at every step. The only correct
schedule is zero grid exchange.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve

_EAST_KW = [1.0, 1.0, 1.0, 1.0]
_WEST_KW = [0.5, 0.5, 0.5, 0.5]
_TOTAL_KW = [e + w for e, w in zip(_EAST_KW, _WEST_KW, strict=True)]


def _config(arrays: dict | None = None) -> MimirheimConfig:
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0},
            "pv_arrays": arrays
            if arrays is not None
            else {
                "east": {"max_power_kw": 2.0},
                "west": {"max_power_kw": 2.0},
            },
            "static_loads": {"base": {}},
        }
    )


def _bundle(*, per_array: bool = True) -> SolveBundle:
    horizon = len(_TOTAL_KW)
    return SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=_TOTAL_KW,
        base_load_forecast=_TOTAL_KW,
        pv_forecasts={"east": _EAST_KW, "west": _WEST_KW} if per_array else {},
    )


def test_two_arrays_forecast_enters_balance_once() -> None:
    """The summed PV power in the schedule must equal the forecast total once.

    PV exactly covers the base load, so grid exchange must be zero at every
    step. Under the double-count bug each array contributed the fleet total,
    the balance saw 3.0 kW of PV against a 1.5 kW load, and the solver
    exported 1.5 kW at every step.
    """
    result = build_and_solve(_bundle(), _config())
    assert result.solve_status in ("optimal", "feasible")

    for step in result.schedule:
        pv_sum = sum(sp.kw for sp in step.devices.values() if sp.type == "pv")
        assert pv_sum == pytest.approx(_TOTAL_KW[step.t], abs=1e-6)
        assert step.grid_export_kw == pytest.approx(0.0, abs=1e-6)
        assert step.grid_import_kw == pytest.approx(0.0, abs=1e-6)


def test_two_arrays_each_setpoint_is_own_forecast() -> None:
    """Each fixed array's published kw must be its own forecast, not the total."""
    result = build_and_solve(_bundle(), _config())

    for step in result.schedule:
        assert step.devices["east"].kw == pytest.approx(_EAST_KW[step.t], abs=1e-6)
        assert step.devices["west"].kw == pytest.approx(_WEST_KW[step.t], abs=1e-6)


def test_multi_array_without_per_array_forecasts_is_rejected() -> None:
    """A summed-only bundle is ambiguous with >1 array and must fail loudly.

    Silently splitting or reusing the total would reintroduce the
    double-count in disguise. Older dump files (which predate
    pv_forecasts) can only be replayed against multi-array configs after
    regenerating them.
    """
    with pytest.raises(ValueError, match="pv_forecasts"):
        build_and_solve(_bundle(per_array=False), _config())


def test_single_array_falls_back_to_summed_series() -> None:
    """With one array the summed series IS the array: older dumps stay valid."""
    result = build_and_solve(
        _bundle(per_array=False),
        _config(arrays={"only": {"max_power_kw": 2.0}}),
    )
    assert result.solve_status in ("optimal", "feasible")
    for step in result.schedule:
        assert step.devices["only"].kw == pytest.approx(_TOTAL_KW[step.t], abs=1e-6)


def test_inconsistent_summed_and_per_array_series_is_rejected() -> None:
    """pv_forecast must equal the per-step sum of pv_forecasts.

    The power balance uses the per-array series while the naive-cost
    baseline uses the summed series; letting them diverge would report a
    wrong baseline and saving for an otherwise correct schedule.
    """
    horizon = len(_TOTAL_KW)
    with pytest.raises(ValueError, match="must equal"):
        SolveBundle(
            solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
            horizon_prices=[0.25] * horizon,
            horizon_export_prices=[0.10] * horizon,
            horizon_confidence=[1.0] * horizon,
            pv_forecast=[10.0] * horizon,
            base_load_forecast=_TOTAL_KW,
            pv_forecasts={"east": _EAST_KW, "west": _WEST_KW},
        )


def test_forecast_for_unconfigured_array_is_rejected() -> None:
    """A pv_forecasts key with no matching configured array must fail loudly.

    A stale key (array removed from config after the dump was written)
    passes the bundle sum check but would silently vanish from the power
    balance while the naive-cost baseline still counts it.
    """
    with pytest.raises(ValueError, match="unknown PV array"):
        build_and_solve(
            _bundle(),
            _config(arrays={"east": {"max_power_kw": 2.0}}),
        )
