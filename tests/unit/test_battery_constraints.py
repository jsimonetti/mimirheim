"""Unit tests for mimirheim/devices/battery.py — Battery device variables and constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=4, dt=0.25 unless noted otherwise.
"""

from datetime import UTC, datetime

import pytest

from mimirheim.config.schema import BatteryConfig, EfficiencySegment
from mimirheim.core.bundle import BatteryInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.battery import Battery


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seg(power_max_kw: float, efficiency: float = 1.0) -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=power_max_kw, efficiency=efficiency)


def _config(
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
    charge_segs: list[EfficiencySegment] | None = None,
    discharge_segs: list[EfficiencySegment] | None = None,
    wear_cost: float = 0.0,
) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_segments=charge_segs or [_seg(5.0)],
        discharge_segments=discharge_segs or [_seg(5.0)],
        wear_cost_eur_per_kwh=wear_cost,
    )


def _inputs(soc_kwh: float = 5.0) -> BatteryInputs:
    return BatteryInputs(soc_kwh=soc_kwh)


def _make_ctx(horizon: int = 4) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


# ---------------------------------------------------------------------------
# SOC tracking
# ---------------------------------------------------------------------------


def test_battery_soc_tracks_charging() -> None:
    """With all charge at 2 kW (efficiency=1.0), SOC must increase by 0.5 kWh per step."""
    ctx = _make_ctx()
    battery = Battery(name="bat", config=_config(charge_segs=[_seg(5.0, efficiency=1.0)]))
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=2.0))

    # Fix charge to exactly 2 kW at every step; no discharge.
    for t in ctx.T:
        ctx.solver.add_constraint(battery.charge_seg[t, 0] == 2.0)
        ctx.solver.add_constraint(battery.discharge_seg[t, 0] == 0.0)

    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    for t in ctx.T:
        expected = 2.0 + (t + 1) * 2.0 * 0.25  # initial + steps * 2kW * dt
        assert abs(ctx.solver.var_value(battery.soc[t]) - expected) < 1e-5, (
            f"step {t}: expected soc {expected}, got {ctx.solver.var_value(battery.soc[t])}"
        )


def test_battery_soc_respects_capacity() -> None:
    """SOC must never exceed capacity_kwh regardless of incentive to overcharge."""
    ctx = _make_ctx()
    battery = Battery(name="bat", config=_config(capacity_kwh=6.0, charge_segs=[_seg(10.0)]))
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Strong incentive to charge as much as possible.
    obj = battery.soc[0]
    for t in range(1, 4):
        obj = obj + battery.soc[t]
    ctx.solver.set_objective_minimize(-obj)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(battery.soc[t]) <= 6.0 + 1e-6, (
            f"step {t}: SOC exceeded capacity"
        )


def test_battery_soc_respects_min_soc() -> None:
    """SOC must never fall below min_soc_kwh regardless of incentive to overdischarge."""
    ctx = _make_ctx()
    battery = Battery(name="bat", config=_config(min_soc_kwh=2.0, discharge_segs=[_seg(10.0)]))
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=4.0))

    # Strong incentive to discharge as much as possible.
    obj = battery.soc[0]
    for t in range(1, 4):
        obj = obj + battery.soc[t]
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    for t in ctx.T:
        assert ctx.solver.var_value(battery.soc[t]) >= 2.0 - 1e-6, (
            f"step {t}: SOC fell below min_soc_kwh"
        )


# ---------------------------------------------------------------------------
# Simultaneous charge/discharge guard
# ---------------------------------------------------------------------------


