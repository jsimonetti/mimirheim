"""Unit tests for mimirheim/devices/space_heating.py — SpaceHeatingDevice constraints.

All tests must fail before the implementation exists (TDD). Tests use a real
CBCSolverBackend and ModelContext with T=8, dt=0.25 unless noted otherwise.

Default fixture: SpaceHeatingConfig with elec_power_kw=5.0, cop=3.5, min_run_steps=4.
Default inputs: SpaceHeatingInputs with heat_needed_kwh=7.0.
"""

from mimirheim.config.schema import HeatingStage, SpaceHeatingConfig
from mimirheim.core.bundle import SpaceHeatingInputs
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.space_heating import SpaceHeatingDevice


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config_on_off(
    elec_power_kw: float = 5.0,
    cop: float = 3.5,
    min_run_steps: int = 4,
    wear_cost: float = 0.0,
) -> SpaceHeatingConfig:
    return SpaceHeatingConfig(
        elec_power_kw=elec_power_kw,
        cop=cop,
        min_run_steps=min_run_steps,
        wear_cost_eur_per_kwh=wear_cost,
    )


def _config_staged(
    stages: list[HeatingStage],
    min_run_steps: int = 4,
    wear_cost: float = 0.0,
) -> SpaceHeatingConfig:
    return SpaceHeatingConfig(
        stages=stages,
        min_run_steps=min_run_steps,
        wear_cost_eur_per_kwh=wear_cost,
    )


def _stage(elec_kw: float, cop: float) -> HeatingStage:
    return HeatingStage(elec_kw=elec_kw, cop=cop)


def _inputs(heat_needed_kwh: float = 7.0) -> SpaceHeatingInputs:
    return SpaceHeatingInputs(heat_needed_kwh=heat_needed_kwh)


def _make_ctx(horizon: int = 8) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


# ---------------------------------------------------------------------------
# On/off mode — basic constraints
# ---------------------------------------------------------------------------


def test_space_heating_produces_required_heat() -> None:
    """Total thermal output must satisfy heat_needed_kwh over the horizon.

    Setup: 8-step horizon, cop=3.5, elec=5.0 kW. Each active step produces
    5.0 × 3.5 × 0.25 = 4.375 kWh thermal. heat_needed_kwh=7.0 requires at
    least 2 active steps (2 × 4.375 = 8.75 kWh ≥ 7.0 kWh).

    With min_run_steps=4, the solver runs 4 consecutive steps (17.5 kWh).
    The test simply asserts the constraint is not violated.
    """
    ctx = _make_ctx(horizon=8)
    cfg = _config_on_off()
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=7.0))

    # Minimise total electrical consumption.
    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    thermal_per_step = cfg.elec_power_kw * cfg.cop * ctx.dt
    total_thermal = sum(
        ctx.solver.var_value(device._hp_on[t]) * thermal_per_step for t in ctx.T
    )
    assert total_thermal >= 7.0 - 1e-4, (
        f"Total thermal {total_thermal:.4f} kWh < heat_needed 7.0 kWh"
    )


def test_space_heating_schedules_at_cheap_steps() -> None:
    """Solver places all active steps in the cheap price window.

    T=8:
    - Steps 0–3: expensive (1.0 EUR/kWh).
    - Steps 4–7: cheap (0.05 EUR/kWh).

    heat_needed_kwh = 4 steps × (5.0 × 3.5 × 0.25) = 17.5 kWh.
    With min_run_steps=4, the solver must run exactly 4 consecutive steps.

    Expected: all 4 active steps fall in [4, 7].
    """
    heat_per_step = 5.0 * 3.5 * 0.25  # 4.375 kWh per active step
    heat_needed = 4 * heat_per_step     # exactly 4 steps of full-power output

    ctx = _make_ctx(horizon=8)
    cfg = _config_on_off()
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=heat_needed))

    prices = [1.0] * 4 + [0.05] * 4
    cost = sum(
        prices[t] * cfg.elec_power_kw * device._hp_on[t] * ctx.dt
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    on_vals = [round(ctx.solver.var_value(device._hp_on[t])) for t in ctx.T]
    for t in range(4):
        assert on_vals[t] == 0, (
            f"Step {t} (expensive) should be off, got on={on_vals[t]}"
        )
    assert sum(on_vals[4:]) == 4, (
        f"Expected 4 active steps in [4,7], got {on_vals[4:]}"
    )


def test_space_heating_min_run_steps_respected() -> None:
    """With min_run_steps=4, the HP runs in consecutive blocks of ≥4 steps.

    heat_needed_kwh is small (2.0 kWh) — easily met by one step. Without
    min_run_steps, the solver would run 1 step. With min_run_steps=4, if it
    runs at all, it runs at least 4 consecutive steps.

    The total active steps must equal 0 or be a run of ≥4 consecutive steps.
    """
    ctx = _make_ctx(horizon=8)
    cfg = _config_on_off(min_run_steps=4)
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=2.0))

    # Uniform cost: solver minimises runs broadly.
    total_on = sum(device._hp_on[t] for t in ctx.T)
    ctx.solver.set_objective_minimize(total_on)
    ctx.solver.solve()

    on = [round(ctx.solver.var_value(device._hp_on[t])) for t in ctx.T]
    total = sum(on)

    if total > 0:
        # Check there are no isolated runs shorter than min_run_steps.
        # Verify all runs are consecutive >= 4 steps.
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
                f"Found a run of length {length} < min_run_steps=4. Schedule: {on}"
            )


