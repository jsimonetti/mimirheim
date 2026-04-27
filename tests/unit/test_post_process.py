"""Unit tests for mimirheim/core/post_process.py.

Tests cover ``apply_gain_threshold`` and its helper logic: suppression
conditions, idle schedule construction, and all bypass guards.

Closed-loop enforcer tests (formerly ``apply_zero_export_flags``) have been
moved to ``tests/unit/test_control_arbitration.py``.
"""

from datetime import UTC, datetime, timedelta

import pytest

from mimirheim.config.schema import (
    BatteryConfig,
    EfficiencySegment,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    ObjectivesConfig,
    OutputsConfig,
)
from mimirheim.core.bundle import (
    BatteryInputs,
    DeviceSetpoint,
    DeferrableWindow,
    EvInputs,
    ScheduleStep,
    SolveBundle,
    SolveResult,
)
from mimirheim.core.post_process import apply_gain_threshold


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
_LATER = _NOW + timedelta(hours=4)


def _seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=5.0, efficiency=0.95)


def _make_config(threshold: float = 0.0) -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test", topic_prefix="mimir"),
        outputs=OutputsConfig(
            schedule="mimir/strategy/schedule",
            current="mimir/strategy/current",
            last_solve="mimir/status/last_solve",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
            )
        },
        objectives=ObjectivesConfig(min_dispatch_gain_eur=threshold),
    )


def _bundle(
    strategy: str = "minimize_cost",
    ev_inputs: dict | None = None,
    deferrable_windows: dict | None = None,
) -> SolveBundle:
    n = 4  # 4 steps for brevity
    return SolveBundle(
        strategy=strategy,
        solve_time_utc=_NOW,
        horizon_prices=[0.20] * n,
        horizon_export_prices=[0.05] * n,
        horizon_confidence=[1.0] * n,
        pv_forecast=[2.0] * n,
        base_load_forecast=[3.0] * n,
        battery_inputs={"bat": BatteryInputs(soc_kwh=5.0)},
        ev_inputs=ev_inputs or {},
        deferrable_windows=deferrable_windows or {},
    )


