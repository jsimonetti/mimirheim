"""Unit tests for mimirheim/devices/combi_heat_pump.py — CombiHeatPumpDevice constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=8, dt=0.25 unless noted otherwise.

Default fixture: CombiHeatPumpConfig with elec_power_kw=6.0, cop_dhw=2.8,
cop_sh=3.8, volume_liters=200, setpoint_c=55.0, min_temp_c=40.0,
cooling_rate_k_per_hour=2.0, min_run_steps=4.
Default inputs: CombiHeatPumpInputs with current_temp_c=45.0, heat_needed_kwh=5.0.
"""

from mimirheim.config.schema import CombiHeatPumpConfig
from mimirheim.core.bundle import CombiHeatPumpInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.combi_heat_pump import CombiHeatPumpDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WATER_CAP = 4186 / 3600 / 1000  # kWh/(litre·K), same constant as device


def _config(
    elec_power_kw: float = 6.0,
    cop_dhw: float = 2.8,
    cop_sh: float = 3.8,
    volume_liters: float = 200.0,
    setpoint_c: float = 55.0,
    min_temp_c: float = 40.0,
    cooling_rate_k_per_hour: float = 2.0,
    min_run_steps: int = 4,
    wear_cost: float = 0.0,
) -> CombiHeatPumpConfig:
    return CombiHeatPumpConfig(
        elec_power_kw=elec_power_kw,
        cop_dhw=cop_dhw,
        cop_sh=cop_sh,
        volume_liters=volume_liters,
        setpoint_c=setpoint_c,
        min_temp_c=min_temp_c,
        cooling_rate_k_per_hour=cooling_rate_k_per_hour,
        min_run_steps=min_run_steps,
        wear_cost_eur_per_kwh=wear_cost,
    )


def _inputs(current_temp_c: float = 45.0, heat_needed_kwh: float = 5.0) -> CombiHeatPumpInputs:
    return CombiHeatPumpInputs(current_temp_c=current_temp_c, heat_needed_kwh=heat_needed_kwh)


def _make_ctx(horizon: int = 8) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


# ---------------------------------------------------------------------------
# Mutual exclusion
# ---------------------------------------------------------------------------


def test_combi_mutual_exclusion_no_simultaneous_modes() -> None:
    """At every step, dhw_mode[t] + sh_mode[t] <= 1.

    Configure a scenario where SH demand is non-zero so the solver has reason
    to use SH mode, while the DHW tank starts well above minimum so the solver
    can also use DHW mode. The solver must not set both binaries to 1 at any step.

    Note: current_temp_c=45.0 is chosen to keep the model feasible. Starting
    near min_temp_c (40°C) produces an infeasible model given this config's
    thermal parameters: a single DHW step raises the tank by ~18°C (overshoots
    setpoint_c=55°C), while skipping heating drops it below min_temp_c in one step.
    """
    ctx = _make_ctx(horizon=8)
    cfg = _config(min_run_steps=0)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0, heat_needed_kwh=5.0))

    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Expected feasible solve, got {status!r}"

    for t in ctx.T:
        dhw = round(ctx.solver.var_value(device._dhw_mode[t]))
        sh = round(ctx.solver.var_value(device._sh_mode[t]))
        assert dhw + sh <= 1, (
            f"Step {t}: mutual exclusion violated — dhw_mode={dhw}, sh_mode={sh}"
        )


# ---------------------------------------------------------------------------
# DHW tank temperature tracking
# ---------------------------------------------------------------------------


