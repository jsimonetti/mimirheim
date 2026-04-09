"""Unit tests for mimirheim/devices/thermal_boiler.py — ThermalBoilerDevice constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=4, dt=0.25 unless noted otherwise.

The default configuration is: volume_liters=200, elec_power_kw=3.0,
cooling_rate_k_per_hour=2.0, setpoint_c=55.0, min_temp_c=40.0.
The default initial temperature is 45.0°C.
"""

import math

from mimirheim.config.schema import ThermalBoilerConfig
from mimirheim.core.bundle import ThermalBoilerInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.thermal_boiler import ThermalBoilerDevice, _WATER_THERMAL_CAP_KWH_PER_LITRE_K


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    volume_liters: float = 200.0,
    elec_power_kw: float = 3.0,
    cop: float = 1.0,
    setpoint_c: float = 55.0,
    min_temp_c: float = 40.0,
    cooling_rate_k_per_hour: float = 2.0,
    min_run_steps: int = 0,
    wear_cost: float = 0.0,
) -> ThermalBoilerConfig:
    return ThermalBoilerConfig(
        volume_liters=volume_liters,
        elec_power_kw=elec_power_kw,
        cop=cop,
        setpoint_c=setpoint_c,
        min_temp_c=min_temp_c,
        cooling_rate_k_per_hour=cooling_rate_k_per_hour,
        min_run_steps=min_run_steps,
        wear_cost_eur_per_kwh=wear_cost,
    )


def _inputs(current_temp_c: float = 45.0) -> ThermalBoilerInputs:
    return ThermalBoilerInputs(current_temp_c=current_temp_c)


def _make_ctx(horizon: int = 4) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _thermal_cap(volume_liters: float) -> float:
    """Return tank thermal capacity in kWh/K for the given volume."""
    return volume_liters * _WATER_THERMAL_CAP_KWH_PER_LITRE_K


# ---------------------------------------------------------------------------
# Temperature dynamics
# ---------------------------------------------------------------------------


def test_boiler_temp_rises_when_heating() -> None:
    """Tank temperature increases by heat_rise_per_step and decreases by cool_per_step.

    With all heater_on[t]=1 forced, the temperature at step t equals:
        current_temp_c + (t+1) * (heat_rise - cool_per_step)

    Uses cop=1.0. Sets setpoint_c=80.0 to prevent the temperature rising above
    the setpoint during the test (which would make the model infeasible).
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(cop=1.0, setpoint_c=80.0)  # high setpoint avoids infeasibility
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0))

    # Force heater on at every step.
    for t in ctx.T:
        ctx.solver.add_constraint(device._heater_on[t] == 1)

    ctx.solver.set_objective_minimize(device._T_tank[0])
    ctx.solver.solve()

    thermal_cap = _thermal_cap(cfg.volume_liters)
    heat_rise = cfg.elec_power_kw * cfg.cop * ctx.dt / thermal_cap
    cool = cfg.cooling_rate_k_per_hour * ctx.dt
    net_rise = heat_rise - cool

    for t in ctx.T:
        expected = 45.0 + (t + 1) * net_rise
        actual = ctx.solver.var_value(device._T_tank[t])
        assert abs(actual - expected) < 1e-4, (
            f"Step {t}: expected T={expected:.4f}°C, got {actual:.4f}°C"
        )


def test_boiler_temp_drops_when_not_heating() -> None:
    """When the heater is forced off, temperature decreases by cool_per_step each step."""
    ctx = _make_ctx(horizon=4)
    cfg = _config()
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=50.0))

    # Force heater off at every step.
    for t in ctx.T:
        ctx.solver.add_constraint(device._heater_on[t] == 0)

    ctx.solver.set_objective_minimize(device._T_tank[0])
    ctx.solver.solve()

    cool = cfg.cooling_rate_k_per_hour * ctx.dt
    for t in ctx.T:
        expected = 50.0 - (t + 1) * cool
        actual = ctx.solver.var_value(device._T_tank[t])
        assert abs(actual - expected) < 1e-4, (
            f"Step {t}: expected T={expected:.4f}°C, got {actual:.4f}°C"
        )


def test_boiler_temp_never_below_min() -> None:
    """Temperature must stay at or above min_temp_c at every step.

    Start temperature just above min_temp_c. With high electricity cost the
    solver prefers not to heat, but the solver must heat enough to stay above
    min_temp_c.
    """
    cool_per_step = 2.0 * 0.25  # 0.5 K per step
    start_temp = 40.0 + cool_per_step * 4 + 0.01  # just above min after 4 cooling steps

    ctx = _make_ctx(horizon=4)
    cfg = _config()
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=start_temp))

    # Penalise heating heavily; solver should heat only when forced.
    penalty = sum(1000.0 * device._heater_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(penalty)
    ctx.solver.solve()

    for t in ctx.T:
        actual = ctx.solver.var_value(device._T_tank[t])
        assert actual >= 40.0 - 1e-4, (
            f"Step {t}: T={actual:.4f}°C is below min_temp_c=40.0°C"
        )


def test_boiler_temp_never_above_setpoint() -> None:
    """Temperature must not exceed setpoint_c at any step.

    With very cheap electricity (strong incentive to heat), the solver should
    still respect the setpoint upper bound.
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config()
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0))

    # Strong incentive to heat as much as possible.
    ctx.solver.set_objective_minimize(
        sum(-device._T_tank[t] for t in ctx.T)
    )
    ctx.solver.solve()

    for t in ctx.T:
        actual = ctx.solver.var_value(device._T_tank[t])
        assert actual <= cfg.setpoint_c + 1e-4, (
            f"Step {t}: T={actual:.4f}°C exceeds setpoint_c={cfg.setpoint_c}°C"
        )