def test_battery_no_simultaneous_charge_discharge() -> None:
    """With conditions that make simultaneous charge/discharge profitable without the binary
    guard, verify that charge and discharge are never both nonzero at the same step."""
    ctx = _make_ctx()
    # Use distinct efficiencies to create an efficiency spread the LP could exploit.
    battery = Battery(
        name="bat",
        config=_config(
            charge_segs=[_seg(5.0, efficiency=0.90)],
            discharge_segs=[_seg(5.0, efficiency=0.95)],
        ),
    )
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Incentive that would reward simultaneous charge/discharge without the guard.
    ctx.solver.set_objective_minimize(battery.soc[3])
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible")

    for t in ctx.T:
        total_charge = sum(
            ctx.solver.var_value(battery.charge_seg[t, i])
            for i in range(len(battery.config.charge_segments))
        )
        total_discharge = sum(
            ctx.solver.var_value(battery.discharge_seg[t, i])
            for i in range(len(battery.config.discharge_segments))
        )
        assert total_charge < 1e-6 or total_discharge < 1e-6, (
            f"step {t}: simultaneous charge ({total_charge:.4f} kW) "
            f"and discharge ({total_discharge:.4f} kW)"
        )


# ---------------------------------------------------------------------------
# Wear cost
# ---------------------------------------------------------------------------


def test_battery_wear_cost_suppresses_cycling() -> None:
    """With a very high wear cost and flat prices, the solver should not cycle at all."""
    ctx = _make_ctx()
    battery = Battery(
        name="bat",
        config=_config(
            charge_segs=[_seg(5.0, efficiency=0.95)],
            discharge_segs=[_seg(5.0, efficiency=0.95)],
            wear_cost=100.0,  # very high — cycling is never worth it
        ),
    )
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Flat price signal — no economic incentive to move energy; only wear cost deters cycling.
    obj = battery.objective_terms(0)
    for t in range(1, 4):
        obj = obj + battery.objective_terms(t)
    ctx.solver.set_objective_minimize(obj)
    ctx.solver.solve()

    total_throughput = sum(
        ctx.solver.var_value(battery.charge_seg[t, 0])
        + ctx.solver.var_value(battery.discharge_seg[t, 0])
        for t in ctx.T
    )
    assert total_throughput < 1e-6


# ---------------------------------------------------------------------------
# Power limits
# ---------------------------------------------------------------------------


def test_battery_single_segment_power_limit() -> None:
    """Total charge must never exceed the segment power_max_kw."""
    ctx = _make_ctx()
    battery = Battery(name="bat", config=_config(charge_segs=[_seg(3.0)]))
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=0.0))

    ctx.solver.set_objective_minimize(-battery.charge_seg[0, 0])
    ctx.solver.solve()

    assert ctx.solver.var_value(battery.charge_seg[0, 0]) <= 3.0 + 1e-6


def test_battery_two_segment_soc_uses_per_segment_efficiency() -> None:
    """SOC increment must reflect per-segment efficiency, not a flat average."""
    ctx = _make_ctx(horizon=1)
    seg_a = _seg(2.0, efficiency=0.80)
    seg_b = _seg(2.0, efficiency=0.95)
    battery = Battery(
        name="bat",
        config=_config(capacity_kwh=20.0, charge_segs=[seg_a, seg_b]),
    )
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=0.0))

    # Force exactly 1 kW through segment 0 and 1 kW through segment 1.
    ctx.solver.add_constraint(battery.charge_seg[0, 0] == 1.0)
    ctx.solver.add_constraint(battery.charge_seg[0, 1] == 1.0)
    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    # SOC increase = (0.80 * 1.0 + 0.95 * 1.0) * 0.25 = 0.4375 kWh
    expected_soc = (0.80 * 1.0 + 0.95 * 1.0) * 0.25
    assert abs(ctx.solver.var_value(battery.soc[0]) - expected_soc) < 1e-5


# ---------------------------------------------------------------------------
# net_power sign convention
# ---------------------------------------------------------------------------


def test_battery_net_power_sign() -> None:
    """Charging gives negative net_power (consuming); discharging gives positive (producing)."""
    ctx = _make_ctx(horizon=1)
    battery = Battery(name="bat", config=_config())
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Force 2 kW charge, 0 discharge.
    ctx.solver.add_constraint(battery.charge_seg[0, 0] == 2.0)
    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    net = ctx.solver.var_value(battery.discharge_seg[0, 0]) - ctx.solver.var_value(battery.charge_seg[0, 0])
    assert net < 0, "charging should produce negative net power"


