"""Benchmark tests for the mimirheim MILP solve loop.

These tests measure the wall-clock time required by ``build_and_solve`` across
three scenarios of increasing complexity.  They use the ``pytest-benchmark``
fixture rather than assertions about timing — benchmarks never fail on
performance alone, only on correctness (``solve_status == "optimal"``).

Run with:
    uv run pytest tests/benchmarks/ --benchmark-only

Or include in the normal suite (benchmarked but not gated):
    uv run pytest tests/benchmarks/

Scenario descriptions
---------------------
minimal_home_24h:
    Single battery + AC-coupled PV + static load.  96 steps (24 h).
    This is the smallest meaningful MILP: a battery arbitrage problem
    on a single day with price spread and PV curtailment.  Expected
    solve time: < 50 ms.

prosumer_ev_48h:
    Battery + PV + EV (charge window) + DHW boiler + deferrable load.
    192 steps (48 h).  Exercises V2G-adjacent scheduling, thermal
    setpoint tracking, and discrete deferrable placement.  Expected
    solve time: < 500 ms.

worst_case_72h:
    Full installation: 11 kW grid, 1 standalone battery + 2 hybrid
    inverters (= 3 battery devices), 2 AC-coupled PV arrays summed into
    ``pv_forecast`` + 2 hybrid PV arrays (4 physical arrays), 2 EVs,
    DHW boiler, space heating heat pump (on/off binary per step), and 3
    deferrable loads.  288 steps (72 h).
    This is the largest horizon at which CBC reliably finds an optimal
    solution within the 59-second wall-clock budget.  The 7-day variant
    (672 steps, ~24 000 binary variables) exceeds the CBC branch-and-bound
    budget and never produces a feasible solution within the time limit.
    Expected solve time: < 10 s (informational only).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle
from mimirheim.core.model_builder import build_and_solve

BENCH_DIR = Path(__file__).parent


def _load(scenario_name: str) -> tuple[SolveBundle, MimirheimConfig]:
    """Load a benchmark scenario from its data directory.

    Args:
        scenario_name: Subdirectory name under ``tests/benchmarks/``.

    Returns:
        Tuple of (SolveBundle, MimirheimConfig) ready to pass to build_and_solve.
    """
    d = BENCH_DIR / scenario_name
    bundle = SolveBundle.model_validate(
        json.loads((d / "input.json").read_text(encoding="utf-8"))
    )
    config = MimirheimConfig.model_validate(
        yaml.safe_load((d / "config.yaml").read_text(encoding="utf-8"))
    )
    return bundle, config


@pytest.mark.benchmark(group="solve")
def test_bench_minimal_home_24h(benchmark: pytest.FixtureRequest) -> None:
    """Benchmark the 24-hour minimal residential scenario.

    Validates that the solver finds an optimal solution and measures the
    time taken for a single battery + AC PV + static load problem on a
    96-step horizon.

    Uses ``pedantic`` mode (1 round, 1 iteration) so the benchmark records a
    single wall-clock measurement.  pytest-benchmark's default calibration loop
    would repeat a MILP solve many times to achieve statistical stability, which
    is inappropriate for a function that takes seconds to run.
    """
    bundle, config = _load("minimal_home_24h")
    result = benchmark.pedantic(build_and_solve, args=(bundle, config), rounds=1, iterations=1)
    assert result.solve_status == "optimal", (
        f"Expected optimal solve, got: {result.solve_status}"
    )


@pytest.mark.benchmark(group="solve")
def test_bench_prosumer_ev_48h(benchmark: pytest.FixtureRequest) -> None:
    """Benchmark the 48-hour prosumer + EV scenario.

    Validates that the solver finds an optimal solution and measures the
    time taken for a battery + PV + EV + boiler + deferrable load problem
    on a 192-step horizon.

    Uses ``pedantic`` mode (1 round, 1 iteration) — see ``test_bench_minimal_home_24h``
    for rationale.
    """
    bundle, config = _load("prosumer_ev_48h")
    result = benchmark.pedantic(build_and_solve, args=(bundle, config), rounds=1, iterations=1)
    assert result.solve_status == "optimal", (
        f"Expected optimal solve, got: {result.solve_status}"
    )


@pytest.mark.benchmark(group="solve")
def test_bench_worst_case_72h(benchmark: pytest.FixtureRequest) -> None:
    """Benchmark the 72-hour worst-case full-installation scenario.

    Validates that the solver finds an optimal solution (within gap tolerance)
    for the largest expected residential installation: 3 batteries (including
    2 hybrid inverters), 4 PV sources, 2 EVs, DHW boiler, space heating heat
    pump, and 3 deferrable loads across a 288-step (72-hour) horizon.

    The 7-day (672-step) variant of this scenario exceeds what CBC can solve
    within the 59-second production time limit: the branch-and-bound tree for
    ~24 000 binary variables requires more search time than the budget allows.
    288 steps (~10 000 binary variables) is the largest horizon that terminates
    reliably within budget, measured at ~5 s wallclock on a 12-core machine.

    Both ``"optimal"`` (within 0.5 % gap tolerance) and ``"feasible"``
    (time-limited incumbent) are accepted: the 72-hour horizon is near the
    edge of the budget and may take slightly longer on slower hardware.

    This test runs once (rounds=1, iterations=1) rather than letting
    pytest-benchmark calibrate the repetition count. A full calibration run
    would exceed the CI time budget.
    """
    bundle, config = _load("worst_case_72h")
    result = benchmark.pedantic(build_and_solve, args=(bundle, config), rounds=1, iterations=1)
    assert result.solve_status in ("optimal", "feasible"), (
        f"Solver failed: {result.solve_status}"
    )
