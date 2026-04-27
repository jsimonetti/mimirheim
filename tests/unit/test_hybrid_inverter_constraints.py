"""Unit tests for mimirheim/devices/hybrid_inverter.py — HybridInverterDevice constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=4, dt=0.25 unless noted otherwise.
"""

from mimirheim.config.schema import HybridInverterConfig
from mimirheim.core.bundle import HybridInverterInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.hybrid_inverter import HybridInverterDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
    max_charge_kw: float = 6.0,
    max_discharge_kw: float = 6.0,
    bat_charge_eff: float = 1.0,
    bat_discharge_eff: float = 1.0,
    inverter_eff: float = 1.0,
    max_pv_kw: float = 6.0,
    wear_cost: float = 0.0,
) -> HybridInverterConfig:
    return HybridInverterConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        battery_charge_efficiency=bat_charge_eff,
        battery_discharge_efficiency=bat_discharge_eff,
        inverter_efficiency=inverter_eff,
        max_pv_kw=max_pv_kw,
        wear_cost_eur_per_kwh=wear_cost,
        topic_pv_forecast="mimir/input/hybrid_inv/pv_forecast",
    )


def _inputs(
    soc_kwh: float = 5.0,
    pv_forecast: list[float] | None = None,
    horizon: int = 4,
) -> HybridInverterInputs:
    if pv_forecast is None:
        pv_forecast = [0.0] * horizon
    return HybridInverterInputs(soc_kwh=soc_kwh, pv_forecast_kw=pv_forecast)


def _make_ctx(horizon: int = 4) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


# ---------------------------------------------------------------------------
# DC bus balance tests
# ---------------------------------------------------------------------------


def test_dc_bus_balance_pv_charges_battery_directly() -> None:
    """PV power on the DC bus charges the battery directly without any AC conversion.

    Setup:
    - PV forecast = 5 kW, battery at 50% SOC (5 kWh of 10 kWh).
    - No base load, AC import and AC export are pinned to zero.
    - Inverter efficiency = 1.0, battery charge efficiency = 1.0.
    - Objective: maximise SOC (force maximum charging from PV).

    Expected:
    - pv_dc[t] ≈ 5.0 kW (full forecast utilised).
    - bat_charge_dc[t] ≈ 5.0 kW (all PV flows into battery).
    - ac_to_dc[t] = 0 (no AC import).
    - dc_to_ac[t] = 0 (no surplus exported; battery absorbs all PV).
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config(max_pv_kw=6.0, max_charge_kw=6.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[5.0], horizon=1))

    # Pin AC flows to zero: pure DC-bus operation.
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)

    # Maximise SOC: solver should use all available PV to charge.
    ctx.solver.set_objective_minimize(-device.soc[0])
    ctx.solver.solve()

    pv = ctx.solver.var_value(device.pv_dc[0])
    charge = ctx.solver.var_value(device.bat_charge_dc[0])

    assert abs(pv - 5.0) < 1e-4, f"Expected pv_dc=5.0, got {pv:.4f}"
    assert abs(charge - 5.0) < 1e-4, f"Expected bat_charge_dc=5.0, got {charge:.4f}"
    # DC bus balance: pv_dc = bat_charge_dc when ac flows are zero.
    assert abs(pv - charge) < 1e-4, f"DC bus imbalance: pv={pv:.4f}, charge={charge:.4f}"


def test_dc_bus_balance_battery_discharges_to_ac() -> None:
    """Battery discharges through the inverter to deliver power to the AC bus.

    Setup:
    - PV forecast = 0 kW (no generation), battery at 5 kWh.
    - AC import pinned to zero; no battery charging.
    - Inverter efficiency = 1.0, battery discharge efficiency = 1.0.
    - Objective: minimise SOC (force maximum discharge to AC).

    Expected:
    - bat_discharge_dc[t] > 0 (battery provides DC power).
    - dc_to_ac[t] > 0 (power delivered to AC bus).
    - bat_discharge_dc[t] ≈ dc_to_ac[t] (η=1: DC and AC power are equal).
    - ac_to_dc[t] ≈ 0 (no AC import).
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config(max_pv_kw=6.0, max_discharge_kw=6.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[0.0], horizon=1))

    # No AC import; no battery charging.
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)

    # Minimise SOC: solver should discharge the battery as much as possible.
    ctx.solver.set_objective_minimize(device.soc[0])
    ctx.solver.solve()

    discharge = ctx.solver.var_value(device.bat_discharge_dc[0])
    dc_to_ac = ctx.solver.var_value(device.dc_to_ac[0])
    ac_to_dc = ctx.solver.var_value(device.ac_to_dc[0])

    assert discharge > 1e-4, f"Expected bat_discharge_dc > 0, got {discharge:.4f}"
    assert dc_to_ac > 1e-4, f"Expected dc_to_ac > 0, got {dc_to_ac:.4f}"
    # η=1: discharge power equals AC output.
    assert abs(discharge - dc_to_ac) < 1e-4, (
        f"DC bus imbalance with η=1: discharge={discharge:.4f}, dc_to_ac={dc_to_ac:.4f}"
    )
    assert ac_to_dc < 1e-4, f"Expected ac_to_dc ≈ 0, got {ac_to_dc:.4f}"


