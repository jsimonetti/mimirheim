"""Unit tests for mimirheim/devices/deferrable_load.py.

All tests must fail before the implementation exists (TDD). Tests use T=8,
dt=0.25 (2-hour horizon) unless noted otherwise.
"""

from datetime import UTC, datetime, timedelta

import pytest

from mimirheim.config.schema import DeferrableLoadConfig
from mimirheim.core.bundle import DeferrableWindow
from mimirheim.core.context import ModelContext
from mimirheim.core.solver_backend import CBCSolverBackend
from mimirheim.devices.deferrable_load import DeferrableLoad


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _config(
    power_profile: list[float] | None = None,
    duration_steps: int | None = None,
    flat_kw: float = 1.5,
) -> DeferrableLoadConfig:
    """Construct a DeferrableLoadConfig for testing.

    Convenience overloads:
    - ``power_profile``: use this profile directly.
    - ``duration_steps``: build a flat profile of ``flat_kw`` repeated
      ``duration_steps`` times (mirrors the old API for test brevity).
    - Neither: flat profile of ``flat_kw`` repeated 2 times.
    """
    if power_profile is None:
        steps = duration_steps if duration_steps is not None else 2
        power_profile = [flat_kw] * steps
    return DeferrableLoadConfig(
        power_profile=power_profile,
        topic_window_earliest="mimir/load/wash/window_earliest",
        topic_window_latest="mimir/load/wash/window_latest",
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _make_ctx(horizon: int = 8) -> ModelContext:
    return ModelContext(solver=CBCSolverBackend(), horizon=horizon, dt=0.25)


def _full_window(solve_time: datetime, horizon: int = 8, dt: float = 0.25) -> DeferrableWindow:
    """A window that spans the entire horizon."""
    return DeferrableWindow(
        earliest=solve_time,
        latest=solve_time + timedelta(hours=horizon * dt),
    )


# ---------------------------------------------------------------------------
# Basic scheduling behaviour
# ---------------------------------------------------------------------------


def test_deferrable_load_runs_exactly_once() -> None:
    """The load must start exactly once within the window (sum of start[t] == 1)."""
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config(duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    # Minimise anything — just need a feasible solution.
    ctx.solver.set_objective_minimize(load.start[0])
    ctx.solver.solve()

    total_starts = sum(ctx.solver.var_value(load.start[t]) for t in ctx.T)
    assert abs(total_starts - 1.0) < 1e-6


def test_deferrable_load_completes_within_window() -> None:
    """The load must not run outside the declared window."""
    solve_time = _now()
    # Window: steps 2–5 only (0.5 h offset, 1.5 h duration)
    window = DeferrableWindow(
        earliest=solve_time + timedelta(hours=0.5),
        latest=solve_time + timedelta(hours=1.5),
    )
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config(duration_steps=1))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=window, solve_time_utc=solve_time)

    ctx.solver.set_objective_minimize(load.start[2])
    ctx.solver.solve()

    # Steps outside the window must have start == 0.
    for t in [0, 1, 6, 7]:
        assert ctx.solver.var_value(load.start[t]) < 1e-6, (
            f"step {t}: start should be 0 outside window"
        )


def test_deferrable_load_power_correct_when_running() -> None:
    """net_power must reflect the profile at running steps and be 0 elsewhere."""
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.5, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    # Force start at step 3.
    ctx.solver.add_constraint(load.start[3] == 1.0)
    ctx.solver.set_objective_minimize(load.start[0])
    ctx.solver.solve()

    # Steps 3 and 4 are running; net_power should evaluate to -1.5.
    for t in ctx.T:
        np_val = ctx.solver.var_value(load.net_power(t))
        if t in (3, 4):
            assert abs(np_val - (-1.5)) < 1e-6, f"step {t}: expected -1.5, got {np_val}"
        else:
            assert abs(np_val) < 1e-6, f"step {t}: expected 0.0, got {np_val}"


def test_deferrable_load_no_window_skips_all_constraints() -> None:
    """When window is None, no variables are added and net_power returns 0."""
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config())
    load.add_variables(ctx)
    load.add_constraints(ctx, window=None, solve_time_utc=_now())

    assert len(load.start) == 0
    for t in ctx.T:
        assert load.net_power(t) == 0