def test_combi_dhw_tank_tracks_temperature() -> None:
    """DHW tank temperature dynamics equal ThermalBoilerDevice with cop=cop_dhw.

    Set heat_needed_kwh=0.0 (no SH demand) so only DHW mode is active.
    Start the tank at 44.0 °C. After 1 step of DHW heating, the temperature
    should rise by elec_power_kw × cop_dhw × dt / (volume × water_cap).

    setpoint_c is set high (90.0 °C) so that one heating step never hits the
    upper bound, keeping the model feasible.
    """
    cfg = _config(
        elec_power_kw=6.0,
        cop_dhw=2.8,
        volume_liters=200.0,
        setpoint_c=90.0,   # high enough that one heating step never hits the bound
        min_temp_c=40.0,
        cooling_rate_k_per_hour=0.0,  # no cooling, isolate heating rise
        min_run_steps=0,
    )
    ctx = _make_ctx(horizon=1)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=44.0, heat_needed_kwh=0.0))

    # Force DHW mode on for step 0.
    ctx.solver.add_constraint(device._dhw_mode[0] == 1)
    ctx.solver.set_objective_minimize(device._dhw_mode[0])
    ctx.solver.solve()

    thermal_cap = 200.0 * _WATER_CAP
    expected_rise = 6.0 * 2.8 * 0.25 / thermal_cap
    expected_temp = 44.0 + expected_rise
    actual_temp = ctx.solver.var_value(device._T_tank[0])
    assert abs(actual_temp - expected_temp) < 1e-3, (
        f"Expected tank temp {expected_temp:.4f} °C, got {actual_temp:.4f} °C"
    )


def test_combi_sh_produces_required_heat() -> None:
    """Solver satisfies SH demand when DHW tank needs no heating.

    Set current_temp_c = setpoint_c (tank already full). The solver only
    activates SH mode to meet heat_needed_kwh.
    """
    cfg = _config(
        cop_sh=3.8,
        setpoint_c=55.0,
        cooling_rate_k_per_hour=0.0,
        min_run_steps=0,
    )
    # Tank at setpoint: no DHW heating forced. SH demand = 4 steps × (6×3.8×0.25) = 22.8 kWh
    heat_needed = 4 * 6.0 * 3.8 * 0.25  # 22.8 kWh
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=55.0, heat_needed_kwh=heat_needed))

    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    thermal_per_step = 6.0 * 3.8 * 0.25
    total_sh = sum(
        round(ctx.solver.var_value(device._sh_mode[t])) * thermal_per_step
        for t in ctx.T
    )
    assert total_sh >= heat_needed - 1e-3, (
        f"SH thermal output {total_sh:.4f} kWh < heat_needed {heat_needed:.4f} kWh"
    )


# ---------------------------------------------------------------------------
# Both modes needed in same horizon
# ---------------------------------------------------------------------------


def test_combi_dhw_and_sh_both_needed_within_horizon() -> None:
    """Both DHW and SH constraints are satisfied with no mode overlap.

    DHW: start at min_temp_c + 0.5 = 40.5 °C. Tank cools at 4 K/h (1 K/step).
    Without DHW heating, the tank falls below min_temp_c in 1 step. The solver
    is forced to allocate DHW steps to maintain the temperature lower bound.
    SH: heat_needed_kwh requires at least 2 SH steps.

    After solving, assert:
      - All T_tank[t] >= min_temp_c.
      - Total SH thermal >= heat_needed_kwh.
      - No step has both modes active.
    """
    cfg = _config(
        elec_power_kw=6.0,
        cop_dhw=2.8,
        cop_sh=3.8,
        volume_liters=300.0,   # larger tank so one DHW step doesn't overshoot setpoint
        setpoint_c=55.0,
        min_temp_c=40.0,
        cooling_rate_k_per_hour=4.0,  # 1 K/step: tank drops below min_temp without DHW
        min_run_steps=0,
    )
    # 2 SH steps of output: 2 × 6×3.8×0.25 = 11.4 kWh
    heat_needed = 2 * 6.0 * 3.8 * 0.25
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(
        ctx, inputs=_inputs(current_temp_c=40.5, heat_needed_kwh=heat_needed)
    )

    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    # Mutual exclusion.
    for t in ctx.T:
        dhw = round(ctx.solver.var_value(device._dhw_mode[t]))
        sh = round(ctx.solver.var_value(device._sh_mode[t]))
        assert dhw + sh <= 1, f"Step {t}: mode overlap — dhw={dhw}, sh={sh}"

    # Tank temperature stays above min_temp_c.
    for t in ctx.T:
        temp = ctx.solver.var_value(device._T_tank[t])
        assert temp >= cfg.min_temp_c - 1e-3, (
            f"Step {t}: T_tank={temp:.4f} < min_temp={cfg.min_temp_c}"
        )

    # SH demand satisfied.
    thermal_per_step = 6.0 * 3.8 * 0.25
    total_sh = sum(
        round(ctx.solver.var_value(device._sh_mode[t])) * thermal_per_step
        for t in ctx.T
    )
    assert total_sh >= heat_needed - 1e-3, (
        f"SH output {total_sh:.4f} kWh < demand {heat_needed:.4f} kWh"
    )


