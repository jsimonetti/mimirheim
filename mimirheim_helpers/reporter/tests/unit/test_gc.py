"""Unit tests for reporter.gc.collect().

Verifies that collect() removes the correct HTML files and inventory entries
when the report count exceeds the configured maximum, and that it is a no-op
when the limit is not exceeded or when max_reports is 0 (unlimited).
"""
from __future__ import annotations

import json

import pytest

from reporter import gc as reporter_gc
from reporter import inventory as inv


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STEP_HOURS = 0.25


def _minimal_out() -> dict:
    return {
        "strategy": "minimize_cost",
        "solve_status": "optimal",
        "naive_cost_eur": 1.0,
        "optimised_cost_eur": 0.8,
        "soc_credit_eur": 0.0,
        "dispatch_suppressed": False,
        "schedule": [],
    }


def _populate(tmp_path, safe_tss: list[str]) -> list[str]:
    """Create inventory entries and dummy HTML report files for each safe_ts.

    Returns the list of report filenames in the same order.
    """
    filenames = []
    for safe_ts in safe_tss:
        ts_iso = safe_ts.replace("T", "T").replace("-", ":", 2) if False else _safe_to_iso(safe_ts)
        filename = f"{safe_ts}_report.html"
        html_path = tmp_path / filename
        html_path.write_text("<html></html>")
        inv.update(tmp_path, ts_iso, filename, {}, _minimal_out())
        filenames.append(filename)
    return filenames


def _safe_to_iso(safe_ts: str) -> str:
    """Convert a filename-safe timestamp to ISO 8601 format.

    ``"2026-04-02T14-00-00Z"`` -> ``"2026-04-02T14:00:00Z"``
    """
    if "T" not in safe_ts:
        return safe_ts
    date_part, time_part = safe_ts.split("T", 1)
    return date_part + "T" + time_part.replace("-", ":", 2)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_gc_noop_when_below_limit(tmp_path) -> None:
    """collect() must not remove anything when count <= max_reports."""
    _populate(tmp_path, ["2026-04-02T14-00-00Z", "2026-04-02T14-15-00Z"])
    reporter_gc.collect(tmp_path, max_reports=5)
    assert len(list(tmp_path.glob("*_report.html"))) == 2
    assert len(inv._read_inventory(tmp_path)) == 2


def test_gc_noop_when_at_limit(tmp_path) -> None:
    """collect() must not remove anything when count == max_reports."""
    _populate(tmp_path, ["2026-04-02T14-00-00Z", "2026-04-02T14-15-00Z"])
    reporter_gc.collect(tmp_path, max_reports=2)
    assert len(list(tmp_path.glob("*_report.html"))) == 2


def test_gc_removes_oldest_beyond_limit(tmp_path) -> None:
    """collect() must remove the oldest reports to bring count to max_reports."""
    safe_tss = [
        "2026-04-02T14-00-00Z",
        "2026-04-02T14-15-00Z",
        "2026-04-02T14-30-00Z",
        "2026-04-02T14-45-00Z",
        "2026-04-02T15-00-00Z",
    ]
    _populate(tmp_path, safe_tss)
    reporter_gc.collect(tmp_path, max_reports=3)
    remaining = sorted(tmp_path.glob("*_report.html"))
    assert len(remaining) == 3
    # Newest three must be retained.
    retained_names = {p.name for p in remaining}
    assert "2026-04-02T15-00-00Z_report.html" in retained_names
    assert "2026-04-02T14-45-00Z_report.html" in retained_names
    assert "2026-04-02T14-30-00Z_report.html" in retained_names
    # Oldest two must be deleted.
    assert not (tmp_path / "2026-04-02T14-00-00Z_report.html").exists()
    assert not (tmp_path / "2026-04-02T14-15-00Z_report.html").exists()


def test_gc_removes_inventory_entries(tmp_path) -> None:
    """collect() must also remove the corresponding inventory entries."""
    safe_tss = [
        "2026-04-02T14-00-00Z",
        "2026-04-02T14-15-00Z",
        "2026-04-02T14-30-00Z",
    ]
    _populate(tmp_path, safe_tss)
    reporter_gc.collect(tmp_path, max_reports=1)
    entries = inv._read_inventory(tmp_path)
    assert len(entries) == 1
    assert entries[0]["ts"] == _safe_to_iso("2026-04-02T14-30-00Z")


def test_gc_deletes_html_files(tmp_path) -> None:
    """collect() must actually unlink the HTML files from disk."""
    safe_tss = ["2026-04-02T14-00-00Z", "2026-04-02T14-15-00Z"]
    _populate(tmp_path, safe_tss)
    reporter_gc.collect(tmp_path, max_reports=1)
    assert not (tmp_path / "2026-04-02T14-00-00Z_report.html").exists()
    assert (tmp_path / "2026-04-02T14-15-00Z_report.html").exists()


def test_gc_zero_limit_is_unlimited(tmp_path) -> None:
    """max_reports == 0 must be treated as unlimited; nothing is removed."""
    safe_tss = [f"2026-04-02T{h:02d}-00-00Z" for h in range(10)]
    _populate(tmp_path, safe_tss)
    reporter_gc.collect(tmp_path, max_reports=0)
    assert len(list(tmp_path.glob("*_report.html"))) == 10