def test_deferrable_load_runs_at_cheapest_time() -> None:
    """Solver should start load at steps with cheapest import price."""
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.0, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    # Import price is low at steps 4 and 5, high elsewhere.
    prices = [0.30] * 8
    prices[4] = 0.05
    prices[5] = 0.05

    # Objective: minimise cost = Σ_t prices[t] * |net_power(t)| * dt
    # Since net_power is a solver expression, build the cost directly from start[t].
    # Running at step t means start[t'] = 1 for some t' in [t-duration+1, t].
    # Cheapest pair is start[4]=1 (runs at 4 and 5).
    cost = sum(
        prices[t] * load.net_power(t) * (-ctx.dt)  # net_power negative, so negate
        for t in ctx.T
    )
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    assert abs(ctx.solver.var_value(load.start[4]) - 1.0) < 1e-6, (
        "Expected load to start at step 4 (cheapest price pair)"
    )


def test_deferrable_load_net_power_negative() -> None:
    """Load consumes power — net_power must be negative (or zero when idle)."""
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(name="wash", config=_config(flat_kw=2.0, duration_steps=1))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    ctx.solver.set_objective_minimize(load.start[0])
    ctx.solver.solve()

    for t in ctx.T:
        np_val = ctx.solver.var_value(load.net_power(t))
        assert np_val <= 1e-6, f"step {t}: net_power should be <= 0, got {np_val}"


# ---------------------------------------------------------------------------
# Running state: start_time is known (Option B — externally published)
# ---------------------------------------------------------------------------


def test_deferrable_load_running_has_fixed_draw() -> None:
    """When start_time falls within the current horizon, the steps still running
    must contribute the correct profile power. No binary variable is used."""
    solve_time = _now()
    # Load started one step ago; 1 step of flat profile (1.5 kW) remains.
    start_time = solve_time - timedelta(minutes=15)
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.5, duration_steps=2))


def test_deferrable_load_running_all_steps_remaining() -> None:
    """When start_time equals solve_start the load runs for the full duration."""
    solve_time = _now()
    start_time = solve_time  # starts right now
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=2.0, duration_steps=3))
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=None,
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    assert len(load.start) == 0

    for t in range(3):
        assert load.net_power(t) == pytest.approx(-2.0)
    for t in range(3, 8):
        assert load.net_power(t) == pytest.approx(0.0)


def test_deferrable_load_completed_contributes_nothing() -> None:
    """When start_time is in the past and the run has ended, net_power is 0
    for all steps — the completed load is excluded silently."""
    solve_time = _now()
    # Load finished 30 minutes ago (duration 2, started 45 minutes ago).
    start_time = solve_time - timedelta(minutes=45)
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.5, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=None,
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    assert len(load.start) == 0
    for t in range(8):
        assert load.net_power(t) == pytest.approx(0.0)


def test_deferrable_load_start_time_overrides_window() -> None:
    """When both start_time and window are provided, the device is in 'running'
    state and the window is ignored — no binary variable is created."""
    solve_time = _now()
    start_time = solve_time  # currently running
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.0, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=_full_window(solve_time),  # window also present — must be ignored
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    # Running state: no binary variables.
    assert len(load.start) == 0
    assert load.net_power(0) == pytest.approx(-1.0)
    assert load.net_power(1) == pytest.approx(-1.0)
    for t in range(2, 8):
        assert load.net_power(t) == pytest.approx(0.0)


def test_deferrable_load_config_topic_committed_start_time_optional() -> None:
    """DeferrableLoadConfig is valid without topic_committed_start_time (field is optional)."""
    cfg = DeferrableLoadConfig(
        power_profile=[1.0, 0.8, 0.8, 1.2],
        topic_window_earliest="mimir/load/wash/window_earliest",
        topic_window_latest="mimir/load/wash/window_latest",
    )
    assert cfg.topic_committed_start_time is None


def test_deferrable_load_config_topic_committed_start_time_accepted() -> None:
    """DeferrableLoadConfig accepts a topic_committed_start_time string."""
    cfg = DeferrableLoadConfig(
        power_profile=[1.0, 0.8, 0.8, 1.2],
        topic_window_earliest="mimir/load/wash/window_earliest",
        topic_window_latest="mimir/load/wash/window_latest",
        topic_committed_start_time="mimir/load/wash/start_time",
    )
    assert cfg.topic_committed_start_time == "mimir/load/wash/start_time"