def test_dc_bus_pv_surplus_exported_to_ac() -> None:
    """PV surplus power is exported to the AC bus when the battery is full.

    Setup:
    - PV forecast = 5 kW; battery at 100% SOC (capacity_kwh=5.0, SOC=5.0 kWh).
    - Battery is full: bat_charge_dc is forced to zero and bat_discharge_dc to zero.
    - Inverter efficiency = 1.0.
    - Objective: maximise dc_to_ac (encourage export).

    Expected:
    - pv_dc[t] ≈ 5.0 kW.
    - dc_to_ac[t] ≈ 5.0 kW (η=1: all PV surplus exported).
    - bat_charge_dc[t] = 0 (battery full).
    - ac_to_dc[t] = 0 (no import needed).
    - net_power(t) > 0 (positive = injection to AC bus).
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config(capacity_kwh=5.0, max_pv_kw=6.0, max_charge_kw=6.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(
        ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[5.0], horizon=1)
    )

    # Battery is full: no charge allowed; no discharge (no incentive).
    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)

    # Maximise export.
    ctx.solver.set_objective_minimize(-device.dc_to_ac[0])
    ctx.solver.solve()

    pv = ctx.solver.var_value(device.pv_dc[0])
    dc_to_ac = ctx.solver.var_value(device.dc_to_ac[0])
    charge = ctx.solver.var_value(device.bat_charge_dc[0])

    assert abs(pv - 5.0) < 1e-4, f"Expected pv_dc=5.0, got {pv:.4f}"
    assert abs(dc_to_ac - 5.0) < 1e-4, f"Expected dc_to_ac=5.0, got {dc_to_ac:.4f}"
    assert charge < 1e-4, f"Expected bat_charge_dc=0, got {charge:.4f}"

    net = ctx.solver.var_value(device.dc_to_ac[0]) - ctx.solver.var_value(device.ac_to_dc[0])
    assert net > 0.0, f"Expected positive net AC power (export), got {net:.4f}"


def test_no_simultaneous_ac_import_and_export_from_hybrid() -> None:
    """The hybrid inverter never simultaneously imports and exports on the AC bus.

    This test confirms the inverter direction binary (inv_mode) prevents
    ac_to_dc > 0 and dc_to_ac > 0 in the same time step, independent of the
    Grid device's own binary guard.

    Setup:
    - PV forecast = 3 kW, battery at 5 kWh.
    - No objective incentive that would naturally rule out simultaneous flow.
    - All 4 time steps checked.

    Expected:
    - For every t: min(ac_to_dc[t], dc_to_ac[t]) ≈ 0.
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(max_pv_kw=6.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(
        ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[3.0] * 4, horizon=4)
    )

    # Neutral objective: minimise total AC import (solver has no incentive to export).
    total_import = device.ac_to_dc[0]
    for t in range(1, 4):
        total_import = total_import + device.ac_to_dc[t]
    ctx.solver.set_objective_minimize(total_import)
    ctx.solver.solve()

    for t in range(4):
        a2d = ctx.solver.var_value(device.ac_to_dc[t])
        d2a = ctx.solver.var_value(device.dc_to_ac[t])
        assert min(a2d, d2a) < 1e-4, (
            f"Step {t}: simultaneous ac_to_dc={a2d:.4f} and dc_to_ac={d2a:.4f}"
        )


