"""Golden file scenario tests for build_and_solve.

Each test runs build_and_solve against a scenario's input.json and
config.yaml, then compares the result against golden.json.

To regenerate golden files after an intentional solver change:

    uv run pytest tests/scenarios/ --update-golden
"""

from pathlib import Path

import pytest

from mimirheim.core.bundle import SolveResult


def test_golden_scenario(
    scenario_dir: Path,
    request: pytest.FixtureRequest,
) -> None:
    """Run build_and_solve for the scenario and compare against golden.json.

    Loaded from the ``scenario_dir`` fixture parameterised in conftest.py.
    """
    # Import here to avoid circular imports at collection time.
    import json

    import yaml

    from mimirheim.config.schema import MimirheimConfig
    from mimirheim.core.bundle import SolveBundle
    from mimirheim.core.model_builder import build_and_solve

    # --- Load inputs ---
    raw = json.loads((scenario_dir / "input.json").read_text())
    bundle = SolveBundle.model_validate(raw)

    config_data = yaml.safe_load((scenario_dir / "config.yaml").read_text())
    config = MimirheimConfig.model_validate(config_data)

    # --- Solve ---
    result = build_and_solve(bundle, config)

    # --- Update or compare ---
    golden_path = scenario_dir / "golden.json"
    update = request.config.getoption("--update-golden", default=False)

    if update:
        golden_path.write_text(result.model_dump_json(indent=2))
        return

    if not golden_path.exists():
        pytest.fail(
            f"Golden file not found: {golden_path}. "
            "Run with --update-golden to generate it."
        )

    golden = SolveResult.model_validate_json(golden_path.read_text())

    # Compare top-level scalar fields.
    assert result.strategy == golden.strategy
    assert result.solve_status == golden.solve_status
    assert result.objective_value == pytest.approx(golden.objective_value, abs=1e-4)

    # Compare schedule step-by-step.
    assert len(result.schedule) == len(golden.schedule)
    for step, g_step in zip(result.schedule, golden.schedule):
        assert step.t == g_step.t
        assert step.grid_import_kw == pytest.approx(g_step.grid_import_kw, abs=1e-4)
        assert step.grid_export_kw == pytest.approx(g_step.grid_export_kw, abs=1e-4)
        assert set(step.devices.keys()) == set(g_step.devices.keys())
        for name, setpoint in step.devices.items():
            assert setpoint.type == g_step.devices[name].type
            assert setpoint.kw == pytest.approx(
                g_step.devices[name].kw, abs=1e-4
            ), (
                f"step={step.t} device={name!r}: "
                f"{setpoint.kw} != {g_step.devices[name].kw}"
            )
