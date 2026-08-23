#!/usr/bin/env python3
"""Regenerate the committed reporter test fixtures from the current dump writer.

The reporter's render tests run against a dump pair committed under
``mimirheim_helpers/reporter/tests/fixtures/``. Committed JSON goes stale: a
field added to ``DeviceSetpoint`` does not appear in a fixture written months
earlier, so a test that renders it proves nothing about the current format.

This script solves a purpose-built case, writes a dump with the real
``debug_dump``, and copies the pair over the fixtures. Run it whenever the dump
format changes, and review the diff before committing, because a fixture change
alters what every render test sees.

Usage:

    uv run python scripts/refresh_reporter_fixtures.py
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
from datetime import UTC, datetime, timedelta
from pathlib import Path

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import DeviceSetpoint, ScheduleStep, SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve, debug_dump

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = (
    _REPO_ROOT / "mimirheim_helpers" / "reporter" / "tests" / "fixtures"
)

# The timestamp the reporter conftest looks for. Keeping the filenames stable
# means refreshing the content does not require touching the test code.
_FIXTURE_TS = "2026-04-03T15-30-00Z"


# ---------------------------------------------------------------------------
# The fixture case
# ---------------------------------------------------------------------------
#
# Purpose-built rather than borrowed from tests/scenarios, because no scenario
# exercises every field the reporter charts. The solver scenarios cover solver
# behaviour; this covers report rendering, which needs one of everything:
#
#   home_battery  zero_exchange capability  -> zero_exchange_active, soc_kwh
#   roof_pv       staged production         -> power_limit_kw, pv_is_curtailed
#   garage_pv     on_off + zero_export      -> on_off_active
#   car           v2h + loadbalance, plugged-> loadbalance_active, soc_kwh
#   dishwasher    scheduling window         -> deferrable_recommended_starts
#   base_load     static forecast           -> a non-controllable series
#
# 96 steps, so the charts and the step table have a realistic amount to show,
# and a price ramp with enough spread that the solver actually dispatches.

_STEPS = 96
_T0 = datetime(2026, 4, 3, 15, 30, tzinfo=UTC)

_CONFIG: dict = {
    "mqtt": {"host": "localhost", "client_id": "reporter-fixture"},
    "outputs": {
        "schedule": "mimir/strategy/schedule", "current": "mimir/strategy/current",
        "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
    },
    "grid": {"import_limit_kw": 17.0, "export_limit_kw": 17.0},
    "batteries": {
        "home_battery": {
            "capacity_kwh": 10.0, "min_soc_kwh": 1.0,
            "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "mimir/battery/home_battery/exchange_mode"},
        }
    },
    "pv_arrays": {
        "roof_pv": {
            "topic_forecast": "mimir/input/pv/roof_pv/forecast",
            "max_power_kw": 6.0,
            "production_stages": [0.0, 2.0, 4.0, 6.0],
            "outputs": {
                "power_limit_kw": "mimir/pv/roof_pv/power_limit",
                "is_curtailed": "mimir/pv/roof_pv/is_curtailed",
            },
        },
        "garage_pv": {
            "topic_forecast": "mimir/input/pv/garage_pv/forecast",
            "max_power_kw": 2.0,
            "capabilities": {"on_off": True, "zero_export": True},
            "outputs": {
                "on_off_mode": "mimir/pv/garage_pv/on_off",
                "zero_export_mode": "mimir/pv/garage_pv/zero_export",
                "is_curtailed": "mimir/pv/garage_pv/is_curtailed",
            },
        },
    },
    "ev_chargers": {
        "car": {
            "capacity_kwh": 52.0, "min_soc_kwh": 5.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "discharge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"v2h": True, "zero_exchange": True, "loadbalance": True},
            "outputs": {
                "exchange_mode": "mimir/ev/car/exchange_mode",
                "loadbalance_cmd": "mimir/ev/car/loadbalance",
            },
        }
    },
    "static_loads": {"base_load": {"topic_forecast": "mimir/input/base_load/forecast"}},
    "deferrable_loads": {
        "dishwasher": {
            "power_profile": [1.8, 1.8, 0.6, 0.6],
            "topic_window_earliest": "mimir/input/dishwasher/earliest",
            "topic_window_latest": "mimir/input/dishwasher/latest",
            "topic_recommended_start_time": "mimir/dishwasher/recommended_start",
        }
    },
}



def _solar_curve(peak_kw: float, peak_hour: float, width_h: float) -> list[float]:
    """A smooth single-humped PV day, clipped at zero outside daylight."""
    out: list[float] = []
    for i in range(_STEPS):
        t = _T0 + timedelta(minutes=15 * i)
        hour = t.hour + t.minute / 60
        out.append(round(max(0.0, peak_kw * (1 - ((hour - peak_hour) / width_h) ** 2)), 3))
    return out


def _fixture_case() -> tuple[SolveBundle, MimirheimConfig]:
    """Build the bundle and config for the reporter fixture."""
    prices = [round(0.14 + 0.16 * ((i * 7) % _STEPS) / _STEPS, 4) for i in range(_STEPS)]
    bundle = {
        "strategy": "minimize_cost",
        "solve_time_utc": _T0.isoformat(),
        "triggered_at_utc": (_T0 + timedelta(seconds=41)).isoformat(),
        "horizon_prices": prices,
        "horizon_export_prices": [round(p - 0.06, 4) for p in prices],
        "horizon_confidence": [1.0] * _STEPS,
        "pv_forecast": _solar_curve(5.5, 13.5, 6.0),
        "base_load_forecast": [
            round(0.35 + 0.25 * ((i % 12) / 12), 3) for i in range(_STEPS)
        ],
        "battery_inputs": {"home_battery": {"soc_kwh": 4.6}},
        "ev_inputs": {
            "car": {
                "soc_kwh": 21.0,
                "available": True,
                "target_soc_kwh": 42.0,
                "window_latest": (_T0 + timedelta(hours=14)).isoformat(),
            }
        },
        "deferrable_windows": {
            "dishwasher": {
                "earliest": (_T0 + timedelta(hours=1)).isoformat(),
                "latest": (_T0 + timedelta(hours=10)).isoformat(),
            }
        },
    }
    return (
        SolveBundle.model_validate(bundle),
        MimirheimConfig.model_validate(_CONFIG),
    )


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

    bundle, config = _fixture_case()
    result = build_and_solve(bundle, config)
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

    print("Refreshed reporter fixtures:")
    print(f"  {_FIXTURES / f'{_FIXTURE_TS}_input.json'}")
    print(f"  {_FIXTURES / f'{_FIXTURE_TS}_output.json'}")
    print(f"  {len(out['schedule'])} steps, status {out['solve_status']}")
    _report(_missing_fields(out))
    print("Review the diff before committing; it changes what every render test sees.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