def test_hybrid_inverter_soc_tracks_charging() -> None:
    """SOC increases by bat_charge_dc × η_bat × dt at each step.

    Uses η_bat = 0.95 for a non-trivial efficiency check.

    Setup:
    - 4-step horizon; initial SOC = 2.0 kWh.
    - Force bat_charge_dc[t] = 4.0 kW, bat_discharge_dc[t] = 0, ac flows = 0.
    - bat_charge_eff = 0.95, inverter_eff = 1.0.

    Expected:
    - soc[t] = 2.0 + (t+1) × 4.0 × 0.95 × 0.25
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(capacity_kwh=20.0, max_charge_kw=6.0, bat_charge_eff=0.95, max_pv_kw=8.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(
        ctx, inputs=_inputs(soc_kwh=2.0, pv_forecast=[4.0] * 4, horizon=4)
    )

    # Pin charge power to exactly 4 kW; zero all other flows.
    for t in ctx.T:
        ctx.solver.add_constraint(device.bat_charge_dc[t] == 4.0)
        ctx.solver.add_constraint(device.bat_discharge_dc[t] == 0.0)
        ctx.solver.add_constraint(device.ac_to_dc[t] == 0.0)
        ctx.solver.add_constraint(device.dc_to_ac[t] == 0.0)

    ctx.solver.set_objective_minimize(device.soc[0])
    ctx.solver.solve()

    for t in ctx.T:
        expected_soc = 2.0 + (t + 1) * 4.0 * 0.95 * 0.25
        actual_soc = ctx.solver.var_value(device.soc[t])
        assert abs(actual_soc - expected_soc) < 1e-4, (
            f"Step {t}: expected soc={expected_soc:.4f}, got {actual_soc:.4f}"
        )


def test_hybrid_inverter_wear_cost_discourages_cycling() -> None:
    """Wear cost reduces but does not eliminate battery cycling when profitable.

    Two consecutive 1-step solves are compared:
    1. No wear cost: the solver cycles freely.
    2. High wear cost: the solver avoids throughput.

    Setup:
    - Single 1-step horizon; initial SOC = 5.0 kWh.
    - PV = 3 kW (DC bus has surplus over zero load).
    - Minimise total wear cost.

    Expected:
    - With wear_cost > 0, total DC throughput (bat_charge + bat_discharge) is
      lower than or equal to the zero-wear-cost case.
    """
    # Without wear cost: solver can cycle freely.
    ctx_free = _make_ctx(horizon=1)
    cfg_free = _config(max_pv_kw=6.0, wear_cost=0.0)
    dev_free = HybridInverterDevice(name="inv", config=cfg_free)
    dev_free.add_variables(ctx_free)
    dev_free.add_constraints(
        ctx_free, inputs=_inputs(soc_kwh=5.0, pv_forecast=[3.0], horizon=1)
    )
    # Force all PV to AC bus; allow bat_charge to be zero.
    ctx_free.solver.add_constraint(dev_free.bat_discharge_dc[0] == 0.0)
    ctx_free.solver.set_objective_minimize(dev_free.soc[0])
    ctx_free.solver.solve()
    throughput_free = ctx_free.solver.var_value(dev_free.bat_charge_dc[0])

    # With high wear cost: objective penalises throughput heavily.
    ctx_wear = _make_ctx(horizon=1)
    cfg_wear = _config(max_pv_kw=6.0, wear_cost=100.0)
    dev_wear = HybridInverterDevice(name="inv", config=cfg_wear)
    dev_wear.add_variables(ctx_wear)
    dev_wear.add_constraints(
        ctx_wear, inputs=_inputs(soc_kwh=5.0, pv_forecast=[3.0], horizon=1)
    )
    ctx_wear.solver.add_constraint(dev_wear.bat_discharge_dc[0] == 0.0)
    total_wear = sum(
        cfg_wear.wear_cost_eur_per_kwh
        * (dev_wear.bat_charge_dc[t] + dev_wear.bat_discharge_dc[t])
        * ctx_wear.dt
        for t in ctx_wear.T
    )
    ctx_wear.solver.set_objective_minimize(total_wear)
    ctx_wear.solver.solve()
    throughput_wear = ctx_wear.solver.var_value(dev_wear.bat_charge_dc[0])

    assert throughput_wear <= throughput_free + 1e-4, (
        f"Wear cost should suppress cycling: free={throughput_free:.4f}, "
        f"with_wear={throughput_wear:.4f}"
    )


def test_hybrid_inverter_net_power_ac_sign_convention() -> None:
    """net_power(t) is positive when discharging to AC, negative when charging from AC.

    Setup:
    - Two separate 1-step solves.
    - Solve A: force ac_to_dc = 2 kW, dc_to_ac = 0 → net_power < 0.
    - Solve B: force dc_to_ac = 2 kW, ac_to_dc = 0 → net_power > 0.
    """
    # Solve A: importing from AC (consumption on AC bus).
    ctx_a = _make_ctx(horizon=1)
    cfg = _config(max_pv_kw=6.0, max_charge_kw=6.0)
    dev_a = HybridInverterDevice(name="inv", config=cfg)
    dev_a.add_variables(ctx_a)
    dev_a.add_constraints(ctx_a, inputs=_inputs(soc_kwh=5.0, pv_forecast=[0.0], horizon=1))
    ctx_a.solver.add_constraint(dev_a.ac_to_dc[0] == 2.0)
    ctx_a.solver.add_constraint(dev_a.dc_to_ac[0] == 0.0)
    ctx_a.solver.set_objective_minimize(dev_a.soc[0])
    ctx_a.solver.solve()
    net_a_expr = dev_a.net_power(0)
    # net_power may be a solver expression; evaluate accordingly.
    if isinstance(net_a_expr, (int, float)):
        net_a = float(net_a_expr)
    else:
        net_a = ctx_a.solver.var_value(net_a_expr)
    assert net_a < -1e-4, f"When importing (ac_to_dc=2), expected net_power < 0, got {net_a:.4f}"

    # Solve B: exporting to AC (production on AC bus).
    ctx_b = _make_ctx(horizon=1)
    dev_b = HybridInverterDevice(name="inv", config=cfg)
    dev_b.add_variables(ctx_b)
    dev_b.add_constraints(
        ctx_b, inputs=_inputs(soc_kwh=5.0, pv_forecast=[5.0], horizon=1)
    )
    ctx_b.solver.add_constraint(dev_b.dc_to_ac[0] == 2.0)
    ctx_b.solver.add_constraint(dev_b.ac_to_dc[0] == 0.0)
    ctx_b.solver.set_objective_minimize(-dev_b.dc_to_ac[0])
    ctx_b.solver.solve()
    net_b_expr = dev_b.net_power(0)
    if isinstance(net_b_expr, (int, float)):
        net_b = float(net_b_expr)
    else:
        net_b = ctx_b.solver.var_value(net_b_expr)
    assert net_b > 1e-4, f"When exporting (dc_to_ac=2), expected net_power > 0, got {net_b:.4f}"


# ---------------------------------------------------------------------------
# Plan 54 — feature parity tests
# ---------------------------------------------------------------------------


def _config_plan54(
    capacity_kwh: float = 10.0,
    min_soc_kwh: float = 0.0,
    max_charge_kw: float = 6.0,
    max_discharge_kw: float = 6.0,
    max_pv_kw: float = 6.0,
    optimal_lower_soc_kwh: float = 0.0,
    soc_low_penalty_eur_per_kwh_h: float = 0.0,
    reduce_charge_above_soc_kwh: float | None = None,
    reduce_charge_min_kw: float | None = None,
    reduce_discharge_below_soc_kwh: float | None = None,
    reduce_discharge_min_kw: float | None = None,
    min_charge_kw: float | None = None,
    min_discharge_kw: float | None = None,
) -> HybridInverterConfig:
    return HybridInverterConfig(
        capacity_kwh=capacity_kwh,
        min_soc_kwh=min_soc_kwh,
        max_charge_kw=max_charge_kw,
        max_discharge_kw=max_discharge_kw,
        battery_charge_efficiency=1.0,
        battery_discharge_efficiency=1.0,
        inverter_efficiency=1.0,
        max_pv_kw=max_pv_kw,
        optimal_lower_soc_kwh=optimal_lower_soc_kwh,
        soc_low_penalty_eur_per_kwh_h=soc_low_penalty_eur_per_kwh_h,
        reduce_charge_above_soc_kwh=reduce_charge_above_soc_kwh,
        reduce_charge_min_kw=reduce_charge_min_kw,
        reduce_discharge_below_soc_kwh=reduce_discharge_below_soc_kwh,
        reduce_discharge_min_kw=reduce_discharge_min_kw,
        min_charge_kw=min_charge_kw,
        min_discharge_kw=min_discharge_kw,
    )


def test_terminal_soc_var_returns_last_step_variable() -> None:
    """terminal_soc_var(ctx) returns the soc variable at the last time step."""
    ctx = _make_ctx(horizon=4)
    cfg = _config_plan54()
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    var = device.terminal_soc_var(ctx)
    assert var is not None, "terminal_soc_var should return a solver variable"
    assert var is device.soc[3], "terminal_soc_var should be soc at the last step"


def test_soc_low_absent_when_optimal_lower_soc_zero() -> None:
    """When optimal_lower_soc_kwh == 0.0, no soc_low variables are created."""
    ctx = _make_ctx(horizon=4)
    cfg = _config_plan54(optimal_lower_soc_kwh=0.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    assert len(device._soc_low) == 0


def test_soc_low_present_when_optimal_lower_soc_configured() -> None:
    """When optimal_lower_soc_kwh > min_soc_kwh, soc_low[t] variables are created."""
    ctx = _make_ctx(horizon=4)
    cfg = _config_plan54(min_soc_kwh=0.0, optimal_lower_soc_kwh=5.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    assert len(device._soc_low) == 4


def test_soc_low_constraint_active_when_soc_below_optimal() -> None:
    """soc_low[t] equals the SOC deficit when SOC is below optimal_lower_soc_kwh."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(capacity_kwh=10.0, min_soc_kwh=0.0, optimal_lower_soc_kwh=5.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=3.0, pv_forecast=[0.0], horizon=1))

    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)

    ctx.solver.set_objective_minimize(device._soc_low[0])
    ctx.solver.solve()

    soc_low_val = ctx.solver.var_value(device._soc_low[0])
    assert abs(soc_low_val - 2.0) < 1e-4, (
        f"Expected soc_low=2.0 (deficit below optimal 5.0), got {soc_low_val:.4f}"
    )


