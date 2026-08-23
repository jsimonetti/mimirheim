#!/usr/bin/env python3
"""Regenerate the committed reporter test fixtures from the current dump writer.

The reporter's render tests run against a dump pair committed under
``mimirheim_helpers/reporter/tests/fixtures/``. Committed JSON goes stale: a
field added to ``DeviceSetpoint`` does not appear in a fixture written months
earlier, so a test that renders it proves nothing about the current format.

This script solves a scenario, writes a dump with the real ``debug_dump``, and
copies the pair over the fixtures. Run it whenever the dump format changes, and
review the diff before committing, because a fixture change alters what every
render test sees.

Usage:

    uv run python scripts/refresh_reporter_fixtures.py
    uv run python scripts/refresh_reporter_fixtures.py --scenario flat_price
    uv run python scripts/refresh_reporter_fixtures.py --check

``--check`` writes nothing and exits non-zero when the committed fixtures are
missing a field the current models define. Useful in CI to catch drift without
regenerating anything.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve, debug_dump

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCENARIOS = _REPO_ROOT / "tests" / "scenarios"
_FIXTURES = (
    _REPO_ROOT / "mimirheim_helpers" / "reporter" / "tests" / "fixtures"
)

# The timestamp the reporter conftest looks for. Keeping the filenames stable
# means refreshing the content does not require touching the test code.
_FIXTURE_TS = "2026-04-03T15-30-00Z"


def _solve(scenario: str) -> tuple[SolveBundle, MimirheimConfig, SolveResult]:
    """Solve one scenario and return its bundle, config and result."""
    d = _SCENARIOS / scenario
    bundle = SolveBundle.model_validate(json.loads((d / "input.json").read_text()))
    config = MimirheimConfig.model_validate(
        yaml.safe_load((d / "config.yaml").read_text())
    )
    return bundle, config, build_and_solve(bundle, config)


def _missing_fields(out: dict) -> dict[str, list[str]]:
    """Return model fields absent from a dump, keyed by model name.

    ``exclude_none`` means a field legitimately missing because the device
    lacks that capability cannot be told apart from one the writer forgot. So
    this checks the union across every step and every device, which is the same
    thing ``tests/unit/test_debug_dump.py`` asserts against a result that
    populates everything.
    """
    setpoint_keys: set[str] = set()
    step_keys: set[str] = set()
    for step in out.get("schedule", []):
        step_keys |= set(step)
        for entry in step.get("devices", {}).values():
            setpoint_keys |= set(entry)

    return {
        "SolveResult": sorted(set(SolveResult.model_fields) - set(out)),
        "ScheduleStep": sorted(set(ScheduleStep.model_fields) - step_keys),
        "DeviceSetpoint": sorted(set(DeviceSetpoint.model_fields) - setpoint_keys),
    }


def _report(missing: dict[str, list[str]]) -> bool:
    """Print any missing fields. Returns True when everything is present."""
    clean = True
    for model, fields in missing.items():
        if fields:
            clean = False
            print(f"  {model}: missing {', '.join(fields)}")
    if clean:
        print("  every model field is present")
    return clean


def main() -> int:
    """Entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="high_price_spread",
        help="Scenario directory under tests/scenarios to solve. "
        "Pick one that exercises the devices the reporter charts.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report drift in the committed fixtures and write nothing.",
    )
    args = parser.parse_args()

    if args.check:
        path = _FIXTURES / f"{_FIXTURE_TS}_output.json"
        if not path.exists():
            print(f"Committed fixture missing: {path}")
            return 1
        print(f"Checking {path.name} against the current models:")
        return 0 if _report(_missing_fields(json.loads(path.read_text()))) else 1

    bundle, config, result = _solve(args.scenario)
    with tempfile.TemporaryDirectory() as tmp:
        paths = debug_dump(bundle, result, config, Path(tmp), max_dumps=5)
        if paths is None:
            print("debug_dump wrote nothing; is dump_dir set?")
            return 1
        input_path, output_path = paths
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, _FIXTURES / f"{_FIXTURE_TS}_input.json")
        shutil.copy2(output_path, _FIXTURES / f"{_FIXTURE_TS}_output.json")
        out = json.loads(output_path.read_text())

    print(f"Refreshed fixtures from scenario {args.scenario!r}:")
    print(f"  {_FIXTURES / f'{_FIXTURE_TS}_input.json'}")
    print(f"  {_FIXTURES / f'{_FIXTURE_TS}_output.json'}")
    print(f"  {len(out['schedule'])} steps, status {out['solve_status']}")
    _report(_missing_fields(out))
    print("Review the diff before committing; it changes what every render test sees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
