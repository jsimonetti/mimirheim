"""Unit tests for mimirheim/core/bundle.py — SolveBundle, SolveResult, and all per-device input models.

Tests are written before the implementation (TDD). All tests in this file must
fail before bundle.py exists, and all must pass once it is implemented.
"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from mimirheim.core.bundle import (
    BatteryInputs,
    DeferrableWindow,
    DeviceSetpoint,
    EvInputs,
    ScheduleStep,
    SolveBundle,
    SolveResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _prices(n: int = 96) -> list[float]:
    return [0.25] * n


def _minimal_bundle(**overrides) -> SolveBundle:
    """Construct the smallest valid SolveBundle."""
    kwargs = dict(
        solve_time_utc=_now(),
        horizon_prices=_prices(),
        horizon_export_prices=_prices(),
        horizon_confidence=[1.0] * 96,
        pv_forecast=[0.0] * 96,
        base_load_forecast=[0.5] * 96,
    )
    kwargs.update(overrides)
    return SolveBundle(**kwargs)


# ---------------------------------------------------------------------------
# BatteryInputs
# ---------------------------------------------------------------------------


def test_battery_inputs_valid() -> None:
    bi = BatteryInputs(soc_kwh=5.0)
    assert bi.soc_kwh == 5.0


def test_battery_inputs_negative_soc_rejected() -> None:
    with pytest.raises(ValidationError):
        BatteryInputs(soc_kwh=-1.0)


# ---------------------------------------------------------------------------
# EvInputs
# ---------------------------------------------------------------------------


def test_ev_inputs_valid() -> None:
    ev = EvInputs(
        soc_kwh=20.0,
        available=True,
        window_earliest=_now(),
        window_latest=_now() + timedelta(hours=8),
    )
    assert ev.available is True
    assert ev.soc_kwh == 20.0


def test_ev_inputs_not_plugged() -> None:
    ev = EvInputs(available=False, soc_kwh=0.0)
    assert ev.window_earliest is None
    assert ev.window_latest is None


# ---------------------------------------------------------------------------
# DeferrableWindow
# ---------------------------------------------------------------------------


def test_deferrable_window_valid() -> None:
    w = DeferrableWindow(earliest=_now(), latest=_now() + timedelta(hours=4))
    assert w.latest > w.earliest


# ---------------------------------------------------------------------------
# SolveBundle
# ---------------------------------------------------------------------------


def test_solve_bundle_strategy_defaults_to_minimize_cost() -> None:
    bundle = _minimal_bundle()
    assert bundle.strategy == "minimize_cost"


def test_solve_bundle_valid() -> None:
    bundle = _minimal_bundle()
    assert len(bundle.horizon_prices) == 96
    assert bundle.battery_inputs == {}
    assert bundle.ev_inputs == {}
    assert bundle.deferrable_windows == {}


def test_solve_bundle_with_devices() -> None:
    bundle = _minimal_bundle(
        battery_inputs={"battery_main": BatteryInputs(soc_kwh=8.0)},
        ev_inputs={
            "ev_charger": EvInputs(
                soc_kwh=15.0,
                available=True,
                window_earliest=_now(),
                window_latest=_now() + timedelta(hours=6),
            )
        },
    )
    assert "battery_main" in bundle.battery_inputs
    assert "ev_charger" in bundle.ev_inputs


def test_solve_bundle_one_step_valid() -> None:
    """A SolveBundle with a single price step is accepted (min_length is now 1)."""
    bundle = _minimal_bundle(
        horizon_prices=_prices(n=1),
        horizon_export_prices=_prices(n=1),
        horizon_confidence=[1.0],
        pv_forecast=[0.0],
        base_load_forecast=[0.5],
    )
    assert len(bundle.horizon_prices) == 1


def test_solve_bundle_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        _minimal_bundle(unexpected_field="oops")


# ---------------------------------------------------------------------------
# SolveResult, ScheduleStep, DeviceSetpoint
# ---------------------------------------------------------------------------


def test_solve_result_valid() -> None:
    step = ScheduleStep(
        t=0,
        grid_import_kw=1.0,
        grid_export_kw=0.0,
        devices={"battery_main": DeviceSetpoint(kw=-0.5, type="battery")},
    )
    result = SolveResult(
        strategy="minimize_cost",
        objective_value=-0.12,
        solve_status="optimal",
        schedule=[step],
    )
    assert result.solve_status == "optimal"
    assert result.schedule[0].t == 0


def test_solve_result_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        SolveResult(
            strategy="minimize_cost",
            objective_value=0.0,
            solve_status="optimal",
            schedule=[],
            unexpected="oops",
        )


def test_device_setpoint_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        DeviceSetpoint(kw=1.0, type="battery", unknown=True)
