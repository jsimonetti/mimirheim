"""Regression test: multiple pv_arrays must not multiply the PV forecast.

Bug: ``readiness`` sums all PV array forecasts into the single
``SolveBundle.pv_forecast`` series, and ``build_and_solve`` then passes that
*summed* series to every ``PvDevice``. With N configured arrays the power
balance therefore contains N x the real PV power, and every published
per-device setpoint equals the fleet total instead of the array's own output.

The scenario below has two fixed (non-controllable) arrays whose combined
forecast exactly matches the base load at every step. The only correct
schedule is zero grid exchange. Under the bug, PV enters the balance twice
and the solver exports the phantom surplus.
"""

from datetime import datetime, timezone

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve

_EAST_KW = [1.0, 1.0, 1.0, 1.0]
_WEST_KW = [0.5, 0.5, 0.5, 0.5]
_TOTAL_KW = [e + w for e, w in zip(_EAST_KW, _WEST_KW)]


def _config() -> MimirheimConfig:
    return MimirheimConfig.model_validate(
        {
            "mqtt": {"host": "localhost", "client_id": "test"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0},
            "pv_arrays": {
                "east": {"max_power_kw": 2.0},
                "west": {"max_power_kw": 2.0},
            },
            "static_loads": {"base": {}},
        }
    )


def _bundle() -> SolveBundle:
    horizon = len(_TOTAL_KW)
    return SolveBundle(
        solve_time_utc=datetime(2026, 6, 1, 12, tzinfo=timezone.utc),
        horizon_prices=[0.25] * horizon,
        horizon_export_prices=[0.10] * horizon,
        horizon_confidence=[1.0] * horizon,
        pv_forecast=_TOTAL_KW,
        base_load_forecast=_TOTAL_KW,
    )


def test_two_arrays_forecast_enters_balance_once() -> None:
    """The summed PV power in the schedule must equal the forecast total once.

    PV exactly covers the base load, so grid exchange must be zero at every
    step. Under the double-count bug each array contributes the fleet total,
    the balance sees 3.0 kW of PV against a 1.5 kW load, and the solver
    exports 1.5 kW at every step.
    """
    result = build_and_solve(_bundle(), _config())
    assert result.solve_status in ("optimal", "feasible")

    for step in result.schedule:
        pv_sum = sum(
            sp.kw for sp in step.devices.values() if sp.type == "pv"
        )
        assert pv_sum == pytest.approx(_TOTAL_KW[step.t], abs=1e-6)
        assert step.grid_export_kw == pytest.approx(0.0, abs=1e-6)
        assert step.grid_import_kw == pytest.approx(0.0, abs=1e-6)

