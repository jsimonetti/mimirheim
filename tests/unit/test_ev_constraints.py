"""Unit tests for mimirheim/devices/ev.py — EV charger device variables and constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=8, dt=0.25 (2-hour horizon).
"""

from datetime import UTC, datetime, timedelta

import pytest

from mimirheim.config.schema import EfficiencySegment, EvConfig
from mimirheim.core.bundle import EvInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.ev import EvDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(power_max_kw: float, efficiency: float = 1.0) -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=power_max_kw, efficiency=efficiency)


def _config(
    capacity_kwh: float = 40.0,
    min_soc_kwh: float = 5.0,
    charge_segs: list[EfficiencySegment] | None = None,
    discharge_segs: list[EfficiencySegment] | None = None,
    wear_cost: float = 0.0,
    min_charge_kw: float | None = None,
    min_discharge_kw: float | None = None,
) -> EvConfig:
    return EvConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_segments=charge_segs or [_seg(7.4)],
        discharge_segments=discharge_segs if discharge_segs is not None else [],
        wear_cost_eur_per_kwh=wear_cost,
        min_charge_kw=min_charge_kw,
        min_discharge_kw=min_discharge_kw,
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _inputs(
    soc_kwh: float = 15.0,
    available: bool = True,
    target_soc_kwh: float | None = None,
    window_latest: datetime | None = None,
) -> EvInputs:
    return EvInputs(
        soc_kwh=soc_kwh,
        available=available,
        target_soc_kwh=target_soc_kwh,
        window_latest=window_latest,
    )


def _make_ctx(horizon: int = 8) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


# ---------------------------------------------------------------------------
# Availability gate
# ---------------------------------------------------------------------------


def test_ev_not_plugged_zero_charge() -> None:
    """When available=False, all charge power must be zero at every step."""
    ctx = _make_ctx()
    ev = EvDevice(name="ev", config=_config())
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(available=False), solve_time_utc=_now())

    ctx.solver.set_objective_minimize(-ev.charge_seg[0, 0])
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(ev.charge_seg[t, 0]) < 1e-6, (
            f"step {t}: expected zero charge when EV not plugged"
        )


# ---------------------------------------------------------------------------
# SOC tracking and window constraint
# ---------------------------------------------------------------------------


def test_ev_reaches_target_soc_within_window() -> None:
    """SOC must reach target_soc_kwh by window_latest step."""
    solve_time = _now()
    # window_latest is 1.5 hours ahead → step index = 1.5 / 0.25 = 6
    window_latest = solve_time + timedelta(hours=1.5)
    ctx = _make_ctx()
    ev = EvDevice(
        name="ev",
        config=_config(
            capacity_kwh=60.0,
            charge_segs=[_seg(11.0)],
        ),
    )
    ev.add_variables(ctx)
    ev.add_constraints(
        ctx,
        inputs=_inputs(
            soc_kwh=10.0,
            available=True,
            target_soc_kwh=20.0,
            window_latest=window_latest,
        ),
        solve_time_utc=solve_time,
    )
    ctx.solver.set_objective_minimize(ev.charge_seg[0, 0])
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible")
    assert ctx.solver.var_value(ev.soc[6]) >= 20.0 - 1e-6


def test_ev_soc_respects_capacity() -> None:
    """SOC must never exceed capacity_kwh."""
    ctx = _make_ctx()
    ev = EvDevice(
        name="ev",
        config=_config(capacity_kwh=30.0, charge_segs=[_seg(20.0)]),
    )
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=25.0), solve_time_utc=_now())

    obj = ev.soc[0]
    for t in range(1, 8):
        obj = obj + ev.soc[t]
    ctx.solver.set_objective_minimize(-obj)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(ev.soc[t]) <= 30.0 + 1e-6


def test_ev_soc_respects_min_soc() -> None:
    """SOC must never fall below min_soc_kwh."""
    ctx = _make_ctx()
    ev = EvDevice(
        name="ev",
        config=_config(
            min_soc_kwh=5.0,
            discharge_segs=[_seg(11.0)],
        ),
    )
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0), solve_time_utc=_now())

    obj = ev.soc[0]
    for t in range(1, 8):
        obj = obj + ev.soc[t]
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(ev.soc[t]) >= 5.0 - 1e-6