# ---------------------------------------------------------------------------
# Minimum run length
# ---------------------------------------------------------------------------


def test_combi_min_run_steps_respected() -> None:
    """HP runs in consecutive blocks of at least min_run_steps once started.

    Small heat_needed_kwh (achievable in 1 step) with min_run_steps=4. The
    solver must run a block of at least 4 steps if it runs at all.
    """
    cfg = _config(
        cooling_rate_k_per_hour=0.0,  # ignore DHW cooling so tank is stable
        setpoint_c=55.0,
        min_temp_c=40.0,
        min_run_steps=4,
    )
    # Start at setpoint — no DHW heating needed. Small SH demand.
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=55.0, heat_needed_kwh=2.0))

    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    on = [round(ctx.solver.var_value(device._hp_on[t])) for t in ctx.T]
    total = sum(on)
    if total > 0:
        run_lengths = []
        i, n = 0, len(on)
        while i < n:
            if on[i] == 1:
                j = i
                while j < n and on[j] == 1:
                    j += 1
                run_lengths.append(j - i)
                i = j
            else:
                i += 1
        for length in run_lengths:
            assert length >= 4, (
                f"Run of length {length} < min_run_steps=4. Schedule: {on}"
            )


# ---------------------------------------------------------------------------
# Net power convention
# ---------------------------------------------------------------------------