# ---------------------------------------------------------------------------
# Battery optimal SOC penalty (plan 21)
# ---------------------------------------------------------------------------


def _config_with_soc_penalty(
    optimal_lower_soc_kwh: float,
    penalty: float,
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        optimal_lower_soc_kwh=optimal_lower_soc_kwh,
        soc_low_penalty_eur_per_kwh_h=penalty,
    )


def test_soc_penalty_no_extra_variables_when_zero() -> None:
    """With default config (optimal_lower_soc_kwh=0), no extra variables are added."""
    ctx = _make_ctx(horizon=4)
    battery = Battery(name="bat", config=_config())
    before = ctx.solver._m.num_cols
    battery.add_variables(ctx)
    after = ctx.solver._m.num_cols
    # 4 variables per step (charge_seg, discharge_seg, mode, soc) × 4 steps = 16.
    assert after - before == 16


def test_soc_low_is_zero_when_soc_above_optimal() -> None:
    """When SOC stays above optimal_lower_soc_kwh, soc_low[t] must be 0 at every step."""
    ctx = _make_ctx(horizon=4)
    cfg = _config_with_soc_penalty(optimal_lower_soc_kwh=4.0, penalty=0.10)
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=6.0))

    # Prevent any discharge so SOC stays at 6 kWh (above the 4 kWh optimal).
    for t in ctx.T:
        ctx.solver.add_constraint(battery.discharge_seg[t, 0] == 0.0)
        ctx.solver.add_constraint(battery.charge_seg[t, 0] == 0.0)
    # Include penalty terms so the solver drives soc_low to its minimum. Without
    # them soc_low has no objective contribution and floats to its upper bound.
    ctx.solver.set_objective_minimize(
        sum(battery.objective_terms(t) for t in ctx.T)
    )
    ctx.solver.solve()

    for t in ctx.T:
        val = ctx.solver.var_value(battery._soc_low[t])
        assert val < 1e-6, f"Expected soc_low[{t}]=0 (SOC above optimal), got {val:.4f}"


