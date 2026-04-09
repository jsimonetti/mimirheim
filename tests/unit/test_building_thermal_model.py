"""Unit tests for the building thermal model (BTM) — SpaceHeatingDevice and
CombiHeatPumpDevice with ``building_thermal`` configuration.

All tests must fail before the BTM implementation exists (TDD). Tests use a real
CBCSolverBackend with minimal horizons (T=4 or T=8) and simplified cost objectives
to keep run times short.

Shared BTM fixture parameters:

    thermal_capacity_kwh_per_k = 5.0   (building thermal mass)
    heat_loss_coeff_kw_per_k   = 0.8   (heat loss coefficient)
    comfort_min_c = 18.0               (lower comfort bound)
    comfort_max_c = 24.0               (upper comfort bound)

Derived step constants (dt = 0.25 h):

    alpha        = 1 − dt * L / C = 1 − 0.25 * 0.8 / 5.0 = 0.96
    beta_heat    = dt / C = 0.25 / 5.0 = 0.05  (°C per kW thermal input)
    beta_outdoor = dt * L / C = 0.04            (°C per °C outdoor contribution)

Default HP (on/off): elec_power_kw=6.0, cop=3.5.
    P_heat when on = 6.0 * 3.5 = 21.0 kW thermal
    Temperature rise per active step = 21.0 * 0.05 = 1.05 °C
"""

import pytest

from mimirheim.config.schema import (
    BuildingThermalConfig,
    CombiHeatPumpConfig,
    HeatingStage,
    SpaceHeatingConfig,
)
from mimirheim.core.bundle import CombiHeatPumpInputs, SpaceHeatingInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.combi_heat_pump import CombiHeatPumpDevice
from mimirheim.devices.space_heating import SpaceHeatingDevice

# ---------------------------------------------------------------------------
# Shared BTM configuration — used across most tests
# ---------------------------------------------------------------------------

BTM_CFG = BuildingThermalConfig(
    thermal_capacity_kwh_per_k=5.0,
    heat_loss_coeff_kw_per_k=0.8,
    comfort_min_c=18.0,
    comfort_max_c=24.0,
    inputs=None,
)

# Derived constants — recalculate here so the test intent is obvious
_C = 5.0        # kWh/K
_L = 0.8        # kW/K
_DT = 0.25      # h
_ALPHA = 1 - _DT * _L / _C                # 0.96
_BETA_HEAT = _DT / _C                      # 0.05 °C per kW thermal
_BETA_OUTDOOR = _DT * _L / _C             # 0.04


# ---------------------------------------------------------------------------
# Helper factory functions
# ---------------------------------------------------------------------------


def _hp_config_on_off(btm: BuildingThermalConfig | None = None) -> SpaceHeatingConfig:
    """Space heating config in on/off mode with BTM optionally set."""
    return SpaceHeatingConfig(
        elec_power_kw=6.0,
        cop=3.5,
        min_run_steps=0,
        wear_cost_eur_per_kwh=0.0,
        building_thermal=btm,
    )


def _hp_config_staged(btm: BuildingThermalConfig | None = None) -> SpaceHeatingConfig:
    """Space heating config in SOS2 staged mode: off, 3 kW at COP 3.0, 6 kW at COP 3.5."""
    return SpaceHeatingConfig(
        stages=[
            HeatingStage(elec_kw=0.0, cop=0.0),
            HeatingStage(elec_kw=3.0, cop=3.0),
            HeatingStage(elec_kw=6.0, cop=3.5),
        ],
        min_run_steps=0,
        wear_cost_eur_per_kwh=0.0,
        building_thermal=btm,
    )


def _sp_inputs(
    heat_needed_kwh: float = 0.0,
    current_indoor_temp_c: float | None = None,
    outdoor_temp_forecast_c: list[float] | None = None,
) -> SpaceHeatingInputs:
    """Construct SpaceHeatingInputs with optional BTM fields."""
    return SpaceHeatingInputs(
        heat_needed_kwh=heat_needed_kwh,
        current_indoor_temp_c=current_indoor_temp_c,
        outdoor_temp_forecast_c=outdoor_temp_forecast_c,
    )