def test_combi_net_power_negative_when_running() -> None:
    """net_power(t) ≈ -elec_power_kw when HP is running in any mode.

    Force DHW mode at step 0 via equality constraint, verify net_power = -elec.
    setpoint_c is set high so that the forced DHW step does not violate the
    upper temperature bound.
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config(
        cooling_rate_k_per_hour=0.0,
        setpoint_c=90.0,   # high enough for one DHW step from 50 °C
        min_temp_c=40.0,
        min_run_steps=0,
    )
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=50.0, heat_needed_kwh=0.0))

    ctx.solver.add_constraint(device._dhw_mode[0] == 1)
    ctx.solver.set_objective_minimize(device._dhw_mode[0])
    ctx.solver.solve()

    net = device.net_power(0)
    val = ctx.solver.var_value(net) if not isinstance(net, (int, float)) else float(net)
    assert abs(val - (-cfg.elec_power_kw)) < 1e-4, (
        f"Expected net_power={-cfg.elec_power_kw} kW, got {val:.4f} kW"
    )


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------


def test_combi_terminal_value_prevents_tank_drain() -> None:
    """The terminal value term prevents the solver from draining the DHW tank.

    Without a terminal value, a cost-minimising solver drains the DHW tank to
    min_temp_c at the last step. With the terminal value the solver keeps the
    tank above min_temp_c by a meaningful margin.

    Setup: T=8, uniform prices, heat_needed_kwh=0 (no SH), current_temp_c=50.
    cooling_rate=4 K/h (1 K/step). Without any heating the tank drifts to 42 °C
    (still above min_temp_c=40, so no forced DHW). Terminal value is accessed via
    device.terminal_soc_var(ctx).

    volume_liters=1500 is chosen so that one DHW step raises the tank by ~2.4 K
    (small enough to stay within setpoint=55, large enough to exceed the 1 °C
    assertion threshold). The terminal credit (~1.5 kWh_electric per step) exceeds
    the run cost (1 step), so the solver heats when the terminal value is active.

    Add the terminal value term (as a negative) to the objective to credit
    stored thermal energy at end of horizon.
    """
    cfg = _config(
        volume_liters=1500.0,   # large tank: ~2.4 K rise per step, fits within setpoint
        cooling_rate_k_per_hour=4.0,
        setpoint_c=55.0,
        min_temp_c=40.0,
        min_run_steps=0,
    )
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=50.0, heat_needed_kwh=0.0))

    # Without terminal value: minimise electrical consumption only.
    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    tank_final_no_tv = ctx.solver.var_value(device._T_tank[ctx.T[-1]])

    # With terminal value: also credit remaining stored energy.
    ctx2 = _make_ctx(horizon=8)
    device2 = CombiHeatPumpDevice(name="chp", config=cfg)
    device2.add_variables(ctx2)
    device2.add_constraints(ctx2, inputs=_inputs(current_temp_c=50.0, heat_needed_kwh=0.0))

    total_on2 = sum(device2._hp_on[t] for t in ctx2.T)
    terminal = device2.terminal_soc_var(ctx2)
    ctx2.solver.set_objective_minimize(total_on2 - terminal)
    ctx2.solver.solve()

    tank_final_with_tv = ctx2.solver.var_value(device2._T_tank[ctx2.T[-1]])

    assert tank_final_with_tv > tank_final_no_tv + 1.0, (
        f"Terminal value did not raise end-of-horizon tank temp: "
        f"without={tank_final_no_tv:.2f} °C, with={tank_final_with_tv:.2f} °C"
    )


# ---------------------------------------------------------------------------
# COP preference
# ---------------------------------------------------------------------------


def test_combi_cop_difference_affects_mode_preference() -> None:
    """When cop_sh > cop_dhw, SH mode is preferred at cheap price steps.

    Setup: T=2, step 0 cheap (0.01 EUR/kWh), step 1 expensive (1.0 EUR/kWh).
    Both DHW (tank must heat a bit) and SH are needed. cop_sh=3.8 > cop_dhw=2.8.
    At the cheap step, the solver gets more thermal value from SH mode.

    Force: both a DHW and an SH step are required (so both modes appear in
    the schedule). Assert SH mode is used at the cheap step.
    """
    cfg = _config(
        elec_power_kw=6.0,
        cop_dhw=2.8,
        cop_sh=3.8,
        volume_liters=200.0,
        setpoint_c=55.0,
        min_temp_c=40.0,
        cooling_rate_k_per_hour=0.0,
        min_run_steps=0,
    )
    # DHW: start at 40.0 — forces exactly one DHW step (must heat to min_temp_c).
    # SH: demand equals exactly 1 step of SH output.
    thermal_cap = 200.0 * _WATER_CAP
    dhw_rise_per_step = 6.0 * 2.8 * 0.25 / thermal_cap  # K per DHW step
    # start just below min_temp_c + rise: forces at least 1 DHW step
    start_temp = cfg.min_temp_c + dhw_rise_per_step * 0.5  # ~40.something °C
    sh_heat_needed = 6.0 * 3.8 * 0.25  # exactly 1 SH step

    ctx = _make_ctx(horizon=2)
    device = CombiHeatPumpDevice(name="chp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(
        ctx, inputs=_inputs(current_temp_c=start_temp, heat_needed_kwh=sh_heat_needed)
    )

    prices = [0.01, 1.0]
    cost = sum(
        prices[t] * cfg.elec_power_kw * device._hp_on[t] * 0.25
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    sh_at_cheap = round(ctx.solver.var_value(device._sh_mode[0]))
    assert sh_at_cheap == 1, (
        f"Expected SH mode at cheap step 0 (better COP), got sh_mode[0]={sh_at_cheap}. "
        f"dhw_mode[0]={round(ctx.solver.var_value(device._dhw_mode[0]))}"
    )