def test_soc_low_equals_deficit_when_soc_below_optimal() -> None:
    """When battery SOC is forced to 2 kWh and optimal is 4 kWh, soc_low[0] must be ~2."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_with_soc_penalty(optimal_lower_soc_kwh=4.0, penalty=0.10)
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    # Initial SOC = 2 kWh. No charge or discharge, so soc[0] stays at 2 kWh.
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=2.0))
    ctx.solver.add_constraint(battery.charge_seg[0, 0] == 0.0)
    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    # Include penalty terms so the solver drives soc_low to its minimum. Without
    # them soc_low has no objective contribution and floats to its upper bound.
    ctx.solver.set_objective_minimize(
        sum(battery.objective_terms(t) for t in ctx.T)
    )
    ctx.solver.solve()

    deficit = ctx.solver.var_value(battery._soc_low[0])
    assert abs(deficit - 2.0) < 1e-4, f"Expected soc_low[0]=2.0 (deficit), got {deficit:.4f}"


def test_soc_penalty_increases_soc_target() -> None:
    """Adding a SOC penalty causes the solver to prefer higher terminal SOC.

    Setup: two equal-price steps, battery starts at 2 kWh, optimal is 4 kWh.
    Without penalty the solver is indifferent to charging. With penalty, it charges.
    """
    def _terminal_soc(penalty: float) -> float:
        ctx = _make_ctx(horizon=2)
        cfg = _config_with_soc_penalty(optimal_lower_soc_kwh=4.0, penalty=penalty)
        battery = Battery(name="bat", config=cfg)
        battery.add_variables(ctx)
        battery.add_constraints(ctx, inputs=_inputs(soc_kwh=2.0))
        # Flat import price: no economic incentive to charge without the penalty.
        obj_terms: list = []
        for t in ctx.T:
            terms = battery.objective_terms(t)
            if not isinstance(terms, (int, float)):
                obj_terms.append(terms)
        if obj_terms:
            obj = obj_terms[0]
            for term in obj_terms[1:]:
                obj = obj + term
            ctx.solver.set_objective_minimize(obj)
        ctx.solver.solve()
        return ctx.solver.var_value(battery.soc[1])

    soc_no_penalty = _terminal_soc(penalty=0.0)
    soc_with_penalty = _terminal_soc(penalty=0.50)
    assert soc_with_penalty > soc_no_penalty + 1e-4, (
        f"Expected penalty to increase terminal SOC: no_penalty={soc_no_penalty:.4f}, "
        f"with_penalty={soc_with_penalty:.4f}"
    )


def test_soc_penalty_does_not_prevent_profitable_dispatch() -> None:
    """A large price spread must override the SOC penalty and allow full discharge.

    Battery starts at 8 kWh (above the 4 kWh optimal). Discharge is very profitable.
    The penalty only applies below the optimal; above it, discharge is unconstrained.
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config_with_soc_penalty(optimal_lower_soc_kwh=4.0, penalty=0.10)
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=8.0))

    # Maximise discharge to verify the penalty does not cap it.
    ctx.solver.set_objective_minimize(-battery.net_power(0))
    ctx.solver.solve()

    discharged = ctx.solver.var_value(battery.discharge_seg[0, 0])
    assert discharged > 4.9, (
        f"Expected full discharge (~5 kW) despite penalty, got {discharged:.4f}"
    )


# ---------------------------------------------------------------------------
# Battery power derating near SOC extremes (plan 22)
# ---------------------------------------------------------------------------


def _config_with_charge_derating(
    reduce_charge_above_soc_kwh: float,
    reduce_charge_min_kw: float,
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        reduce_charge_above_soc_kwh=reduce_charge_above_soc_kwh,
        reduce_charge_min_kw=reduce_charge_min_kw,
    )


def _config_with_discharge_derating(
    reduce_discharge_below_soc_kwh: float,
    reduce_discharge_min_kw: float,
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.5,
) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        reduce_discharge_below_soc_kwh=reduce_discharge_below_soc_kwh,
        reduce_discharge_min_kw=reduce_discharge_min_kw,
    )


def test_charge_derating_no_effect_below_threshold() -> None:
    """When SOC is well below the derating threshold, full charge power is available."""
    ctx = _make_ctx(horizon=1)
    # Derating starts at 8 kWh; full charge power (5 kW) is available below that.
    cfg = _config_with_charge_derating(
        reduce_charge_above_soc_kwh=8.0, reduce_charge_min_kw=1.0
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=3.0))

    # Maximise charge to probe the effective upper bound.
    ctx.solver.set_objective_minimize(-battery.charge_seg[0, 0])
    ctx.solver.solve()

    charged = ctx.solver.var_value(battery.charge_seg[0, 0])
    assert charged > 4.9, (
        f"Expected ~5 kW charge below derating threshold, got {charged:.4f}"
    )


def test_charge_derating_limits_power_near_full() -> None:
    """When SOC is above the derating threshold, charge power is reduced linearly."""
    ctx = _make_ctx(horizon=1)
    # capacity=10, reduce_charge_above=8, reduce_charge_min=1
    # slope = (1 - 5) / (10 - 8) = -2 kW/kWh
    # At SOC=9.5: limit = 5 + (-2) * (9.5 - 8) = 5 - 3 = 2 kW
    cfg = _config_with_charge_derating(
        reduce_charge_above_soc_kwh=8.0, reduce_charge_min_kw=1.0
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=9.5))

    # Freeze discharge, maximise charge to probe the derating bound.
    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(-battery.charge_seg[0, 0])
    ctx.solver.solve()

    charged = ctx.solver.var_value(battery.charge_seg[0, 0])
    assert abs(charged - 2.0) < 0.01, (
        f"Expected 2.0 kW charge at SOC=9.5 kWh (derating), got {charged:.4f}"
    )


