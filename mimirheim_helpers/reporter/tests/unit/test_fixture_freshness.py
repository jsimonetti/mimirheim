"""Guard that the committed reporter fixture matches the current dump format.

The render tests are only as good as the fixture they run against. A fixture
written before a field existed renders a report that silently omits it and
still passes, which is how a flat SOC chart survived for months. This asserts
the fixture carries every field the models define, so the fixture rots loudly
rather than quietly.

Regenerate with ``uv run python scripts/refresh_reporter_fixtures.py``.
"""
from __future__ import annotations

from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveResult


def _keys(fixture_out: dict) -> tuple[set[str], set[str], set[str]]:
    """Return the (result, step, setpoint) key unions present in the fixture."""
    step_keys: set[str] = set()
    setpoint_keys: set[str] = set()
    for step in fixture_out["schedule"]:
        step_keys |= set(step)
        for entry in step["devices"].values():
            setpoint_keys |= set(entry)
    return set(fixture_out), step_keys, setpoint_keys


def test_fixture_covers_every_solve_result_field(fixture_out: dict) -> None:
    """A field the fixture lacks is a field no render test can exercise."""
    result_keys, _, _ = _keys(fixture_out)
    missing = set(SolveResult.model_fields) - result_keys
    assert not missing, (
        f"fixture is missing SolveResult field(s) {sorted(missing)}; "
        "regenerate with scripts/refresh_reporter_fixtures.py"
    )


def test_fixture_covers_every_schedule_step_field(fixture_out: dict) -> None:
    """Same for the per-step fields."""
    _, step_keys, _ = _keys(fixture_out)
    missing = set(ScheduleStep.model_fields) - step_keys
    assert not missing, (
        f"fixture is missing ScheduleStep field(s) {sorted(missing)}; "
        "regenerate with scripts/refresh_reporter_fixtures.py"
    )


def test_fixture_covers_every_device_setpoint_field(fixture_out: dict) -> None:
    """Same for the per-device setpoint fields.

    Checked as a union across all steps and devices, because exclude_none
    means a single device legitimately omits the capabilities it lacks. The
    fixture is built with one device of each relevant kind so the union is
    complete.
    """
    _, _, setpoint_keys = _keys(fixture_out)
    missing = set(DeviceSetpoint.model_fields) - setpoint_keys
    assert not missing, (
        f"fixture is missing DeviceSetpoint field(s) {sorted(missing)}; "
        "regenerate with scripts/refresh_reporter_fixtures.py"
    )


def test_fixture_has_a_realistic_horizon(fixture_out: dict) -> None:
    """A handful of steps does not exercise the charts or the step table."""
    assert len(fixture_out["schedule"]) >= 48, (
        "fixture horizon is too short to be a meaningful render test"
    )
