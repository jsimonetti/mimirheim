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

    # The reported economics are derived from the schedule but are not implied
    # by it: soc_credit_eur in particular reads per-device SOC, which the
    # step comparison below does not cover.
    assert result.naive_cost_eur == pytest.approx(golden.naive_cost_eur, abs=1e-6)
    assert result.optimised_cost_eur == pytest.approx(golden.optimised_cost_eur, abs=1e-6)
    assert result.soc_credit_eur == pytest.approx(golden.soc_credit_eur, abs=1e-6)

    # Compare schedule step-by-step.
    assert len(result.schedule) == len(golden.schedule)
    for step, g_step in zip(result.schedule, golden.schedule, strict=True):
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
            g_soc = g_step.devices[name].soc_kwh
            if g_soc is None:
                assert setpoint.soc_kwh is None, (
                    f"step={step.t} device={name!r}: "
                    f"gained a SOC reading of {setpoint.soc_kwh}"
                )
            else:
                assert setpoint.soc_kwh == pytest.approx(g_soc, abs=1e-4), (
                    f"step={step.t} device={name!r}: "
                    f"{setpoint.soc_kwh} != {g_soc}"
                )
            # Heat store fields: the commanded stage and the planned temperature.
            assert setpoint.boost_stage == g_step.devices[name].boost_stage, (
                f"step={step.t} device={name!r}: boost_stage "
                f"{setpoint.boost_stage} != {g_step.devices[name].boost_stage}"
            )
            g_temp = g_step.devices[name].temp_c
            if g_temp is None:
                assert setpoint.temp_c is None
            else:
                assert setpoint.temp_c == pytest.approx(g_temp, abs=1e-4), (
                    f"step={step.t} device={name!r}: temp_c "
                    f"{setpoint.temp_c} != {g_temp}"
                )