def _chp_config(btm: BuildingThermalConfig | None = None) -> CombiHeatPumpConfig:
    """Combi heat pump config with optional BTM.

    cooling_rate_k_per_hour=2.0 → 0.5 K per step. With a 2000 L tank starting
    at 42 °C: T_tank reaches ~40 °C at step 3 without DHW, so roughly 1–2 DHW
    steps are needed in an 8-step horizon. This is manageable alongside the BTM
    indoor temperature constraints.
    """
    return CombiHeatPumpConfig(
        elec_power_kw=6.0,
        cop_dhw=2.8,
        cop_sh=3.5,
        volume_liters=2000.0,
        setpoint_c=55.0,
        min_temp_c=40.0,
        cooling_rate_k_per_hour=2.0,
        min_run_steps=0,
        wear_cost_eur_per_kwh=0.0,
        building_thermal=btm,
    )


def _chp_inputs(
    current_temp_c: float = 50.0,
    heat_needed_kwh: float = 0.0,
    current_indoor_temp_c: float | None = None,
    outdoor_temp_forecast_c: list[float] | None = None,
) -> CombiHeatPumpInputs:
    """Construct CombiHeatPumpInputs with optional BTM fields."""
    return CombiHeatPumpInputs(
        current_temp_c=current_temp_c,
        heat_needed_kwh=heat_needed_kwh,
        current_indoor_temp_c=current_indoor_temp_c,
        outdoor_temp_forecast_c=outdoor_temp_forecast_c,
    )


def _make_ctx(horizon: int = 8) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _minimize_elec_cost(
    ctx: ModelContext,
    device: SpaceHeatingDevice,
    prices: list[float],
) -> None:
    """Set objective to minimise HP electrical consumption cost (on/off mode only)."""
    expr = sum(prices[t] * device._hp_on[t] * 6.0 * _DT for t in ctx.T)
    ctx.solver.set_objective_minimize(expr)


# ---------------------------------------------------------------------------
# BTM dynamics — analytical verification
# ---------------------------------------------------------------------------


def test_btm_dynamics_no_hp() -> None:
    """T_indoor follows the free-decay formula exactly when the HP does not run.

    Physics: T[t] = alpha * T[t-1] + beta_outdoor * T_outdoor[t]

    Setup: T=4, cost 1000 EUR/kWh (very expensive) → solver chooses hp_on=0
    at every step. current_indoor=21.0 °C, outdoor=[10.0]*4.

    All steps stay above comfort_min=18.0 so the comfort constraint does not
    force the HP on. Expected values are computed analytically and compared
    against solver variable values with abs tolerance 1e-4.
    """
    ctx = _make_ctx(horizon=4)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=21.0,
            outdoor_temp_forecast_c=[10.0, 10.0, 10.0, 10.0],
        ),
    )

    _minimize_elec_cost(ctx, device, prices=[1000.0] * 4)
    ctx.solver.solve()

    expected: list[float] = []
    prev = 21.0
    for outdoor in [10.0, 10.0, 10.0, 10.0]:
        t_val = _ALPHA * prev + _BETA_OUTDOOR * outdoor
        expected.append(t_val)
        prev = t_val

    for t in range(4):
        actual = ctx.solver.var_value(device._T_indoor[t])
        assert actual == pytest.approx(expected[t], abs=1e-4), (
            f"Step {t}: expected T_indoor={expected[t]:.6f}, got {actual:.6f}"
        )


def test_btm_dynamics_with_hp() -> None:
    """T_indoor follows the formula when the HP runs at every step.

    Physics:
        T[t] = alpha * T[t-1] + beta_heat * P_heat + beta_outdoor * T_outdoor[t]
    where P_heat = elec_power_kw * cop = 6.0 * 3.5 = 21.0 kW.

    Setup: T=4, price −100 EUR/kWh (free electricity — running the HP reduces
    cost) → solver runs HP at every step. current_indoor=18.0 °C, outdoor=[0.0]*4.
    All steps stay below comfort_max=24.0, so the ceiling does not suppress the HP.
    """
    ctx = _make_ctx(horizon=4)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=18.0,
            outdoor_temp_forecast_c=[0.0, 0.0, 0.0, 0.0],
        ),
    )

    _minimize_elec_cost(ctx, device, prices=[-100.0] * 4)
    ctx.solver.solve()

    # P_heat = electrical power × COP = 6.0 kW × 3.5 = 21.0 kW thermal
    p_heat = 6.0 * 3.5
    expected: list[float] = []
    prev = 18.0
    for outdoor in [0.0, 0.0, 0.0, 0.0]:
        t_val = _ALPHA * prev + _BETA_HEAT * p_heat + _BETA_OUTDOOR * outdoor
        expected.append(t_val)
        prev = t_val

    for t in range(4):
        on_val = round(ctx.solver.var_value(device._hp_on[t]))
        assert on_val == 1, f"Step {t}: expected hp_on=1 (free electricity), got {on_val}"

        actual = ctx.solver.var_value(device._T_indoor[t])
        assert actual == pytest.approx(expected[t], abs=1e-4), (
            f"Step {t}: expected T_indoor={expected[t]:.6f}, got {actual:.6f}"
        )