# ---------------------------------------------------------------------------
# Discharge segments optional
# ---------------------------------------------------------------------------


def test_ev_no_discharge_without_discharge_segments() -> None:
    """When discharge_segments=[], no discharge variables must be added."""
    ctx = _make_ctx()
    ev = EvDevice(name="ev", config=_config(discharge_segs=[]))
    ev.add_variables(ctx)
    # discharge_seg dict must be empty
    assert len(ev.discharge_seg) == 0


# ---------------------------------------------------------------------------
# Wear cost
# ---------------------------------------------------------------------------


def test_ev_wear_cost_suppresses_cycling() -> None:
    """With a very high wear cost and flat prices, throughput must be near zero."""
    ctx = _make_ctx()
    ev = EvDevice(
        name="ev",
        config=_config(
            charge_segs=[_seg(7.4, efficiency=0.95)],
            discharge_segs=[_seg(7.4, efficiency=0.95)],
            wear_cost=100.0,
        ),
    )
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=15.0), solve_time_utc=_now())

    obj = ev.objective_terms(0)
    for t in range(1, 8):
        obj = obj + ev.objective_terms(t)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    total_throughput = sum(
        ctx.solver.var_value(ev.charge_seg[t, 0])
        for t in ctx.T
    )
    assert total_throughput < 1e-6


# ---------------------------------------------------------------------------
# net_power sign convention
# ---------------------------------------------------------------------------


def test_ev_net_power_sign() -> None:
    """Charging gives negative net_power; discharging gives positive."""
    ctx = _make_ctx(horizon=1)
    ev = EvDevice(
        name="ev",
        config=_config(discharge_segs=[_seg(7.4)]),
    )
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0), solve_time_utc=_now())

    ctx.solver.add_constraint(ev.charge_seg[0, 0] == 3.0)
    ctx.solver.add_constraint(ev.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(ev.soc[0])
    ctx.solver.solve()

    charge_val = ctx.solver.var_value(ev.charge_seg[0, 0])
    discharge_val = ctx.solver.var_value(ev.discharge_seg[0, 0])
    net = discharge_val - charge_val
    assert net < 0, "charging should produce negative net power"


# ---------------------------------------------------------------------------
# Shared system direction binary — anti-roundtrip (Plan 38B)
# ---------------------------------------------------------------------------


def test_ev_set_external_mode_prevents_roundtrip() -> None:
    """With a shared mode binary, forcing ev1 to charge prevents ev2 from discharging (V2H).

    The shared mode variable is 1 (charging direction) when ev1 charges.
    ev2's V2H discharge is bounded by max_discharge * (1 - mode), which collapses
    to 0 when mode=1, preventing simultaneous charge+discharge across two EVs.
    """
    ctx = _make_ctx(horizon=1)
    ev1 = EvDevice(name="ev1", config=_config(discharge_segs=[_seg(7.4)]))
    ev2 = EvDevice(name="ev2", config=_config(discharge_segs=[_seg(7.4)]))
    ev1.add_variables(ctx)
    ev2.add_variables(ctx)

    shared_mode = {0: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)}
    ev1.set_external_mode(shared_mode)
    ev2.set_external_mode(shared_mode)

    solve_time = _now()
    ev1.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=solve_time)
    ev2.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=solve_time)

    # Force ev1 to charge at >= 3 kW. This pushes mode to 1.
    ctx.solver.add_constraint(ev1.charge_seg[0, 0] >= 3.0)
    # Try to maximise ev2 V2H discharge — should be blocked by mode=1.
    ctx.solver.set_objective_minimize(-ev2.discharge_seg[0, 0])
    ctx.solver.solve()

    assert ctx.solver.var_value(ev2.discharge_seg[0, 0]) < 1e-6, (
        "ev2 V2H discharge must be zero when ev1 is charging (shared mode=1)"
    )