def test_boiler_schedules_at_cheap_step() -> None:
    """Solver heats at the low-price step, not the high-price step.

    T=2 horizon:
    - Step 0: expensive (1.0 EUR/kWh import).
    - Step 1: cheap (0.01 EUR/kWh import).

    start_temp is set so that:
    - Without heating, T drops below min_temp_c after step 1 (must heat once).
    - With heating at only step 0, T[0] = start - cool + heat_rise > 40°C (feasible).
    - With heating at only step 1, T[0] = start - cool >= 40°C and
      T[1] = T[0] - cool + heat_rise (feasible).

    Expected: heater_on[0]=0, heater_on[1]=1 (cheap step selected).
    """
    cool_per_step = 2.0 * 0.25  # 0.5 K per step

    # start_temp such that:
    # - T[0] without heating = start - cool >= 40.0 (no heating needed at step 0)
    # - T[1] without heating = start - 2*cool < 40.0 (heating needed at step 1)
    # Choose start = 40.0 + cool_per_step + 0.01 = 40.51°C
    start_temp = 40.0 + cool_per_step + 0.01

    ctx = _make_ctx(horizon=2)
    cfg = _config()
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=start_temp))

    # Objective: minimize import cost (elec_power * heater_on * price * dt).
    cost = (
        1.0 * cfg.elec_power_kw * device._heater_on[0] * ctx.dt
        + 0.01 * cfg.elec_power_kw * device._heater_on[1] * ctx.dt
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    h0 = round(ctx.solver.var_value(device._heater_on[0]))
    h1 = round(ctx.solver.var_value(device._heater_on[1]))

    assert h0 == 0, f"Expected heater_on[0]=0 (expensive step), got {h0}"
    assert h1 == 1, f"Expected heater_on[1]=1 (cheap step), got {h1}"


def test_boiler_cop_amplifies_thermal_rise() -> None:
    """With cop=3.0, the thermal rise per active step is 3x that of cop=1.0.

    Uses cooling_rate_k_per_hour=0.0 to isolate the COP effect: with no
    cooling the net temperature rise equals exactly the COP-scaled heat input,
    so the 3x ratio holds exactly. With non-zero cooling the net rise would be
    (heat_rise - cool), and the ratio would not be exactly 3 because the
    cooling term is the same for both cases.

    Both devices share the same electrical power and volume. Forcing heater_on=1
    for step 0 and measuring the resulting temperature change checks that COP
    scales the heat input correctly.
    """
    ctx_1 = _make_ctx(horizon=1)
    cfg_1 = _config(cop=1.0, cooling_rate_k_per_hour=0.0)
    dev_1 = ThermalBoilerDevice(name="boiler", config=cfg_1)
    dev_1.add_variables(ctx_1)
    dev_1.add_constraints(ctx_1, inputs=_inputs(current_temp_c=45.0))
    ctx_1.solver.add_constraint(dev_1._heater_on[0] == 1)
    ctx_1.solver.set_objective_minimize(dev_1._T_tank[0])
    ctx_1.solver.solve()
    rise_1 = ctx_1.solver.var_value(dev_1._T_tank[0]) - 45.0

    ctx_3 = _make_ctx(horizon=1)
    cfg_3 = _config(cop=3.0, cooling_rate_k_per_hour=0.0)
    dev_3 = ThermalBoilerDevice(name="boiler", config=cfg_3)
    dev_3.add_variables(ctx_3)
    dev_3.add_constraints(ctx_3, inputs=_inputs(current_temp_c=45.0))
    ctx_3.solver.add_constraint(dev_3._heater_on[0] == 1)
    ctx_3.solver.set_objective_minimize(dev_3._T_tank[0])
    ctx_3.solver.solve()
    rise_3 = ctx_3.solver.var_value(dev_3._T_tank[0]) - 45.0

    assert abs(rise_3 - 3.0 * rise_1) < 1e-4, (
        f"COP 3.0 rise ({rise_3:.4f}K) should equal 3 × COP 1.0 rise ({rise_1:.4f}K)"
    )


def test_boiler_net_power_negative_when_on() -> None:
    """net_power(t) ≈ -elec_power_kw when heater_on[t]=1, 0 otherwise.

    Forces heater_on[t]=1 at every step and verifies the AC bus draw.
    Uses setpoint_c=80.0 to avoid infeasibility when 4 steps of heating
    are forced.
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(elec_power_kw=3.0, setpoint_c=80.0)  # high setpoint avoids infeasibility
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0))

    for t in ctx.T:
        ctx.solver.add_constraint(device._heater_on[t] == 1)

    ctx.solver.set_objective_minimize(device._T_tank[0])
    ctx.solver.solve()

    for t in ctx.T:
        net = device.net_power(t)
        if isinstance(net, (int, float)):
            val = float(net)
        else:
            val = ctx.solver.var_value(net)
        assert abs(val - (-3.0)) < 1e-4, (
            f"Step {t}: expected net_power=-3.0 kW, got {val:.4f} kW"
        )


# ---------------------------------------------------------------------------
# Minimum run constraint
# ---------------------------------------------------------------------------


def test_boiler_min_run_steps_consecutive() -> None:
    """With min_run_steps=4, if the solver starts the heater it must run 4+ steps.

    T=4. Make step 2 very cheap, all others expensive. The solver can avoid
    starting entirely, or if it starts it MUST run for 4 consecutive steps
    (the full horizon). Verify that partial runs (1, 2, or 3 steps) never occur.
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(min_run_steps=4)
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0))

    # Make step 2 cheap, rest expensive.
    prices = [1.0, 1.0, 0.001, 1.0]
    cost = sum(
        prices[t] * cfg.elec_power_kw * device._heater_on[t] * ctx.dt
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    on = [round(ctx.solver.var_value(device._heater_on[t])) for t in ctx.T]
    total_on = sum(on)

    # Either all off, or all on (can't have 1-3 active steps when min_run_steps=4).
    assert total_on == 0 or total_on == 4, (
        f"With min_run_steps=4, expected 0 or 4 active steps, got {on}"
    )


def test_boiler_min_run_zero_allows_single_step() -> None:
    """With min_run_steps=0, the solver may heat for exactly one step.

    Same price setup as above: step 2 cheap, rest expensive. The solver
    should heat only at step 2.
    """
    ctx = _make_ctx(horizon=4)
    cfg = _config(min_run_steps=0)
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=45.0))

    prices = [1.0, 1.0, 0.001, 1.0]
    cost = sum(
        prices[t] * cfg.elec_power_kw * device._heater_on[t] * ctx.dt
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    on = [round(ctx.solver.var_value(device._heater_on[t])) for t in ctx.T]
    # Solver should target only step 2 (or not heat at all if temp allows).
    assert on[0] == 0 and on[1] == 0 and on[3] == 0, (
        f"With min_run_steps=0, expected only step 2 to be on, got {on}"
    )


# ---------------------------------------------------------------------------
# Terminal value
# ---------------------------------------------------------------------------


def test_boiler_terminal_value_prevents_unnecessary_drain() -> None:
    """The terminal value term discourages draining the tank by end of horizon.

    T=8. Uniform low price (no benefit from shifting). Start at setpoint.
    Without terminal value, solver drains tank to min_temp_c.
    With terminal value (via terminal_soc_var), solver leaves tank warm.

    We do not test the exact ObjectiveBuilder terminal coefficient here.
    Instead we verify that ``terminal_soc_var`` returns a non-None solver
    expression (i.e. the method exists and is wired correctly).
    """
    ctx = _make_ctx(horizon=8)
    cfg = _config()
    device = ThermalBoilerDevice(name="boiler", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(current_temp_c=55.0))

    # terminal_soc_var must exist and return a non-None expression.
    terminal = device.terminal_soc_var(ctx)
    assert terminal is not None, "terminal_soc_var() must return a solver expression, not None"

    # Verify it is a solver variable (not a plain float) so it can be used
    # by ObjectiveBuilder in a linear objective term.
    assert not isinstance(terminal, (int, float)), (
        "terminal_soc_var() must return a solver expression, not a plain number"
    )