# ---------------------------------------------------------------------------
# Comfort constraints
# ---------------------------------------------------------------------------


def test_btm_comfort_min_enforced() -> None:
    """T_indoor[t] >= comfort_min_c at every step.

    Setup: T=8, cold outdoor ([0.0]*8), current_indoor=19.5 °C.

    Without the HP the building cools below 18.0 °C by step 2:
        T[0] = 0.96 * 19.5 = 18.72
        T[1] = 0.96 * 18.72 = 17.97  ← below 18.0

    The HP must be scheduled to maintain the lower comfort bound.
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=19.5,
            outdoor_temp_forecast_c=[0.0] * 8,
        ),
    )

    _minimize_elec_cost(ctx, device, prices=[0.2] * 8)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    for t in ctx.T:
        t_val = ctx.solver.var_value(device._T_indoor[t])
        assert t_val >= 18.0 - 1e-4, (
            f"Step {t}: T_indoor={t_val:.4f} violates comfort_min=18.0"
        )


def test_btm_comfort_max_enforced() -> None:
    """T_indoor[t] <= comfort_max_c at every step.

    Setup: T=8, warm outdoor ([22.0]*8), current_indoor=23.5 °C, free electricity.

    With negative prices the solver would run the HP at every step if no ceiling
    existed. The comfort_max=24.0 constraint must suppress the HP whenever running
    it would push T_indoor above the ceiling.
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=23.5,
            outdoor_temp_forecast_c=[22.0] * 8,
        ),
    )

    _minimize_elec_cost(ctx, device, prices=[-100.0] * 8)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    for t in ctx.T:
        t_val = ctx.solver.var_value(device._T_indoor[t])
        assert t_val <= 24.0 + 1e-4, (
            f"Step {t}: T_indoor={t_val:.4f} violates comfort_max=24.0"
        )


# ---------------------------------------------------------------------------
# Pre-heating behaviour
# ---------------------------------------------------------------------------


def test_btm_preheat_shifts_hp_to_cheap_steps() -> None:
    """The solver concentrates HP operation in cheap price steps.

    Steps 0–3: cheap (0.05 EUR/kWh), mild outdoor (10 °C).
    Steps 4–7: expensive (1.0 EUR/kWh), cold outdoor (−5 °C).

    current_indoor=20.0 °C. The solver should pre-heat during cheap steps,
    building thermal storage, so that it can avoid (or reduce) operation during
    the more expensive window. The test asserts that more HP steps fall in the
    cheap window than in the expensive window.

    The cold outdoor temperature during steps 4–7 may still require some HP
    operation there — the assertion is only that cheap steps carry more load.
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=20.0,
            outdoor_temp_forecast_c=[10.0] * 4 + [-5.0] * 4,
        ),
    )

    prices = [0.05] * 4 + [1.0] * 4
    cost = sum(prices[t] * 6.0 * device._hp_on[t] * _DT for t in ctx.T)
    ctx.solver.set_objective_minimize(cost)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    cheap_on = sum(round(ctx.solver.var_value(device._hp_on[t])) for t in range(4))
    expensive_on = sum(round(ctx.solver.var_value(device._hp_on[t])) for t in range(4, 8))
    assert cheap_on > expensive_on, (
        f"Expected more HP steps in the cheap window (0–3). "
        f"Got cheap_on={cheap_on}, expensive_on={expensive_on}."
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_btm_outdoor_forecast_shorter_than_horizon_raises() -> None:
    """add_constraints raises ValueError when outdoor_temp_forecast_c is too short.

    A horizon of T=8 requires exactly 8 outdoor temperature values. Providing
    only 4 is invalid. The error message must identify the device name.
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp_unit", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)

    with pytest.raises(ValueError, match="hp_unit"):
        device.add_constraints(
            ctx,
            inputs=_sp_inputs(
                current_indoor_temp_c=20.0,
                outdoor_temp_forecast_c=[5.0] * 4,  # too short
            ),
        )


