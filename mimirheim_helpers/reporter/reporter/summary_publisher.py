"""Summary MQTT payload builder for mimirheim-reporter.

This module is a pure-function library: it takes parsed dump dicts and returns
plain Python dicts suitable for JSON serialisation and MQTT publication. It
does not open files, connect to MQTT, or import from mimirheim.

The output is consumed by ``ReporterDaemon`` after each report render and
published to the configured summary MQTT topic.

What this module does not do:

- It does not render HTML.
- It does not publish MQTT messages.
- It does not read configuration.
- It does not produce time-series chart data.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from reporter.metrics import compute_economic_metrics, compute_schedule_metrics


def build_summary_payload(inp: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Build a scalar economic summary payload from a dump pair.

    The summary contains the core economic performance indicators and grid
    exchange totals from the last solve. All monetary values are in EUR;
    energy totals are in kWh.

    Args:
        inp: Parsed SolveBundle JSON.
        out: Parsed SolveResult JSON.

    Returns:
        Dict with scalar fields suitable for JSON serialisation.
    """
    eco = compute_economic_metrics(out)
    schedule: list[dict[str, Any]] = out.get("schedule", [])
    sm = compute_schedule_metrics(schedule)

    return {
        "solve_time_utc": inp.get("triggered_at_utc") or inp.get("solve_time_utc", ""),
        "strategy": out.get("strategy", ""),
        "solve_status": out.get("solve_status", ""),
        "naive_cost_eur": eco.naive_cost_eur,
        "optimised_cost_eur": eco.optimised_cost_eur,
        "soc_credit_eur": eco.soc_credit_eur,
        "effective_cost_eur": eco.effective_cost_eur,
        "saving_eur": eco.saving_eur,
        "saving_pct": eco.saving_pct,
        "self_sufficiency_pct": sm.self_sufficiency_pct,
        "grid_import_kwh": sm.grid_import_kwh,
        "grid_export_kwh": sm.grid_export_kwh,
    }


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_utc(iso_str: str) -> datetime:
    """Parse an ISO 8601 UTC string to a timezone-aware datetime.

    Accepts ``Z`` suffix or ``+00:00`` offset. Returns epoch on parse failure
    so callers do not need to handle errors for a display-only timestamp field.
    """
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def _fmt_utc(dt: datetime) -> str:
    """Format a datetime as an ISO 8601 UTC string ending in 'Z'."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