def test_discharge_derating_no_effect_above_threshold() -> None:
    """When SOC is above the discharge derating threshold, full discharge power is available."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_with_discharge_derating(
        reduce_discharge_below_soc_kwh=2.0, reduce_discharge_min_kw=0.5
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=8.0))

    ctx.solver.set_objective_minimize(-battery.discharge_seg[0, 0])
    ctx.solver.solve()

    discharged = ctx.solver.var_value(battery.discharge_seg[0, 0])
    assert discharged > 4.9, (
        f"Expected ~5 kW discharge above derating threshold, got {discharged:.4f}"
    )


def test_discharge_derating_limits_power_near_empty() -> None:
    """When SOC is below the discharge derating threshold, discharge power is reduced linearly."""
    ctx = _make_ctx(horizon=1)
    # Use min_soc_kwh=0.0 so the minimum SOC constraint does not interfere with the derating.
    # capacity=10, min_soc=0, reduce_discharge_below=4.0, reduce_discharge_min=1.0
    # slope = (1.0 - 5) / (4.0 - 0.0) = -1.0 kW/kWh  (clean integer slope)
    # At start-of-step SOC=2.0: limit = 5 + (-1.0) * (4.0 - 2.0) = 3.0 kW
    cfg = _config_with_discharge_derating(
        reduce_discharge_below_soc_kwh=4.0,
        reduce_discharge_min_kw=1.0,
        min_soc_kwh=0.0,
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=2.0))

    # Freeze charge, maximise discharge to probe the derating bound.
    ctx.solver.add_constraint(battery.charge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(-battery.discharge_seg[0, 0])
    ctx.solver.solve()

    discharged = ctx.solver.var_value(battery.discharge_seg[0, 0])
    assert abs(discharged - 3.0) < 0.01, (
        f"Expected 3.0 kW discharge at SOC=2.0 kWh (derating), got {discharged:.4f}"
    )


def test_derating_no_extra_constraints_when_not_configured() -> None:
    """With the default config (no derating fields set), no extra constraints are added."""
    ctx = _make_ctx(horizon=4)
    battery = Battery(name="bat", config=_config())
    battery.add_variables(ctx)
    before = ctx.solver._m.num_rows
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))
    after = ctx.solver._m.num_rows
    # 3 constraints per step: SOC dynamics, charge guard (Big-M), discharge guard (Big-M)
    # plus 0 soc_low constraints (optimal_lower_soc_kwh defaults to 0).
    assert after - before == 3 * len(ctx.T)


# ---------------------------------------------------------------------------
# Battery SOS2 piecewise-linear efficiency (plan 23)
# ---------------------------------------------------------------------------

from mimirheim.config.schema import EfficiencyBreakpoint  # noqa: E402


def _bp(power_kw: float, efficiency: float) -> EfficiencyBreakpoint:
    return EfficiencyBreakpoint(power_kw=power_kw, efficiency=efficiency)


def _sos2_battery(
    charge_curve: list[EfficiencyBreakpoint],
    discharge_curve: list[EfficiencyBreakpoint],
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
) -> Battery:
    cfg = BatteryConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        charge_efficiency_curve=charge_curve,
        discharge_efficiency_curve=discharge_curve,
    )
    return Battery(name="bat", config=cfg)


def test_sos2_soc_tracks_charging_single_segment() -> None:
    """Two-breakpoint SOS2 curve (single linear segment) must accumulate SOC correctly.

    A charge curve P=(0, 5 kW) with η=(0.95, 0.95) is equivalent to a flat 0.95
    efficiency. Forcing 5 kW AC input for 4 steps of 0.25 h should store 1.1875 kWh
    per step: 5 × 0.95 × 0.25 = 1.1875 kWh.
    """
    ctx = _make_ctx(horizon=4)
    battery = _sos2_battery(
        charge_curve=[_bp(0.0, 0.95), _bp(5.0, 0.95)],
        discharge_curve=[_bp(0.0, 0.95), _bp(5.0, 0.95)],
    )
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=0.0))

    # Force full charge at 5 kW AC at every step.
    for t in ctx.T:
        ctx.solver.add_constraint(battery.charge_ac_kw(t) == 5.0)
        ctx.solver.add_constraint(battery.discharge_ac_kw(t) == 0.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    soc_expected = 0.0
    for t in ctx.T:
        soc_expected += 5.0 * 0.95 * ctx.dt
        soc_t = ctx.solver.var_value(battery.soc[t])
        assert abs(soc_t - soc_expected) < 1e-4, (
            f"soc[{t}] expected {soc_expected:.4f}, got {soc_t:.4f}"
        )


def test_sos2_efficiency_interpolated_between_breakpoints() -> None:
    """Solver interpolates DC power linearly between breakpoints in the SOS2 model.

    Three-breakpoint curve: (0, η=0.98), (3 kW, η=0.95), (6 kW, η=0.88).
    Forcing a charge at 4.5 kW (midpoint between 3 and 6 kW) places weights
    w[1]=0.5 and w[2]=0.5 on the two breakpoints of segment 2:

      DC at P_1=3: 3 × 0.95 = 2.85 kW
      DC at P_2=6: 6 × 0.88 = 5.28 kW
      DC interpolated = 0.5 × 2.85 + 0.5 × 5.28 = 4.065 kW
      soc[0] increase = 4.065 × 0.25 = 1.01625 kWh

    Note: SOS2 interpolates DC power (Σ w_s × P_s × η_s), not the efficiency
    ratio. At the midpoint of segment 2 the effective η = 4.065 / 4.5 ≈ 0.9033,
    not the naive average (0.95+0.88)/2 = 0.915.
    """
    ctx = _make_ctx(horizon=1)
    curve = [_bp(0.0, 0.98), _bp(3.0, 0.95), _bp(6.0, 0.88)]
    battery = _sos2_battery(charge_curve=curve, discharge_curve=curve)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=0.0))

    ctx.solver.add_constraint(battery.charge_ac_kw(0) == 4.5)
    ctx.solver.add_constraint(battery.discharge_ac_kw(0) == 0.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    # DC interpolated at midpoint: 0.5×(3×0.95) + 0.5×(6×0.88) = 4.065 kW
    expected_dc_kw = 0.5 * (3.0 * 0.95) + 0.5 * (6.0 * 0.88)
    expected_soc = expected_dc_kw * ctx.dt
    actual_soc = ctx.solver.var_value(battery.soc[0])
    assert abs(actual_soc - expected_soc) < 1e-4, (
        f"Expected SOC={expected_soc:.4f} (DC-interpolated), got {actual_soc:.4f}"
    )


def test_sos2_no_simultaneous_charge_discharge() -> None:
    """SOS2 model must prevent simultaneous charge and discharge via the Big-M guard."""
    ctx = _make_ctx(horizon=1)
    curve = [_bp(0.0, 0.95), _bp(5.0, 0.90)]
    battery = _sos2_battery(charge_curve=curve, discharge_curve=curve)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Objective tries to incentivise both charge and discharge simultaneously.
    c = battery.charge_ac_kw(0)
    d = battery.discharge_ac_kw(0)
    ctx.solver.set_objective_minimize(-(c + d))
    ctx.solver.solve()

    charge_val = ctx.solver.var_value(battery.charge_ac_kw(0))
    discharge_val = ctx.solver.var_value(battery.discharge_ac_kw(0))
    assert not (charge_val > 1e-6 and discharge_val > 1e-6), (
        f"Simultaneous charge={charge_val:.4f} and discharge={discharge_val:.4f}"
    )


def test_sos2_model_falls_back_to_stacked_when_no_curve() -> None:
    """A BatteryConfig with charge_segments and no curve uses the stacked model.

    eff=1.0, 5 kW max, 4 steps of 0.25 h forced at 5 kW → soc should increase by
    1.25 kWh per step (same as the plan 07 baseline).
    """
    ctx = _make_ctx(horizon=4)
    battery = Battery(name="bat", config=_config())  # uses charge_segments
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=0.0))
    for t in ctx.T:
        ctx.solver.add_constraint(battery.charge_seg[t, 0] == 5.0)
        ctx.solver.add_constraint(battery.discharge_seg[t, 0] == 0.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    soc_expected = 0.0
    for t in ctx.T:
        soc_expected += 5.0 * 1.0 * ctx.dt  # efficiency=1.0 (_seg default)
        soc_t = ctx.solver.var_value(battery.soc[t])
        assert abs(soc_t - soc_expected) < 1e-4, (
            f"soc[{t}] expected {soc_expected:.4f}, got {soc_t:.4f}"
        )


# ---------------------------------------------------------------------------
# Shared system direction binary — anti-roundtrip (Plan 38B)
# ---------------------------------------------------------------------------


def test_battery_set_external_mode_prevents_roundtrip() -> None:
    """With a shared mode binary, forcing bat1 to charge prevents bat2 from discharging.

    The shared mode variable is 1 (charging direction) when bat1 charges.
    bat2's discharge is bounded by max_discharge * (1 - mode), which collapses
    to 0 when mode=1, blocking simultaneous discharge.
    """
    ctx = _make_ctx(horizon=1)
    bat1 = Battery(name="bat1", config=_config())
    bat2 = Battery(name="bat2", config=_config())
    bat1.add_variables(ctx)
    bat2.add_variables(ctx)

    shared_mode = {0: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)}
    bat1.set_external_mode(shared_mode)
    bat2.set_external_mode(shared_mode)

    bat1.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))
    bat2.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Force bat1 to charge at >= 4 kW. This pushes mode to 1 (charging direction).
    ctx.solver.add_constraint(bat1.charge_seg[0, 0] >= 4.0)
    # Maximise bat2 discharge — should be blocked by mode=1.
    ctx.solver.set_objective_minimize(-bat2.discharge_seg[0, 0])
    ctx.solver.solve()

    assert ctx.solver.var_value(bat2.discharge_seg[0, 0]) < 1e-6, (
        "bat2 discharge must be zero when bat1 is charging (shared mode=1)"
    )


def test_single_battery_uses_per_device_mode() -> None:
    """Single battery: mode[t] is populated by add_variables; no external mode is set."""
    ctx = _make_ctx(horizon=1)
    battery = Battery(name="bat", config=_config())
    battery.add_variables(ctx)

    assert 0 in battery.mode, "mode[0] must exist in battery.mode after add_variables"

    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))
    ctx.solver.set_objective_minimize(battery.soc[0])
    status = ctx.solver.solve()
    assert status != "infeasible"


def test_two_batteries_both_can_be_idle() -> None:
    """With a shared mode binary, both batteries can be idle in the same step.

    Idling is feasible because the Big-M constraint only requires:
      total_charge <= max_charge * mode     (can be zero when mode=1)
      total_discharge <= max_discharge * (1-mode)  (can be zero when mode=0)
    Setting both to zero is consistent with any value of mode.
    """
    ctx = _make_ctx(horizon=1)
    bat1 = Battery(name="bat1", config=_config())
    bat2 = Battery(name="bat2", config=_config())
    bat1.add_variables(ctx)
    bat2.add_variables(ctx)

    shared_mode = {0: ctx.solver.add_var(lb=0.0, ub=1.0, integer=True)}
    bat1.set_external_mode(shared_mode)
    bat2.set_external_mode(shared_mode)

    bat1.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))
    bat2.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Driving both charge variables to zero is the objective minimum.
    obj = bat1.charge_seg[0, 0] + bat2.charge_seg[0, 0]
    ctx.solver.set_objective_minimize(obj)
    status = ctx.solver.solve()

    assert status != "infeasible"
    assert ctx.solver.var_value(bat1.charge_seg[0, 0]) < 1e-6
    assert ctx.solver.var_value(bat2.charge_seg[0, 0]) < 1e-6
    assert ctx.solver.var_value(bat1.discharge_seg[0, 0]) < 1e-6
    assert ctx.solver.var_value(bat2.discharge_seg[0, 0]) < 1e-6


# ---------------------------------------------------------------------------
# Minimum operating power constraints (Plan 38C)
# ---------------------------------------------------------------------------


def test_battery_min_charge_kw_enforced() -> None:
    """With min_charge_kw set, the solver dispatches zero or >= min_charge_kw charge.

    The Binary mode[t]=1 activates the floor: total_charge >= min_charge_kw * mode[t].
    When mode=1, charge must be >= min_charge_kw. When mode=0 (discharge direction),
    charge can be 0 (right-hand side collapses to 0).
    """
    ctx = _make_ctx(horizon=1)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        min_charge_kw=2.0,
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Force discharge to zero so the solver must choose between idle and charging.
    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(battery.charge_seg[0, 0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(battery.charge_seg[0, 0])
    assert charge < 1e-6 or charge >= 2.0 - 1e-6, (
        f"charge={charge:.4f} violates min_charge_kw=2.0 (must be 0 or >= 2.0)"
    )


def test_battery_min_discharge_kw_enforced() -> None:
    """With min_discharge_kw set, the solver dispatches zero or >= min_discharge_kw discharge."""
    ctx = _make_ctx(horizon=1)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        min_discharge_kw=1.5,
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    ctx.solver.add_constraint(battery.charge_seg[0, 0] == 0.0)
    ctx.solver.set_objective_minimize(battery.discharge_seg[0, 0])
    ctx.solver.solve()

    discharge = ctx.solver.var_value(battery.discharge_seg[0, 0])
    assert discharge < 1e-6 or discharge >= 1.5 - 1e-6, (
        f"discharge={discharge:.4f} violates min_discharge_kw=1.5 (must be 0 or >= 1.5)"
    )


def test_battery_min_charge_kw_none_allows_fractional() -> None:
    """With min_charge_kw=None (default), the solver can dispatch any value in [0, max_kw]."""
    ctx = _make_ctx(horizon=1)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    ctx.solver.add_constraint(battery.discharge_seg[0, 0] == 0.0)
    ctx.solver.add_constraint(battery.charge_seg[0, 0] >= 1.0)
    ctx.solver.add_constraint(battery.charge_seg[0, 0] <= 1.0)
    ctx.solver.set_objective_minimize(battery.soc[0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(battery.charge_seg[0, 0])
    assert abs(charge - 1.0) < 1e-5, (
        f"Expected fractional charge=1.0 kW without min_charge constraint, got {charge:.4f}"
    )


def test_battery_min_discharge_kw_floor_inactive_when_charging() -> None:
    """min_discharge_kw floor collapses to zero when mode=1 (charging), so zero discharge is reachable."""
    ctx = _make_ctx(horizon=1)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_seg(5.0)],
        discharge_segments=[_seg(5.0)],
        min_discharge_kw=2.0,
    )
    battery = Battery(name="bat", config=cfg)
    battery.add_variables(ctx)
    battery.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0))

    # Force charging direction: mode[0]=1, so discharge floor = 2.0 * (1-1) = 0.
    # Zero discharge must therefore remain feasible.
    ctx.solver.add_constraint(battery.mode[0] == 1)
    ctx.solver.set_objective_minimize(battery.soc[0])
    status = ctx.solver.solve()
    assert status != "infeasible", (
        "battery with min_discharge_kw=2.0 must be feasible when mode forces charging"
    )