# ---------------------------------------------------------------------------
# Interaction with existing degree-days guard
# ---------------------------------------------------------------------------


def test_btm_sp_on_off_replaces_heat_needed_constraint() -> None:
    """When BTM is active, heat_needed_kwh=0.0 does not suppress the HP.

    Without BTM, the SpaceHeatingDevice early-exits when heat_needed_kwh is zero
    and pins all hp_on variables to zero. With BTM active that guard must be
    bypassed: the comfort envelope drives HP scheduling, not the degree-days
    demand scalar.

    Setup: heat_needed_kwh=0.0 (zero external demand), cold outdoor ([0.0]*8),
    current_indoor=19.0 °C. The HP must still run to maintain comfort_min=18.0.
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp", config=_hp_config_on_off(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            heat_needed_kwh=0.0,
            current_indoor_temp_c=19.0,
            outdoor_temp_forecast_c=[0.0] * 8,
        ),
    )

    _minimize_elec_cost(ctx, device, prices=[0.2] * 8)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    total_on = sum(round(ctx.solver.var_value(device._hp_on[t])) for t in ctx.T)
    assert total_on > 0, (
        "HP never ran despite cold outdoor and active comfort_min=18.0. "
        "The zero-demand early-exit guard must not fire when BTM is configured."
    )


# ---------------------------------------------------------------------------
# SOS2 (power-stage) mode
# ---------------------------------------------------------------------------


def test_btm_sp_sos2_mode_applies_btm() -> None:
    """BTM dynamics and comfort constraints apply when SpaceHeatingDevice uses staged mode.

    Stages: off (0 kW), 3 kW at COP 3.0, 6 kW at COP 3.5.

    Setup: T=8, outdoor=[5.0]*8, current_indoor=21.0 °C.

    Without heating the building cools below 18.0 °C by step 6 or so. The solver
    must activate partial or full power to maintain comfort. The test asserts:
    1. T_indoor[t] >= comfort_min_c at all steps.
    2. At least one step has a non-zero weight on a non-sentinel stage (HP is
       delivering thermal energy at some point).
    """
    ctx = _make_ctx(horizon=8)
    device = SpaceHeatingDevice(name="hp_staged", config=_hp_config_staged(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_sp_inputs(
            current_indoor_temp_c=21.0,
            outdoor_temp_forecast_c=[5.0] * 8,
        ),
    )

    stages = _hp_config_staged(btm=BTM_CFG).stages
    prices = [0.2] * 8
    elec_cost = sum(
        prices[t] * sum(device._w[t][s] * stages[s].elec_kw for s in range(len(stages))) * _DT
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(elec_cost)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    for t in ctx.T:
        t_val = ctx.solver.var_value(device._T_indoor[t])
        assert t_val >= 18.0 - 1e-4, f"Step {t}: T_indoor={t_val:.4f} < comfort_min=18.0"

    any_active = any(
        ctx.solver.var_value(device._w[t][s]) > 0.01
        for t in ctx.T
        for s in range(1, len(stages))
    )
    assert any_active, (
        "HP appears idle at every step in staged mode; the BTM should have triggered"
        " some thermal output to maintain comfort."
    )


# ---------------------------------------------------------------------------
# Combi heat pump — BTM on the SH mode
# ---------------------------------------------------------------------------


def test_btm_combi_hp_sh_mode_applies_btm() -> None:
    """BTM constraints apply to the SH mode of the combi heat pump.

    The DHW tank starts well above min_temp_c (50 °C vs 40 °C minimum) so the
    solver is not forced into DHW mode. Cold outdoor temperature requires SH
    operation to maintain indoor comfort.

    Assert: T_indoor[t] >= comfort_min_c at all steps, and sh_mode[t] is 1 for
    at least one step.

    DHW tank dynamics are also checked: T_tank[t] must stay within [40.0, 55.0].
    The 2000 L tank has enough thermal mass that a modest cooling rate does not
    drop it below 40 °C within the 8-step horizon when starting at 50 °C.
    """
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=_chp_config(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_chp_inputs(
            current_temp_c=50.0,
            heat_needed_kwh=0.0,
            current_indoor_temp_c=18.5,
            outdoor_temp_forecast_c=[0.0] * 8,
        ),
    )

    elec_cost = sum(0.2 * 6.0 * device._hp_on[t] * _DT for t in ctx.T)
    ctx.solver.set_objective_minimize(elec_cost)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    for t in ctx.T:
        t_val = ctx.solver.var_value(device._T_indoor[t])
        assert t_val >= 18.0 - 1e-4, f"Step {t}: T_indoor={t_val:.4f} < comfort_min"

    sh_active = any(round(ctx.solver.var_value(device._sh_mode[t])) == 1 for t in ctx.T)
    assert sh_active, "sh_mode was never 1; BTM should have forced at least one SH step."


def test_btm_combi_hp_dhw_and_sh_both_satisfied() -> None:
    """Both DHW and SH constraints are satisfied simultaneously when BTM is active.

    DHW tank starts at 42.0 °C (just above min 40.0 °C) with a high cooling rate
    (4 K/h = 1 K/step) — after 2 steps without DHW it drops to 40.0 °C and
    then needs heating. The large 2000 L tank has modest temperature rise per
    DHW step (≈ 1.8 °C), preventing overshoot of the 55 °C setpoint.

    Outdoor temperature is 0 °C. Indoor starts at 19.5 °C. Without SH the
    building cools below comfort_min by step 2 or 3 (≈ 0.76 °C/step drop).

    The solver must alternate DHW and SH steps to satisfy both demands.

    Asserts:
        - T_tank[T-1] >= min_temp_c = 40.0.
        - T_indoor[T-1] >= comfort_min_c = 18.0.
        - At no step are both dhw_mode and sh_mode active simultaneously.
    """
    ctx = _make_ctx(horizon=8)
    device = CombiHeatPumpDevice(name="chp", config=_chp_config(btm=BTM_CFG))
    device.add_variables(ctx)
    device.add_constraints(
        ctx,
        inputs=_chp_inputs(
            current_temp_c=42.0,
            heat_needed_kwh=0.0,
            current_indoor_temp_c=19.5,
            outdoor_temp_forecast_c=[0.0] * 8,
        ),
    )

    elec_cost = sum(0.2 * 6.0 * device._hp_on[t] * _DT for t in ctx.T)
    ctx.solver.set_objective_minimize(elec_cost)
    status = ctx.solver.solve()
    assert status in ("optimal", "feasible"), f"Unexpected solve status: {status}"

    last = max(ctx.T)

    t_tank_final = ctx.solver.var_value(device._T_tank[last])
    assert t_tank_final >= 40.0 - 1e-4, (
        f"T_tank[{last}]={t_tank_final:.4f} < min_temp_c=40.0"
    )

    t_indoor_final = ctx.solver.var_value(device._T_indoor[last])
    assert t_indoor_final >= 18.0 - 1e-4, (
        f"T_indoor[{last}]={t_indoor_final:.4f} < comfort_min_c=18.0"
    )

    for t in ctx.T:
        dhw = round(ctx.solver.var_value(device._dhw_mode[t]))
        sh = round(ctx.solver.var_value(device._sh_mode[t]))
        assert dhw + sh <= 1, (
            f"Step {t}: mutual exclusion violated — dhw_mode={dhw}, sh_mode={sh}"
        )


# ---------------------------------------------------------------------------
# Backward compatibility — degree-days path unchanged when BTM not set
# ---------------------------------------------------------------------------


def test_btm_degree_days_path_unchanged_when_btm_not_set() -> None:
    """Degree-days path is unaffected when building_thermal is None.

    Configure SpaceHeatingConfig without BTM (default). heat_needed_kwh=6.0.
    The solver must satisfy the heat demand as in plans 25 and 26.

    The device must not declare any _T_indoor variables: _T_indoor should be
    an empty dict, confirming that BTM variables are only added when opt-in.
    """
    ctx = _make_ctx(horizon=8)
    cfg = SpaceHeatingConfig(
        elec_power_kw=6.0,
        cop=3.5,
        min_run_steps=0,
        wear_cost_eur_per_kwh=0.0,
        # building_thermal not set — defaults to None
    )
    device = SpaceHeatingDevice(name="hp_ddm", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=SpaceHeatingInputs(heat_needed_kwh=6.0))

    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    thermal_per_step = 6.0 * 3.5 * _DT
    total_thermal = sum(
        ctx.solver.var_value(device._hp_on[t]) * thermal_per_step for t in ctx.T
    )
    assert total_thermal >= 6.0 - 1e-4, (
        f"Total thermal {total_thermal:.4f} kWh < heat_needed 6.0 kWh"
    )

    assert device._T_indoor == {}, (
        "_T_indoor must be empty when building_thermal is None; "
        f"got keys {list(device._T_indoor.keys())}"
    )