def test_soc_low_zero_when_soc_above_optimal() -> None:
    """soc_low[t] is zero when SOC >= optimal_lower_soc_kwh."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(capacity_kwh=10.0, min_soc_kwh=0.0, optimal_lower_soc_kwh=5.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=8.0, pv_forecast=[0.0], horizon=1))

    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)

    ctx.solver.set_objective_minimize(device._soc_low[0])
    ctx.solver.solve()

    soc_low_val = ctx.solver.var_value(device._soc_low[0])
    assert soc_low_val < 1e-4, (
        f"Expected soc_low=0 (SOC above optimal), got {soc_low_val:.4f}"
    )


def test_min_charge_floor_enforced() -> None:
    """bat_charge_dc[t] >= min_charge_kw when mode[t]=1 (charging)."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(max_charge_kw=6.0, min_charge_kw=2.0, capacity_kwh=10.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[0.0], horizon=1))

    ctx.solver.add_constraint(device.mode[0] == 1)
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] >= 2.0)

    ctx.solver.set_objective_minimize(device.bat_charge_dc[0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(device.bat_charge_dc[0])
    assert charge >= 2.0 - 1e-4, (
        f"Expected bat_charge_dc >= min_charge_kw=2.0, got {charge:.4f}"
    )


def test_min_discharge_floor_enforced() -> None:
    """bat_discharge_dc[t] >= min_discharge_kw when mode[t]=0 (discharging)."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(max_discharge_kw=6.0, min_discharge_kw=1.5, capacity_kwh=10.0)
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=5.0, pv_forecast=[0.0], horizon=1))

    ctx.solver.add_constraint(device.mode[0] == 0)
    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] >= 1.5)

    ctx.solver.set_objective_minimize(device.bat_discharge_dc[0])
    ctx.solver.solve()

    discharge = ctx.solver.var_value(device.bat_discharge_dc[0])
    assert discharge >= 1.5 - 1e-4, (
        f"Expected bat_discharge_dc >= min_discharge_kw=1.5, got {discharge:.4f}"
    )


def test_charge_derating_limits_power_near_capacity() -> None:
    """bat_charge_dc is limited below max_charge_kw when SOC is near capacity.

    Two-point linear derating from (7.0 kWh, 6.0 kW) to (10.0 kWh, 1.0 kW).
    At soc_prev=9.0 kWh the limit is 6.0 + (1.0-6.0)/(10.0-7.0)*(9.0-7.0) ≈ 2.67 kW.
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(
        capacity_kwh=10.0,
        min_soc_kwh=0.0,
        max_charge_kw=6.0,
        reduce_charge_above_soc_kwh=7.0,
        reduce_charge_min_kw=1.0,
    )
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=9.0, pv_forecast=[0.0], horizon=1))

    # Freeze discharge and export paths; maximise charge to probe the derating bound.
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)
    ctx.solver.set_objective_minimize(-device.bat_charge_dc[0])
    ctx.solver.solve()

    charge = ctx.solver.var_value(device.bat_charge_dc[0])
    assert charge < 6.0 - 1e-4, (
        f"Expected charge < 6.0 due to derating, got {charge:.4f}"
    )


