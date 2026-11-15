"""The solver's naive baseline for flexible loads, read from dump pairs.

The solver's ``naive_cost_eur`` runs flexible loads the way they would run
without mimirheim (``mimirheim.core.model_builder._naive_flexible_load_kw``):
a plugged-in charge-only EV charges at full power from the first step until it
holds the energy the plan gives it, and a deferrable load the solver scheduled
runs from the first step of its window. This module computes the same load per
step from a dump pair, for every report that shows the baseline.

A single plan is not the whole story for the household view's "today", which
stitches the quarter hours already past from the first step of each earlier
plan. Every plan's baseline starts its car and its scheduled loads afresh, so
stitching those would make the naive car charge whenever the real one did and
the naive dishwasher run whenever the real one ran. ``naive_overrides`` follows
one naive car per plug-in session and one naive run per deferrable cycle across
the earlier plans instead.

What this module does not do:
    - Read files, render anything, or import from ``mimirheim``.
    - Price the load. Callers add it to base load before the import/export
      split, as the solver does.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class Overrides:
    """Replacements for one plan's own naive flexible load.

    Attributes:
        ev_needed: Per charge-only EV, the cell energy in kWh the naive car
            still takes from the plan's first step, instead of the energy the
            plan itself gives the car.
        deferrable_kw: Per deferrable load, the naive draw in kW at each step
            of the plan, instead of the plan's own rule.
    """

    ev_needed: dict[str, float] = field(default_factory=dict)
    deferrable_kw: dict[str, list[float]] = field(default_factory=dict)


def ev_full_rate(charger: dict[str, Any], step_h: float) -> tuple[float, float]:
    """A charger at full power.

    Args:
        charger: The charger's config from the dump.
        step_h: Step length in hours.

    Returns:
        The AC draw in kW with every charge segment at ``power_max_kw``, and
        the cell energy in kWh that adds in one step at that power.
    """
    segments = charger.get("charge_segments") or []
    full_kw = sum(float(seg["power_max_kw"]) for seg in segments)
    per_step = sum(float(seg["power_max_kw"]) * float(seg["efficiency"]) for seg in segments) * step_h
    return full_kw, per_step


def naive_flexible_kw(
    inp: dict[str, Any],
    schedule: list[dict[str, Any]],
    step_h: float,
    starts: dict[str, Any],
    overrides: Overrides | None = None,
) -> list[float]:
    """AC power per step that flexible loads draw in the solver's naive baseline.

    Without ``overrides`` this is the solver's own rule for one plan:

    - A plugged-in charge-only EV charges at full power from the first step
      until its cell holds what the plan's last step holds.
    - A deferrable load with a recommended start (``starts``) runs its
      ``power_profile`` from the first step of its window.
    - Any other deferrable load draws what the plan draws.

    Args:
        inp: The input dump of the plan.
        schedule: The plan's steps, or a slice starting at its first step.
        step_h: Step length in hours.
        starts: The plan's ``deferrable_recommended_starts``.
        overrides: Replacements from ``naive_overrides``, or None.

    Returns:
        Power in kW per step, never negative.
    """
    over = overrides or Overrides()
    cfg = inp.get("config") or {}
    chargers = cfg.get("ev_chargers") or {}
    load = [0.0] * len(schedule)
    if not schedule:
        return load
    last = schedule[-1].get("devices") or {}
    for name, ev in (inp.get("ev_inputs") or {}).items():
        c = chargers.get(name)
        end = (last.get(name) or {}).get("soc_kwh")
        if not ev.get("available") or c is None or end is None or c.get("discharge_segments"):
            continue
        needed = over.ev_needed.get(name, float(end) - float(ev["soc_kwh"]))
        full_kw, per_step = ev_full_rate(c, step_h)
        for t in range(len(schedule)):
            # A remainder this small is solver rounding, not energy to deliver.
            if needed <= 1e-9 or per_step <= 0.0:
                break
            share = min(1.0, needed / per_step)
            load[t] += full_kw * share
            needed -= per_step * share

    loads = cfg.get("deferrable_loads") or {}
    windows = inp.get("deferrable_windows") or {}
    solve = parse_utc(inp.get("solve_time_utc"))
    for name in _deferrable_names(schedule):
        if name in over.deferrable_kw:
            for t, kw in enumerate(over.deferrable_kw[name][: len(load)]):
                load[t] += kw
            continue
        earliest = parse_utc((windows.get(name) or {}).get("earliest"))
        c = loads.get(name)
        if name in starts and c is not None and earliest is not None and solve is not None:
            first = _first_step(earliest, solve, step_h)
            for i, kw in enumerate(c.get("power_profile") or []):
                if first + i < len(load):
                    load[first + i] += float(kw)
            continue
        for t, kw in enumerate(_planned_draw(schedule, name)):
            load[t] += kw
    return load


def naive_overrides(
    history: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
    inp: dict[str, Any],
    out: dict[str, Any],
    step_h: float,
) -> tuple[list[Overrides], Overrides]:
    """Naive flexible loads that carry across a run of consecutive plans.

    Each EV: one naive car per plug-in session. From plug-in it charges at
    full power until it holds what the real car has taken so far plus what the
    latest plan still gives it. Unplugging ends the session. The charger
    config of each plan sets that plan's rate.

    Each deferrable load: one naive run per cycle. A cycle opens when a plan
    first schedules the load for a window, and its naive run starts at that
    window's first step, or at that plan's first step if the window opened
    earlier. A later plan for the same window does not move it. From the
    first cycle on, the load's draw in every plan is the naive run's, so a run
    that has started or ended no longer follows the plan.

    Args:
        history: Earlier plans in time order, as ``(step, inp, out)`` with
            ``step`` that plan's first step. Each is assumed to be what
            happened in its quarter hour.
        inp: The current plan's input dump.
        out: The current plan's output dump.
        step_h: Step length in hours.

    Returns:
        One ``Overrides`` per entry of ``history``, for that entry's first
        step alone, and one for the current plan's whole schedule.
    """
    plans = [(pinp, pout) for _, pinp, pout in history] + [(inp, out)]
    cycles = _deferrable_cycles(plans, step_h)
    real: dict[str, float] = {}
    naive: dict[str, float] = {}

    def owed(plan_inp: dict[str, Any], plan_out: dict[str, Any], name: str) -> float | None:
        ev = (plan_inp.get("ev_inputs") or {}).get(name)
        sched = plan_out.get("schedule") or []
        end = ((sched[-1].get("devices") or {}).get(name) or {}).get("soc_kwh") if sched else None
        if not ev or not ev.get("available") or end is None:
            real[name] = naive[name] = 0.0
            return None
        remaining = max(0.0, float(end) - float(ev["soc_kwh"]))
        return max(0.0, real.get(name, 0.0) + remaining - naive.get(name, 0.0))

    def deferrable(plan_inp: dict[str, Any], steps: list[dict[str, Any]]) -> dict[str, list[float]]:
        solve = parse_utc(plan_inp.get("solve_time_utc"))
        if solve is None:
            return {}
        times = [solve + timedelta(hours=step_h * t) for t in range(len(steps))]
        result = {}
        for name, runs in cycles.items():
            if name not in _deferrable_names(steps):
                continue
            planned = _planned_draw(steps, name)
            draw = []
            for t, when in enumerate(times):
                run = next((r for r in reversed(runs) if r[0] <= when), None)
                if run is None:
                    draw.append(planned[t])
                    continue
                offset = round((when - run[0]) / timedelta(hours=step_h))
                draw.append(run[1][offset] if offset < len(run[1]) else 0.0)
            result[name] = draw
        return result

    per_quarter = []
    for step, pinp, pout in history:
        chargers = (pinp.get("config") or {}).get("ev_chargers") or {}
        needed: dict[str, float] = {}
        for name, c in chargers.items():
            if c.get("discharge_segments"):
                continue
            due = owed(pinp, pout, name)
            if due is None:
                continue
            needed[name] = due
            naive[name] = naive.get(name, 0.0) + min(due, ev_full_rate(c, step_h)[1])
            before = float(pinp["ev_inputs"][name]["soc_kwh"])
            after = ((step.get("devices") or {}).get(name) or {}).get("soc_kwh")
            real[name] = real.get(name, 0.0) + max(0.0, _num(after, before) - before)
        per_quarter.append(Overrides(needed, deferrable(pinp, [step])))

    current = Overrides()
    for name, c in ((inp.get("config") or {}).get("ev_chargers") or {}).items():
        if c.get("discharge_segments"):
            continue
        due = owed(inp, out, name)
        if due is not None:
            current.ev_needed[name] = due
    current.deferrable_kw = deferrable(inp, out.get("schedule") or [])
    return per_quarter, current


def parse_utc(value: Any) -> datetime | None:
    """An ISO 8601 timestamp from a dump as an aware UTC datetime, or None."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _deferrable_cycles(
    plans: list[tuple[dict[str, Any], dict[str, Any]]],
    step_h: float,
) -> dict[str, list[tuple[datetime, list[float]]]]:
    """Each deferrable load's naive runs as ``(start, profile)`` in time order.

    A run opens when a plan schedules the load for a window not seen before.
    """
    step = timedelta(hours=step_h)
    cycles: dict[str, list[tuple[datetime, list[float]]]] = {}
    seen: dict[str, Any] = {}
    for pinp, pout in plans:
        solve = parse_utc(pinp.get("solve_time_utc"))
        loads = (pinp.get("config") or {}).get("deferrable_loads") or {}
        windows = pinp.get("deferrable_windows") or {}
        for name in pout.get("deferrable_recommended_starts") or {}:
            window = windows.get(name) or {}
            earliest = parse_utc(window.get("earliest"))
            profile = (loads.get(name) or {}).get("power_profile")
            if solve is None or earliest is None or not profile:
                continue
            if seen.get(name) == window.get("earliest"):
                continue
            seen[name] = window.get("earliest")
            start = solve + step * _first_step(earliest, solve, step_h)
            cycles.setdefault(name, []).append((start, [float(kw) for kw in profile]))
    return cycles


def _first_step(earliest: datetime, solve: datetime, step_h: float) -> int:
    """The step a window's earliest moment falls in, never before the first,
    with the solver's rule (``DeferrableLoad``'s ``_datetime_to_step``)."""
    return max(0, int((earliest - solve).total_seconds() / 3600.0 / step_h))


def _deferrable_names(schedule: list[dict[str, Any]]) -> set[str]:
    """Names of the deferrable loads that appear in ``schedule``."""
    return {
        name
        for step in schedule
        for name, sp in (step.get("devices") or {}).items()
        if (sp or {}).get("type") == "deferrable_load"
    }


def _planned_draw(schedule: list[dict[str, Any]], name: str) -> list[float]:
    """A deferrable load's draw per step in the plan, in kW (positive)."""
    return [
        max(0.0, -_num(((step.get("devices") or {}).get(name) or {}).get("kw")))
        for step in schedule
    ]


def _num(value: Any, default: float = 0.0) -> float:
    """A dump value as a float, or ``default`` when missing or unreadable."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