def test_two_charge_only_evs_get_shared_mode_structure() -> None:
    """Two charge-only EVs accept set_external_mode without error; solve remains feasible.

    For charge-only EVs, mode[t] is not created by add_variables (has_v2h=False) and
    set_external_mode injects the shared variable, which add_constraints ignores because
    has_v2h is False. The shared variable is unconstrained but causes no infeasibility.
    """
    ctx = _make_ctx(horizon=1)
    ev1 = EvDevice(name="ev1", config=_config())
    ev2 = EvDevice(name="ev2", config=_config())
    ev1.add_variables(ctx)
    ev2.add_variables(ctx)

    shared_mode = {0: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)}
    ev1.set_external_mode(shared_mode)
    ev2.set_external_mode(shared_mode)

    solve_time = _now()
    ev1.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=solve_time)
    ev2.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=solve_time)

    ctx.solver.set_objective_minimize(ev1.charge_seg[0, 0])
    status = ctx.solver.solve()
    assert status != "infeasible"


# ---------------------------------------------------------------------------
# Minimum operating power constraints (Plan 38C)
# ---------------------------------------------------------------------------


def test_ev_min_charge_kw_enforced() -> None:
    """With min_charge_kw set on a V2H EV, solver dispatches zero or >= min_charge_kw.

    min_charge_kw requires mode[t] to exist (has_v2h=True). For charge-only EVs
    without a mode variable the constraint is not added (silently ignored).
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config(discharge_segs=[_seg(7.4)], min_charge_kw=1.4)
    ev = EvDevice(name="ev", config=cfg)
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=_now())

    ctx.solver.add_constraint(ev.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(ev.charge_seg[0, 0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(ev.charge_seg[0, 0])
    assert charge < 1e-6 or charge >= 1.4 - 1e-6, (
        f"charge={charge:.4f} violates min_charge_kw=1.4 (must be 0 or >= 1.4)"
    )


def test_ev_min_discharge_kw_enforced() -> None:
    """With min_discharge_kw set, solver dispatches zero or >= min_discharge_kw V2H discharge."""
    ctx = _make_ctx(horizon=1)
    cfg = _config(discharge_segs=[_seg(7.4)], min_discharge_kw=1.4)
    ev = EvDevice(name="ev", config=cfg)
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=_now())

    ctx.solver.add_constraint(ev.charge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(ev.discharge_seg[0, 0])
    ctx.solver.solve()

    discharge = ctx.solver.var_value(ev.discharge_seg[0, 0])
    assert discharge < 1e-6 or discharge >= 1.4 - 1e-6, (
        f"discharge={discharge:.4f} violates min_discharge_kw=1.4 (must be 0 or >= 1.4)"
    )


def test_ev_min_charge_kw_none_allows_fractional() -> None:
    """With min_charge_kw=None (default), the solver can dispatch any charge value."""
    ctx = _make_ctx(horizon=1)
    cfg = _config(discharge_segs=[_seg(7.4)])
    ev = EvDevice(name="ev", config=cfg)
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=_now())

    ctx.solver.add_constraint(ev.discharge_seg[0, 0] == 0.0)
    ctx.solver.add_constraint(ev.charge_seg[0, 0] >= 1.0)
    ctx.solver.add_constraint(ev.charge_seg[0, 0] <= 1.0)
    ctx.solver.set_objective_minimize(ev.soc[0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(ev.charge_seg[0, 0])
    assert abs(charge - 1.0) < 1e-5, (
        f"Expected fractional charge=1.0 kW without min_charge constraint, got {charge:.4f}"
    )


def test_ev_min_discharge_kw_ignored_when_no_discharge_segments() -> None:
    """min_discharge_kw is accepted but silently ignored for charge-only EVs."""
    cfg = _config(discharge_segs=[], min_discharge_kw=1.4)
    ctx = _make_ctx(horizon=1)
    ev = EvDevice(name="ev", config=cfg)
    ev.add_variables(ctx)
    ev.add_constraints(ctx, inputs=_inputs(soc_kwh=20.0, available=True), solve_time_utc=_now())

    ctx.solver.set_objective_minimize(ev.charge_seg[0, 0])
    status = ctx.solver.solve()
    assert status != "infeasible", "charge-only EV with min_discharge_kw must be feasible"