def test_space_heating_zero_demand_produces_no_heat() -> None:
    """With heat_needed_kwh=0.0, all hp_on[t]=0 and no electricity is consumed."""
    ctx = _make_ctx(horizon=8)
    cfg = _config_on_off()
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=0.0))

    ctx.solver.set_objective_minimize(sum(device._hp_on[t] for t in ctx.T))
    ctx.solver.solve()

    for t in ctx.T:
        val = ctx.solver.var_value(device._hp_on[t])
        assert abs(val) < 1e-4, f"Step {t}: expected hp_on=0, got {val:.4f}"


def test_space_heating_net_power_negative_when_on() -> None:
    """net_power(t) ≈ -elec_power_kw when hp_on[t]=1, 0 when hp_on[t]=0.

    Forces hp_on[t]=1 at step 0 (by adding equality constraint) and verifies
    the AC bus draw equals -elec_power_kw.
    """
    ctx = _make_ctx(horizon=1)
    cfg = _config_on_off(min_run_steps=0)
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=0.0))

    ctx.solver.add_constraint(device._hp_on[0] == 1)
    ctx.solver.set_objective_minimize(device._hp_on[0])
    ctx.solver.solve()

    net = device.net_power(0)
    if isinstance(net, (int, float)):
        val = float(net)
    else:
        val = ctx.solver.var_value(net)
    assert abs(val - (-cfg.elec_power_kw)) < 1e-4, (
        f"Expected net_power={-cfg.elec_power_kw:.1f} kW, got {val:.4f} kW"
    )


# ---------------------------------------------------------------------------
# Power-stage (SOS2) mode
# ---------------------------------------------------------------------------


def test_space_heating_power_stages_sos2_respects_heat_total() -> None:
    """Staged mode: total thermal output satisfies heat_needed_kwh.

    Stages: off (0kW, COP=0), medium (3kW, COP=3.0), full (5kW, COP=3.5).
    heat_needed = 30 kWh. Max from medium stage only: 8 × 3×3×0.25 = 18 < 30.
    The solver must use the full-power stage at some steps.

    Asserts that sum(thermal output) >= 30 kWh.
    """
    stages = [_stage(0.0, 0.0), _stage(3.0, 3.0), _stage(5.0, 3.5)]
    ctx = _make_ctx(horizon=8)
    cfg = _config_staged(stages=stages, min_run_steps=0)
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=30.0))

    # Minimise total electrical consumption.
    total_elec = sum(
        sum(device._w[t][s] * stages[s].elec_kw for s in range(len(stages)))
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(total_elec)
    ctx.solver.solve()

    total_thermal = sum(
        ctx.solver.var_value(
            sum(device._w[t][s] * stages[s].elec_kw * stages[s].cop * ctx.dt
                for s in range(1, len(stages)))
        )
        for t in ctx.T
    )
    assert total_thermal >= 30.0 - 1e-3, (
        f"Total thermal {total_thermal:.4f} kWh < heat_needed 30.0 kWh"
    )


def test_space_heating_power_stages_at_most_two_adjacent_nonzero() -> None:
    """SOS2 constraint: at most two adjacent stage weights nonzero per step.

    Three stages: off (0), medium (3kW), full (5kW). After solving, for each
    step t, verify that non-adjacent stage pairs are not both nonzero. The
    forbidden pattern is w[0] > 0 and w[2] > 0 simultaneously (with w[1]=0).
    """
    stages = [_stage(0.0, 0.0), _stage(3.0, 3.0), _stage(5.0, 3.5)]
    ctx = _make_ctx(horizon=8)
    cfg = _config_staged(stages=stages, min_run_steps=0)
    device = SpaceHeatingDevice(name="hp", config=cfg)
    device.add_variables(ctx)
    device.add_constraints(ctx, inputs=_inputs(heat_needed_kwh=15.0))

    total_elec = sum(
        sum(device._w[t][s] * stages[s].elec_kw for s in range(len(stages)))
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(total_elec)
    ctx.solver.solve()

    for t in ctx.T:
        w = [ctx.solver.var_value(device._w[t][s]) for s in range(len(stages))]
        # Forbidden: stage 0 and stage 2 both positive while stage 1 is zero.
        assert not (w[0] > 1e-4 and w[2] > 1e-4 and w[1] < 1e-4), (
            f"Step {t}: non-adjacent stages active — w={[f'{v:.4f}' for v in w]}"
        )
