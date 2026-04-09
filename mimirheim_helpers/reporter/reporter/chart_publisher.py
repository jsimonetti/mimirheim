"""Chart and summary MQTT payload builders for mimirheim-reporter.

This module is a pure-function library: it takes parsed dump dicts and returns
plain Python dicts suitable for JSON serialisation and MQTT publication. It
does not open files, connect to MQTT, or import from mimirheim.

The outputs are consumed by ``ReporterDaemon`` after each report render and
published to the configured chart and summary MQTT topics.

What this module does not do:

- It does not render HTML.
- It does not publish MQTT messages.
- It does not read configuration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

# Each solver time step is 15 minutes.
_STEP_MINUTES = 15
_STEP_HOURS = _STEP_MINUTES / 60.0


def build_chart_payload(inp: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Build an apex-charts-compatible time-series payload from a dump pair.

    Each series is a list of ``[ISO-timestamp-string, float]`` pairs, one entry
    per 15-minute solver time step. The ISO timestamps are UTC strings that can
    be converted to milliseconds in JavaScript via ``new Date(ts).getTime()``.

    Price and grid series have one entry per step (one for each horizon price).
    Battery SOC series have ``len(schedule) + 1`` entries: the initial SOC
    followed by the end-of-step SOC for each step in the schedule.

    Per-device series use the naming pattern::

        {device_type}__{device_name}__{metric}

    where double underscores separate the three components. Device types are
    ``battery``, ``pv``, ``load``. Metrics are ``soc_kwh``, ``charge_kw``,
    ``discharge_kw``, ``power_kw`` (where applicable). Only device types
    present in the schedule are included.

    Args:
        inp: Parsed SolveBundle JSON (the ``*_input.json`` dump).
        out: Parsed SolveResult JSON (the ``*_output.json`` dump).

    Returns:
        Dict with a ``solve_time_utc`` string and named series arrays.
    """
    solve_time = inp.get("solve_time_utc", "")
    import_prices: list[float] = inp.get("horizon_prices", [])
    export_prices: list[float] = inp.get("horizon_export_prices", [])
    schedule: list[dict[str, Any]] = out.get("schedule", [])
    n = len(import_prices)

    # Build the timestamp array for price/grid series using the slot boundary
    # (solve_time_utc) as the origin. This keeps price and schedule step
    # timestamps aligned on 15-minute boundaries regardless of when the
    # trigger fired.
    origin = _parse_utc(solve_time)
    timestamps = [
        _fmt_utc(origin + timedelta(minutes=i * _STEP_MINUTES))
        for i in range(n)
    ]

    # Use triggered_at_utc (wall-clock trigger time) as the human-visible
    # label. Falls back to solve_time_utc for bundles that pre-date the field
    # (test fixtures, old dump files).
    label = inp.get("triggered_at_utc") or solve_time
    payload: dict[str, Any] = {"solve_time_utc": label}

    # Price series aligned to horizon length.
    payload["import_price"] = [[timestamps[i], import_prices[i]] for i in range(n)]
    payload["export_price"] = [[timestamps[i], export_prices[i]] for i in range(n)]

    # Grid series sourced from the schedule. Use timestamps from schedule steps
    # when available; fall back to the derived timestamps.
    step_timestamps = [s.get("t", timestamps[i]) for i, s in enumerate(schedule[:n])]

    payload["grid_import_kw"] = [
        [step_timestamps[i], schedule[i].get("grid_import_kw", 0.0)]
        for i in range(min(n, len(schedule)))
    ]
    payload["grid_export_kw"] = [
        [step_timestamps[i], schedule[i].get("grid_export_kw", 0.0)]
        for i in range(min(n, len(schedule)))
    ]

    # Aggregate PV and baseload across devices in the schedule.
    pv_series: list[list] = []
    baseload_series: list[list] = []
    for i, step in enumerate(schedule[:n]):
        ts = step_timestamps[i]
        pv_kw = sum(
            max(0.0, d.get("kw", 0.0))
            for d in step.get("devices", {}).values()
            if d.get("type") == "pv"
        )
        load_kw = sum(
            max(0.0, d.get("kw", 0.0))
            for d in step.get("devices", {}).values()
            if d.get("type") in ("static_load", "deferrable_load")
        )
        pv_series.append([ts, pv_kw])
        baseload_series.append([ts, load_kw])

    # Only include PV/baseload series when at least one device contributes.
    if any(entry[1] > 0.0 for entry in pv_series):
        payload["pv_kw"] = pv_series

    if any(entry[1] > 0.0 for entry in baseload_series):
        payload["baseload_kw"] = baseload_series

    # Per-battery SOC and charge/discharge series.
    cfg = inp.get("config", {})
    battery_inputs = inp.get("battery_inputs", {})

    for bat_name, bat_cfg in cfg.get("batteries", {}).items():
        initial_soc = battery_inputs.get(bat_name, {}).get("soc_kwh", 0.0)
        charge_eff = _avg_eff(bat_cfg.get("charge_segments"))
        discharge_eff = _avg_eff(bat_cfg.get("discharge_segments"))

        # Reconstruct SOC trajectory from the schedule.
        # Sign convention: negative kw = charging (SOC increases),
        # positive kw = discharging (SOC decreases).
        soc = initial_soc
        soc_series: list[list] = [[_fmt_utc(origin), soc]]
        charge_series: list[list] = []

        for i, step in enumerate(schedule):
            ts = step.get("t", _fmt_utc(origin + timedelta(minutes=i * _STEP_MINUTES)))
            kw = step.get("devices", {}).get(bat_name, {}).get("kw", 0.0)
            # Charging: kw negative. SOC gain = (-kw) * eff * step_hours.
            # Discharging: kw positive. SOC loss = kw / eff * step_hours.
            if kw < 0:
                soc += (-kw) * charge_eff * _STEP_HOURS
            else:
                soc -= kw / max(discharge_eff, 1e-9) * _STEP_HOURS
            # Stamp the post-step SOC at the step's end time, not its
            # start time. ts is the step's start; adding one interval gives
            # the boundary where the SOC value is first observable.
            next_ts = _fmt_utc(_parse_utc(ts) + timedelta(minutes=_STEP_MINUTES))
            soc_series.append([next_ts, round(soc, 4)])
            # Expose charge as positive value; discharge as zero (shown separately).
            charge_kw = max(0.0, -kw)
            charge_series.append([ts, round(charge_kw, 4)])

        payload[f"battery__{bat_name}__soc_kwh"] = soc_series
        payload[f"battery__{bat_name}__charge_kw"] = charge_series

    return payload


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
    naive = out.get("naive_cost_eur") or 0.0
    optimised = out.get("optimised_cost_eur") or 0.0
    credit = out.get("soc_credit_eur") or 0.0
    effective = optimised - credit
    saving = naive - effective

    if naive != 0.0:
        saving_pct = round(saving / naive * 100.0, 1)
    else:
        saving_pct = 0.0

    schedule: list[dict[str, Any]] = out.get("schedule", [])
    grid_import_kwh = sum(
        s.get("grid_import_kw", 0.0) * _STEP_HOURS for s in schedule
    )
    grid_export_kwh = sum(
        s.get("grid_export_kw", 0.0) * _STEP_HOURS for s in schedule
    )

    # Self-sufficiency: fraction of total load met without importing.
    load_total_kwh = sum(
        max(0.0, d.get("kw", 0.0)) * _STEP_HOURS
        for s in schedule
        for d in s.get("devices", {}).values()
        if d.get("type") in ("static_load", "deferrable_load")
    )
    load_served_local = max(0.0, load_total_kwh - grid_import_kwh)
    self_suf_pct = (
        round(load_served_local / load_total_kwh * 100.0, 1)
        if load_total_kwh > 0.0
        else 0.0
    )

    return {
        "solve_time_utc": inp.get("triggered_at_utc") or inp.get("solve_time_utc", ""),
        "strategy": out.get("strategy", ""),
        "solve_status": out.get("solve_status", ""),
        "naive_cost_eur": round(naive, 4),
        "optimised_cost_eur": round(optimised, 4),
        "soc_credit_eur": round(credit, 4),
        "effective_cost_eur": round(effective, 4),
        "saving_eur": round(saving, 4),
        "saving_pct": saving_pct,
        "self_sufficiency_pct": self_suf_pct,
        "grid_import_kwh": round(grid_import_kwh, 4),
        "grid_export_kwh": round(grid_export_kwh, 4),
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


def _avg_eff(segments: list[dict[str, Any]] | None) -> float:
    """Return the average efficiency across segments, or 1.0 if none are given."""
    if not segments:
        return 1.0
    return sum(s.get("efficiency", 1.0) for s in segments) / len(segments)