def test_deferrable_load_config_topic_recommended_start_optional() -> None:
    """DeferrableLoadConfig is valid without topic_recommended_start_time (field is optional)."""
    cfg = DeferrableLoadConfig(
        power_profile=[1.0, 0.8],
        topic_window_earliest="mimir/load/wash/window_earliest",
        topic_window_latest="mimir/load/wash/window_latest",
    )
    assert cfg.topic_recommended_start_time is None


def test_deferrable_load_config_topic_recommended_start_accepted() -> None:
    """DeferrableLoadConfig accepts a topic_recommended_start_time string."""
    cfg = DeferrableLoadConfig(
        power_profile=[1.0, 0.8],
        topic_window_earliest="mimir/load/wash/window_earliest",
        topic_window_latest="mimir/load/wash/window_latest",
        topic_recommended_start_time="mimir/load/wash/recommended_start",
    )
    assert cfg.topic_recommended_start_time == "mimir/load/wash/recommended_start"


# ---------------------------------------------------------------------------
# Non-uniform power profile
# ---------------------------------------------------------------------------


def test_deferrable_load_non_uniform_profile_power_at_each_step() -> None:
    """net_power at each running step reflects the corresponding profile entry,
    not a flat constant.

    Profile [2.0, 0.5, 3.0]: step 0 of run = 2.0 kW, step 1 = 0.5 kW, step 2 = 3.0 kW.
    When the load is forced to start at horizon step 1, net_power should be:
    - step 1: -2.0 (profile[0])
    - step 2: -0.5 (profile[1])
    - step 3: -3.0 (profile[2])
    - all other steps: 0.0
    """
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(
        name="wash",
        config=_config(power_profile=[2.0, 0.5, 3.0]),
    )
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    # Force start at step 1.
    ctx.solver.add_constraint(load.start[1] == 1.0)
    ctx.solver.set_objective_minimize(load.start[0])
    ctx.solver.solve()

    expected = {1: -2.0, 2: -0.5, 3: -3.0}
    for t in ctx.T:
        np_val = ctx.solver.var_value(load.net_power(t))
        want = expected.get(t, 0.0)
        assert abs(np_val - want) < 1e-6, (
            f"step {t}: expected {want}, got {np_val}"
        )


def test_deferrable_load_solver_picks_cheap_steps_with_varied_profile() -> None:
    """With a non-uniform profile the solver picks the start time that minimises
    the sum cost, accounting for the actual power at each step.

    Profile [3.0, 0.5]: step 0 of run draws 3.0 kW, step 1 draws 0.5 kW.
    Price array: cheap at steps 0,1, expensive at steps 2,3, etc.
    Prices = [0.05, 0.05, 0.30, 0.30, 0.30, 0.30, 0.30, 0.30].
    Starting at step 0 puts the heavy 3.0-kW step at the cheapest price;
    starting at step 1 puts the heavy step at price 0.30. Optimal is start=0.
    """
    solve_time = _now()
    ctx = _make_ctx()
    load = DeferrableLoad(
        name="wash",
        config=_config(power_profile=[3.0, 0.5]),
    )
    load.add_variables(ctx)
    load.add_constraints(ctx, window=_full_window(solve_time), solve_time_utc=solve_time)

    prices = [0.05, 0.05] + [0.30] * 6
    cost = sum(prices[t] * load.net_power(t) * (-ctx.dt) for t in ctx.T)
    ctx.solver.set_objective_minimize(cost)
    ctx.solver.solve()

    assert abs(ctx.solver.var_value(load.start[0]) - 1.0) < 1e-6, (
        "Expected load to start at step 0 to hit cheap price with high-power step"
    )


def test_deferrable_load_running_non_uniform_profile() -> None:
    """In running state with a non-uniform profile, net_power returns the correct
    profile entry for each remaining step, offset by elapsed_steps.

    Profile [2.0, 1.0, 0.5]: 3-step run. Load was started 1 step ago
    (elapsed=1). Remaining profile: [1.0, 0.5].
    - horizon step 0: profile[1] = 1.0 kW
    - horizon step 1: profile[2] = 0.5 kW
    - horizon steps 2+: 0.0 kW
    """
    solve_time = _now()
    start_time = solve_time - timedelta(minutes=15)  # 1 step ago
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(
        name="wash",
        config=_config(power_profile=[2.0, 1.0, 0.5]),
    )
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=None,
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    assert len(load.start) == 0
    assert load.net_power(0) == pytest.approx(-1.0)
    assert load.net_power(1) == pytest.approx(-0.5)
    for t in range(2, 8):
        assert load.net_power(t) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Future start_time: committed state (new behaviour)
