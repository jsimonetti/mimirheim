"""Unit tests for reporter.chart_publisher.

Tests use small synthetic dump dicts constructed inline. No file I/O and no
real dump pair is needed.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from reporter.chart_publisher import build_chart_payload, build_summary_payload


# ---------------------------------------------------------------------------
# Synthetic dump helpers
# ---------------------------------------------------------------------------

_N = 4  # horizon length used by all synthetic dicts


def _make_inp(
    *,
    n: int = _N,
    has_pv: bool = False,
    has_baseload: bool = False,
    has_batteries: bool = False,
) -> dict:
    """Build a minimal synthetic SolveBundle JSON for testing."""
    inp: dict = {
        "solve_time_utc": "2026-04-02T14:00:00Z",
        "horizon_prices": [0.20, 0.21, 0.22, 0.23][:n],
        "horizon_export_prices": [0.05, 0.05, 0.05, 0.05][:n],
        "config": {},
    }
    if has_pv:
        inp["config"]["pv_arrays"] = {"roof_pv": {"max_power_kw": 5.0}}
    if has_batteries:
        inp["config"]["batteries"] = {
            "home_battery": {
                "capacity_kwh": 10.0,
                "min_soc_kwh": 0.5,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
            }
        }
        inp["battery_inputs"] = {"home_battery": {"soc_kwh": 5.0}}
    return inp


def _make_out(*, n: int = _N, has_pv: bool = False, has_batteries: bool = False) -> dict:
    """Build a minimal synthetic SolveResult JSON for testing."""
    schedule = []
    for i in range(n):
        step: dict = {
            "t": f"2026-04-02T{14 + i // 4:02d}:{(i % 4) * 15:02d}:00Z",
            "grid_import_kw": 1.0,
            "grid_export_kw": 0.0,
            "devices": {},
        }
        if has_pv:
            step["devices"]["roof_pv"] = {"type": "pv", "kw": 2.0}
        if has_batteries:
            step["devices"]["home_battery"] = {"type": "battery", "kw": -0.5}
        schedule.append(step)
    return {
        "solve_time_utc": "2026-04-02T14:00:00Z",
        "strategy": "minimize_cost",
        "solve_status": "ok",
        "naive_cost_eur": 3.12,
        "optimised_cost_eur": 2.00,
        "soc_credit_eur": 0.13,
        "schedule": schedule,
    }


# ---------------------------------------------------------------------------
# build_chart_payload — structure
# ---------------------------------------------------------------------------


def test_build_chart_payload_contains_required_keys() -> None:
    """build_chart_payload returns a dict with at least the required top-level keys."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    for key in ("solve_time_utc", "import_price", "export_price", "grid_import_kw", "grid_export_kw"):
        assert key in payload, f"build_chart_payload missing key {key!r}"


def test_series_entries_are_two_element_lists() -> None:
    """Every entry in each series array is a 2-element list [iso_str, float]."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    for series_key in ("import_price", "export_price", "grid_import_kw", "grid_export_kw"):
        series = payload[series_key]
        for entry in series:
            assert isinstance(entry, list) and len(entry) == 2, (
                f"{series_key}: expected [iso_str, float] entries, got {entry!r}"
            )
            iso_str, value = entry
            # The ISO string must be parseable.
            datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            assert isinstance(value, (int, float)), (
                f"{series_key}: expected numeric value, got {type(value)}"
            )


def test_series_length_matches_horizon_prices() -> None:
    """All series arrays have the same length as inp['horizon_prices']."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    expected_len = len(inp["horizon_prices"])
    for key in ("import_price", "export_price", "grid_import_kw", "grid_export_kw"):
        assert len(payload[key]) == expected_len, (
            f"Series {key!r} has {len(payload[key])} entries, expected {expected_len}"
        )


def test_pv_series_present_when_pv_in_schedule() -> None:
    """pv_kw series is present when schedule contains PV device entries."""
    inp = _make_inp(has_pv=True)
    out = _make_out(has_pv=True)
    payload = build_chart_payload(inp, out)
    assert "pv_kw" in payload, "Expected pv_kw series when PV devices present"


def test_baseload_series_absent_when_no_baseload() -> None:
    """baseload_kw series is absent when no static_load devices are in the schedule."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    assert "baseload_kw" not in payload


def test_device_series_included_for_batteries() -> None:
    """Battery SOC and charge series appear under the battery__{name}__ naming pattern."""
    inp = _make_inp(has_batteries=True)
    out = _make_out(has_batteries=True)
    payload = build_chart_payload(inp, out)
    soc_key = "battery__home_battery__soc_kwh"
    charge_key = "battery__home_battery__charge_kw"
    assert soc_key in payload, (
        f"Expected {soc_key!r} in chart payload. Got keys: {list(payload.keys())}"
    )
    assert charge_key in payload, (
        f"Expected {charge_key!r} in chart payload. Got keys: {list(payload.keys())}"
    )


def test_device_series_absent_when_no_batteries() -> None:
    """No battery__ series appear when the schedule has no battery devices."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    bat_keys = [k for k in payload if k.startswith("battery__")]
    assert bat_keys == [], f"Unexpected battery series: {bat_keys}"


def test_battery_soc_series_length_is_n_plus_one() -> None:
    """Battery SOC series has len(schedule) + 1 entries (initial + one per step)."""
    inp = _make_inp(has_batteries=True)
    out = _make_out(has_batteries=True)
    payload = build_chart_payload(inp, out)
    soc = payload["battery__home_battery__soc_kwh"]
    assert len(soc) == _N + 1, (
        f"SOC series length {len(soc)} != {_N + 1}"
    )


