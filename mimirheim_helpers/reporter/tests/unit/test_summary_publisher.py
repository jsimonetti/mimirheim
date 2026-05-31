"""Unit tests for reporter.summary_publisher.

Tests use small synthetic dump dicts constructed inline. No file I/O and no
real dump pair is needed.
"""
from __future__ import annotations

import pytest

from reporter.summary_publisher import build_summary_payload


# ---------------------------------------------------------------------------
# Synthetic dump helpers
# ---------------------------------------------------------------------------

_N = 4  # horizon length used by all synthetic dicts


def _make_inp(*, n: int = _N) -> dict:
    """Build a minimal synthetic SolveBundle JSON for testing."""
    return {
        "solve_time_utc": "2026-04-02T14:00:00Z",
        "horizon_prices": [0.20, 0.21, 0.22, 0.23][:n],
        "horizon_export_prices": [0.05, 0.05, 0.05, 0.05][:n],
        "config": {},
    }


def _make_out(*, n: int = _N) -> dict:
    """Build a minimal synthetic SolveResult JSON for testing."""
    schedule = []
    for i in range(n):
        step: dict = {
            "t": f"2026-04-02T{14 + i // 4:02d}:{(i % 4) * 15:02d}:00Z",
            "grid_import_kw": 1.0,
            "grid_export_kw": 0.0,
            "devices": {},
        }
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
# build_summary_payload — triggered_at_utc label
# ---------------------------------------------------------------------------


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