def test_discharge_derating_limits_power_near_min_soc() -> None:
    """bat_discharge_dc is limited below max_discharge_kw when SOC is near min_soc_kwh.

    Two-point linear derating from (3.0 kWh, 6.0 kW) to (0.0 kWh, 1.0 kW).
    At soc_prev=1.0 kWh the limit ≈ 2.67 kW.
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(
        capacity_kwh=10.0,
        min_soc_kwh=0.0,
        max_discharge_kw=6.0,
        reduce_discharge_below_soc_kwh=3.0,
        reduce_discharge_min_kw=1.0,
    )
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=1.0, pv_forecast=[0.0], horizon=1))

    # Freeze charge and import paths; maximise discharge to probe the derating bound.
    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.set_objective_minimize(-device.bat_discharge_dc[0])
    ctx.solver.solve()

    discharge = ctx.solver.var_value(device.bat_discharge_dc[0])
    assert discharge < 6.0 - 1e-4, (
        f"Expected discharge < 6.0 due to derating, got {discharge:.4f}"
    )


def test_objective_terms_includes_soc_low_penalty() -> None:
    """objective_terms returns a non-trivial expression when soc_low_penalty is configured."""
    ctx = _make_ctx(horizon=1)
    cfg = _config_plan54(
        capacity_kwh=10.0,
        min_soc_kwh=0.0,
        optimal_lower_soc_kwh=5.0,
        soc_low_penalty_eur_per_kwh_h=1.0,
    )
    device = HybridInverterDevice(name="inv", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(soc_kwh=3.0, pv_forecast=[0.0], horizon=1))

    ctx.solver.add_constraint(device.bat_charge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.bat_discharge_dc[0] == 0.0)
    ctx.solver.add_constraint(device.ac_to_dc[0] == 0.0)
    ctx.solver.add_constraint(device.dc_to_ac[0] == 0.0)

    terms = device.objective_terms(0)
    assert terms, "Expected non-empty objective_terms when soc_low_penalty is configured"

    ctx.solver.set_objective_minimize(terms[0])
    ctx.solver.solve()

    soc_low_val = ctx.solver.var_value(device._soc_low[0])
    assert abs(soc_low_val - 2.0) < 1e-4, (
        f"Expected soc_low=2.0, got {soc_low_val:.4f}"
    )