def test_battery_soc_series_timestamps_are_step_end_times() -> None:
    """Battery SOC entries are stamped at step-end times, not step-start times.

    The initial entry sits at the solve origin. Entry i+1 is the SOC after
    step i completes, so its timestamp must be the step's start plus one
    15-minute interval — not the step's start timestamp itself.
    """
    from datetime import timedelta as _td
    inp = _make_inp(has_batteries=True)
    out = _make_out(has_batteries=True)
    payload = build_chart_payload(inp, out)
    soc = payload["battery__home_battery__soc_kwh"]
    # Entry 0: initial SOC stamped at the slot boundary (origin = solve_time_utc).
    assert soc[0][0] == inp["solve_time_utc"]
    # Entries 1..N: stamped at step-end = step-start + 15 min.
    schedule = out["schedule"]
    for i, step in enumerate(schedule):
        step_end = (
            datetime.fromisoformat(step["t"].replace("Z", "+00:00")) + _td(minutes=15)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert soc[i + 1][0] == step_end, (
            f"soc[{i + 1}] timestamp {soc[i + 1][0]!r} != step {i} end {step_end!r}"
        )


def test_solve_time_utc_matches_input() -> None:
    """solve_time_utc in the payload falls back to inp['solve_time_utc'] when
    triggered_at_utc is absent."""
    inp = _make_inp()
    out = _make_out()
    payload = build_chart_payload(inp, out)
    assert payload["solve_time_utc"] == inp["solve_time_utc"]


def test_triggered_at_utc_used_as_label_when_present() -> None:
    """solve_time_utc in the chart payload is taken from triggered_at_utc when
    that field is present, while solve_time_utc (slot boundary) is still used
    as the price series origin so step timestamps remain correctly aligned."""
    inp = _make_inp()
    inp["triggered_at_utc"] = "2026-04-02T14:04:32Z"
    out = _make_out()
    payload = build_chart_payload(inp, out)
    # Label reflects the trigger time, not the floored slot boundary.
    assert payload["solve_time_utc"] == "2026-04-02T14:04:32Z"
    # The price series origin must still be the slot boundary (14:00).
    first_price_ts = payload["import_price"][0][0]
    assert first_price_ts == "2026-04-02T14:00:00Z"


def test_triggered_at_utc_used_in_summary_when_present() -> None:
    """build_summary_payload uses triggered_at_utc as the solve_time_utc label."""
    inp = _make_inp()
    inp["triggered_at_utc"] = "2026-04-02T14:04:32Z"
    out = _make_out()
    summary = build_summary_payload(inp, out)
    assert summary["solve_time_utc"] == "2026-04-02T14:04:32Z"


# ---------------------------------------------------------------------------
# build_summary_payload — structure
# ---------------------------------------------------------------------------


def test_build_summary_payload_contains_required_keys() -> None:
    """build_summary_payload returns all expected scalar keys."""
    inp = _make_inp()
    out = _make_out()
    summary = build_summary_payload(inp, out)
    required = (
        "solve_time_utc",
        "strategy",
        "solve_status",
        "naive_cost_eur",
        "optimised_cost_eur",
        "soc_credit_eur",
        "effective_cost_eur",
        "saving_eur",
        "saving_pct",
        "self_sufficiency_pct",
        "grid_import_kwh",
        "grid_export_kwh",
    )
    for key in required:
        assert key in summary, f"build_summary_payload missing key {key!r}"


def test_build_summary_saving_pct_correct() -> None:
    """saving_pct = (naive - effective) / naive * 100."""
    inp = _make_inp()
    out = _make_out()
    # naive=3.12, optimised=2.00, credit=0.13 → effective=1.87, saving=1.25
    summary = build_summary_payload(inp, out)
    effective = out["optimised_cost_eur"] - out["soc_credit_eur"]
    saving = out["naive_cost_eur"] - effective
    expected_pct = round(saving / out["naive_cost_eur"] * 100.0, 1)
    assert abs(summary["saving_pct"] - expected_pct) < 0.01, (
        f"saving_pct {summary['saving_pct']} != expected {expected_pct}"
    )


def test_build_summary_saving_pct_zero_naive_handled() -> None:
    """When naive_cost_eur is 0, saving_pct is 0.0 (no ZeroDivisionError)."""
    inp = _make_inp()
    out = {**_make_out(), "naive_cost_eur": 0.0, "optimised_cost_eur": 0.0, "soc_credit_eur": 0.0}
    summary = build_summary_payload(inp, out)
    assert summary["saving_pct"] == 0.0


def test_build_summary_effective_cost_is_optimised_minus_credit() -> None:
    """effective_cost_eur == optimised_cost_eur - soc_credit_eur."""
    inp = _make_inp()
    out = _make_out()
    summary = build_summary_payload(inp, out)
    expected = out["optimised_cost_eur"] - out["soc_credit_eur"]
    assert abs(summary["effective_cost_eur"] - expected) < 1e-9


def test_build_summary_grid_kwh_derived_from_schedule() -> None:
    """grid_import_kwh and grid_export_kwh are summed from the schedule."""
    inp = _make_inp()
    out = _make_out()
    summary = build_summary_payload(inp, out)
    # Each of 4 steps has grid_import_kw=1.0; 15-min steps = 0.25 h each → 4 * 0.25 = 1.0 kWh
    assert abs(summary["grid_import_kwh"] - 1.0) < 1e-6
    assert summary["grid_export_kwh"] == 0.0