# ---------------------------------------------------------------------------


def test_deferrable_load_future_start_is_committed() -> None:
    """When start_time is set to a future time, the schedule is treated as
    committed: no binary variable is created and net_power returns the profile
    values at the scheduled horizon steps.

    The automation publishing a future start_time means it has accepted the
    schedule and programmed the device. mimirheim must not re-optimise.

    Profile [2.0, 0.5]: starts at horizon step 2 (30 minutes from now).
    - steps 0, 1: 0.0 (not yet running)
    - step  2:   -2.0 (profile[0])
    - step  3:   -0.5 (profile[1])
    - steps 4+:   0.0 (run complete)
    """
    solve_time = _now()
    start_time = solve_time + timedelta(minutes=30)  # 2 steps in the future
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(power_profile=[2.0, 0.5]))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=None, solve_time_utc=solve_time, start_time=start_time)

    # No binary variables — schedule is committed, not re-optimised.
    assert len(load.start) == 0

    assert load.net_power(0) == pytest.approx(0.0)
    assert load.net_power(1) == pytest.approx(0.0)
    assert load.net_power(2) == pytest.approx(-2.0)
    assert load.net_power(3) == pytest.approx(-0.5)
    for t in range(4, 8):
        assert load.net_power(t) == pytest.approx(0.0)


def test_deferrable_load_future_start_window_ignored() -> None:
    """When start_time is set to a future time, any window present is ignored —
    the device is in committed state and must not be re-scheduled."""
    solve_time = _now()
    start_time = solve_time + timedelta(minutes=30)  # 2 steps in the future
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(power_profile=[2.0, 0.5]))
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=_full_window(solve_time),  # window present — must be ignored
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    # Committed: no binary variables despite window being present.
    assert len(load.start) == 0
    assert load.net_power(2) == pytest.approx(-2.0)
    assert load.net_power(3) == pytest.approx(-0.5)


def test_deferrable_load_future_start_beyond_horizon_no_power() -> None:
    """When the committed start_time falls beyond the current horizon, the
    device contributes zero power to the balance in this planning window."""
    solve_time = _now()
    # 8-step horizon at 15 min/step = 2 hours; start 3 hours from now
    start_time = solve_time + timedelta(hours=3)
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(power_profile=[2.0, 0.5]))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=None, solve_time_utc=solve_time, start_time=start_time)

    assert len(load.start) == 0
    for t in ctx.T:
        assert load.net_power(t) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Completed load rescheduled (new behaviour)
# ---------------------------------------------------------------------------


def test_deferrable_load_completed_rescheduled_when_window_present() -> None:
    """When start_time + duration is entirely in the past and a window is
    provided, the load is re-scheduled for a new run within the window.

    The previous run is over; the automation should have cleared the
    start_time topic, but even if it has not, mimirheim reschedules.
    """
    solve_time = _now()
    # Profile = 2 steps (30 min). Started 45 min ago — fully completed.
    start_time = solve_time - timedelta(minutes=45)
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.5, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(
        ctx,
        window=_full_window(solve_time),
        solve_time_utc=solve_time,
        start_time=start_time,
    )

    # Binary variables must have been created — load is being re-scheduled.
    assert len(load.start) > 0

    ctx.solver.set_objective_minimize(load.start[0])
    ctx.solver.solve()
    total_starts = sum(ctx.solver.var_value(load.start[t]) for t in ctx.T)
    assert abs(total_starts - 1.0) < 1e-6, "Expected exactly one start after rescheduling"


def test_deferrable_load_completed_no_window_still_silent() -> None:
    """When start_time + duration is in the past and no window is provided,
    no rescheduling occurs and net_power is 0 for all steps."""
    solve_time = _now()
    start_time = solve_time - timedelta(minutes=45)
    ctx = _make_ctx(horizon=8)
    load = DeferrableLoad(name="wash", config=_config(flat_kw=1.5, duration_steps=2))
    load.add_variables(ctx)
    load.add_constraints(ctx, window=None, solve_time_utc=solve_time, start_time=start_time)

    assert len(load.start) == 0
    for t in ctx.T:
        assert load.net_power(t) == pytest.approx(0.0)