def _result(
    naive_cost: float,
    optimised_cost: float,
    strategy: str = "minimize_cost",
    solve_status: str = "optimal",
) -> SolveResult:
    """Build a minimal SolveResult with one schedule step containing battery and PV."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=0.5,
        grid_export_kw=0.0,
        devices={
            "bat": DeviceSetpoint(kw=-2.0, type="battery"),
            "pv": DeviceSetpoint(kw=2.0, type="pv"),
            "load": DeviceSetpoint(kw=-3.0, type="static_load"),
        },
    )
    return SolveResult(
        strategy=strategy,
        objective_value=-0.1,
        solve_status=solve_status,
        naive_cost_eur=naive_cost,
        optimised_cost_eur=optimised_cost,
        schedule=[step] * 4,  # repeat the same step for the 4-step horizon
    )


# ---------------------------------------------------------------------------
# No-op cases (suppression must NOT trigger)
# ---------------------------------------------------------------------------


def test_no_suppression_when_threshold_is_zero() -> None:
    """Default threshold of 0.0 must never suppress dispatch."""
    config = _make_config(threshold=0.0)
    res = _result(naive_cost=1.0, optimised_cost=0.99)  # gain = 0.01

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_gain_above_threshold() -> None:
    """When gain >= threshold, the result must be returned unchanged."""
    config = _make_config(threshold=0.05)
    res = _result(naive_cost=1.0, optimised_cost=0.90)  # gain = 0.10

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_gain_equals_threshold() -> None:
    """When gain exactly equals threshold, dispatch should proceed (>= rule)."""
    config = _make_config(threshold=0.05)
    res = _result(naive_cost=1.05, optimised_cost=1.00)  # gain = 0.05

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_infeasible() -> None:
    """An infeasible solve result must pass through unchanged."""
    config = _make_config(threshold=0.10)
    res = SolveResult(
        strategy="minimize_cost",
        objective_value=0.0,
        solve_status="infeasible",
        naive_cost_eur=1.0,
        optimised_cost_eur=1.0,
        schedule=[],
    )

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is res


def test_no_suppression_for_minimize_consumption_strategy() -> None:
    """The threshold check must not apply to the minimize_consumption strategy."""
    config = _make_config(threshold=1.0)
    res = _result(naive_cost=0.01, optimised_cost=0.00, strategy="minimize_consumption")

    out = apply_gain_threshold(res, _bundle(strategy="minimize_consumption"), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_gain_is_negative() -> None:
    """When the optimised cost exceeds the naive cost, suppression must not trigger.

    A negative gain indicates the solver had mandatory work to do (e.g. must
    charge an EV) that is more expensive than the naive baseline. Idling would
    make things worse.
    """
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=0.50, optimised_cost=0.60)  # gain = -0.10

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_ev_has_active_deadline() -> None:
    """Suppression must be bypassed when an EV with a charge deadline is plugged in."""
    config = _make_config(threshold=0.50)
    ev = EvInputs(soc_kwh=10.0, available=True, target_soc_kwh=30.0, window_latest=_LATER)
    res = _result(naive_cost=0.10, optimised_cost=0.09)  # gain = 0.01, below threshold

    out = apply_gain_threshold(res, _bundle(ev_inputs={"car": ev}), config)

    assert out is res
    assert not out.dispatch_suppressed


def test_no_suppression_when_ev_has_no_deadline() -> None:
    """An EV without a target SOC or deadline must not bypass suppression."""
    config = _make_config(threshold=0.50)
    # EV is available but has no hard deadline
    ev = EvInputs(soc_kwh=10.0, available=True, target_soc_kwh=None, window_latest=None)
    res = _result(naive_cost=0.10, optimised_cost=0.09)  # gain = 0.01, below threshold

    out = apply_gain_threshold(res, _bundle(ev_inputs={"car": ev}), config)

    # EV without deadline: suppression should still trigger
    assert out.dispatch_suppressed


def test_no_suppression_when_ev_deadline_is_past() -> None:
    """An EV whose deadline has already passed must not bypass suppression."""
    config = _make_config(threshold=0.50)
    past = _NOW - timedelta(hours=1)
    ev = EvInputs(soc_kwh=10.0, available=True, target_soc_kwh=30.0, window_latest=past)
    res = _result(naive_cost=0.10, optimised_cost=0.09)  # gain below threshold

    out = apply_gain_threshold(res, _bundle(ev_inputs={"car": ev}), config)

    assert out.dispatch_suppressed


def test_no_suppression_when_deferrable_window_active() -> None:
    """Suppression must be bypassed when a deferrable load has an active window."""
    config = _make_config(threshold=0.50)
    window = DeferrableWindow(earliest=_NOW, latest=_LATER)
    res = _result(naive_cost=0.10, optimised_cost=0.09)

    out = apply_gain_threshold(
        res, _bundle(deferrable_windows={"dishwasher": window}), config
    )

    assert out is res
    assert not out.dispatch_suppressed


# ---------------------------------------------------------------------------
# Suppression cases
# ---------------------------------------------------------------------------


def test_suppression_when_gain_below_threshold() -> None:
    """When gain < threshold, apply_gain_threshold must return a suppressed result."""
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)  # gain = 0.03

    out = apply_gain_threshold(res, _bundle(), config)

    assert out is not res
    assert out.dispatch_suppressed


def test_suppressed_result_preserves_cost_figures() -> None:
    """The suppressed result must preserve original naive_cost_eur and optimised_cost_eur."""
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)

    out = apply_gain_threshold(res, _bundle(), config)

    assert out.naive_cost_eur == pytest.approx(1.00)
    assert out.optimised_cost_eur == pytest.approx(0.97)


def test_suppressed_result_preserves_strategy_and_status() -> None:
    """strategy and solve_status must be unchanged in the suppressed result."""
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)

    out = apply_gain_threshold(res, _bundle(), config)

    assert out.strategy == res.strategy
    assert out.solve_status == res.solve_status


# ---------------------------------------------------------------------------
# Idle schedule construction
# ---------------------------------------------------------------------------


def test_idle_schedule_zeros_battery_setpoints() -> None:
    """Suppressed schedule must have kw=0 for all battery setpoints."""
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)

    out = apply_gain_threshold(res, _bundle(), config)

    for step in out.schedule:
        assert step.devices["bat"].kw == pytest.approx(0.0)
        assert step.devices["bat"].type == "battery"


def test_idle_schedule_preserves_pv_setpoints() -> None:
    """PV setpoints must be unchanged in the suppressed schedule."""
    config = _make_config(threshold=0.10)
    original_pv_kw = _result(naive_cost=1.00, optimised_cost=0.97).schedule[0].devices["pv"].kw

    out = apply_gain_threshold(_result(naive_cost=1.00, optimised_cost=0.97), _bundle(), config)

    for step in out.schedule:
        assert step.devices["pv"].kw == pytest.approx(original_pv_kw)


def test_idle_schedule_grid_balance() -> None:
    """Grid import/export in the suppressed schedule must satisfy the power balance.

    With battery zeroed: net_device = pv_kw + load_kw = 2.0 + (-3.0) = -1.0.
    grid_import = max(0, -(-1.0)) = 1.0, grid_export = 0.0.
    """
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)

    out = apply_gain_threshold(res, _bundle(), config)

    for step in out.schedule:
        # pv = +2.0, load = -3.0, bat (zeroed) = 0.0 → net = -1.0 → import 1.0 kW
        assert step.grid_import_kw == pytest.approx(1.0)
        assert step.grid_export_kw == pytest.approx(0.0)


def test_idle_schedule_has_same_length_as_original() -> None:
    """The suppressed schedule must have the same number of steps as the original."""
    config = _make_config(threshold=0.10)
    res = _result(naive_cost=1.00, optimised_cost=0.97)

    out = apply_gain_threshold(res, _bundle(), config)

    assert len(out.schedule) == len(res.schedule)


# ---------------------------------------------------------------------------
# Hybrid inverter in idle schedule (Plan 55 — F2)
# ---------------------------------------------------------------------------


def _result_with_hybrid(
    naive_cost: float,
    optimised_cost: float,
    hi_kw: float = 2.0,
) -> SolveResult:
    """Build a SolveResult whose schedule includes a hybrid inverter setpoint."""
    step = ScheduleStep(
        t=0,
        grid_import_kw=0.0,
        grid_export_kw=1.0,
        devices={
            "bat": DeviceSetpoint(kw=-2.0, type="battery"),
            "hi": DeviceSetpoint(kw=hi_kw, type="hybrid_inverter"),
            "pv": DeviceSetpoint(kw=2.0, type="pv"),
            "load": DeviceSetpoint(kw=-3.0, type="static_load"),
        },
    )
    return SolveResult(
        strategy="minimize_cost",
        objective_value=-0.1,
        solve_status="optimal",
        naive_cost_eur=naive_cost,
        optimised_cost_eur=optimised_cost,
        schedule=[step] * 4,
    )


def test_hybrid_inverter_zeroed_in_idle_schedule() -> None:
    """When dispatch is suppressed, the hybrid inverter kW is set to 0.0
    in the idle schedule.
    """
    config = _make_config(threshold=0.10)
    res = _result_with_hybrid(naive_cost=1.00, optimised_cost=0.97, hi_kw=3.0)

    out = apply_gain_threshold(res, _bundle(), config)

    assert out.dispatch_suppressed
    for step in out.schedule:
        assert step.devices["hi"].kw == pytest.approx(0.0)
        assert step.devices["hi"].type == "hybrid_inverter"


def test_idle_schedule_grid_balance_correct_with_hybrid_inverter() -> None:
    """The grid_import_kw / grid_export_kw in the idle schedule are computed from
    the correct power balance when a hybrid inverter is present.

    Schedule before suppression:
        battery = -2.0 kW (charging, consuming from AC bus)
        hybrid  =  3.0 kW (discharging, producing to AC bus)
        pv      =  2.0 kW (producing)
        load    = -3.0 kW (consuming)

    After suppression (battery and hybrid zeroed):
        net = pv + load = 2.0 - 3.0 = -1.0 kW
        grid_import = 1.0, grid_export = 0.0
    """
    config = _make_config(threshold=0.10)
    res = _result_with_hybrid(naive_cost=1.00, optimised_cost=0.97, hi_kw=3.0)

    out = apply_gain_threshold(res, _bundle(), config)

    assert out.dispatch_suppressed
    for step in out.schedule:
        # pv=+2.0, load=-3.0, battery zeroed, hybrid zeroed → net = -1.0 → import 1.0
        assert step.grid_import_kw == pytest.approx(1.0)
        assert step.grid_export_kw == pytest.approx(0.0)

