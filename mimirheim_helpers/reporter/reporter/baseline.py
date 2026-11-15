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
one naive car per plug-in session and moves each committed deferrable run to
the start of its window, across the earlier plans, instead.

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
    latest plan still gives it. It keeps charging through a gap between dumps,
    for as long as the gap lasts. Unplugging ends the session. The charger
    config of each plan sets that plan's rate.

    Each deferrable load: each committed run is moved to the start of its
    window (see ``_deferrable_cycles``). Any other draw is drawn as planned.

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
    plans = [(pinp, pout, [step]) for step, pinp, pout in history]
    plans.append((inp, out, out.get("schedule") or []))
    deferrable = _deferrable_cycles(plans, step_h)
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

    times = [parse_utc(pinp.get("solve_time_utc")) for pinp, _, _ in plans]
    per_quarter = []
    for k, (step, pinp, pout) in enumerate(history):
        # Quarters until the next dump: the naive car charges through a gap.
        here, there = times[k], times[k + 1]
        gap = 1
        if here is not None and there is not None:
            gap = max(1, round((there - here) / timedelta(hours=step_h)))
        next_ev = (plans[k + 1][0].get("ev_inputs") or {})
        chargers = (pinp.get("config") or {}).get("ev_chargers") or {}
        needed: dict[str, float] = {}
        for name, c in chargers.items():
            if c.get("discharge_segments"):
                continue
            due = owed(pinp, pout, name)
            if due is None:
                continue
            needed[name] = due
            naive[name] = naive.get(name, 0.0) + min(due, ev_full_rate(c, step_h)[1] * gap)
            before = float(pinp["ev_inputs"][name]["soc_kwh"])
            after = _num(((step.get("devices") or {}).get(name) or {}).get("soc_kwh"), before)
            if gap > 1 and (next_ev.get(name) or {}).get("available"):
                # Over a gap only the next reading tells what the car took.
                after = _num(next_ev[name].get("soc_kwh"), after)
            real[name] = real.get(name, 0.0) + max(0.0, after - before)
        per_quarter.append(Overrides(needed, deferrable[k]))

    current = Overrides(deferrable_kw=deferrable[-1])
    for name, c in ((inp.get("config") or {}).get("ev_chargers") or {}).items():
        if c.get("discharge_segments"):
            continue
        due = owed(inp, out, name)
        if due is not None:
            current.ev_needed[name] = due
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
    plans: list[tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]],
    step_h: float,
) -> list[dict[str, list[float]]]:
    """Each deferrable load's naive draw for the steps of each plan.

    ``plans`` are ``(inp, out, steps)`` in time order, ``steps`` the steps of
    that plan that count: the first alone for an earlier plan, all of them for
    the current one.

    A run is anchored on its committed start ``S`` (``deferrable_start_times``
    in the input dump), rounded down to a step: the one fact the automation
    reports about what really ran. A start counts when its run overlaps the
    plans at hand; an older one is stale. The run's real draws, from ``S`` for
    the length of the profile, are replaced by a naive run of the same
    profile from the start of the window last seen before it, or from the
    first plan if that window opened earlier. A window is used for one run
    only. Without such a window the naive run starts at ``S`` itself: the
    baseline then draws the run as the plan does.

    Any other draw is drawn as planned. That includes a load whose automation
    never publishes a committed start: its saving shows only in the current
    plan, by the solver's own rule, until it runs.

    Returns:
        One dict per plan, mapping each deferrable load to its naive draw in
        kW per step. A load without a committed run in reach is left out, so
        the plan's own rule applies (``naive_flexible_kw``).
    """
    step = timedelta(hours=step_h)
    times = [parse_utc(pinp.get("solve_time_utc")) for pinp, _, _ in plans]
    known = [t for t in times if t is not None]
    if not known:
        return [{} for _ in plans]
    first = known[0]
    runs: dict[str, list[tuple[datetime, datetime, list[float]]]] = {}
    window: dict[str, datetime | None] = {}
    for k, (pinp, _, _) in enumerate(plans):
        loads = (pinp.get("config") or {}).get("deferrable_loads") or {}
        for name, w in (pinp.get("deferrable_windows") or {}).items():
            earliest = parse_utc((w or {}).get("earliest"))
            if earliest is not None and times[k] is not None:
                window[name] = earliest
        for name, value in (pinp.get("deferrable_start_times") or {}).items():
            started = parse_utc(value)
            profile = (loads.get(name) or {}).get("power_profile")
            if started is None or not profile:
                continue
            # Round down to a step: an automation that starts a few minutes
            # late still runs in the step the plan chose.
            real = first + step * ((started - first) // step)
            if real + step * len(profile) <= first:
                continue  # stale: the run ended before the first plan
            if any(r[0] == real for r in runs.get(name, [])):
                continue
            earliest = window.pop(name, None)
            if earliest is None:
                naive = real
            else:
                naive = min(real, max(first, first + step * ((earliest - first) // step)))
            runs.setdefault(name, []).append((real, naive, [float(kw) for kw in profile]))

    result: list[dict[str, list[float]]] = []
    for k, (_, _, steps) in enumerate(plans):
        draws: dict[str, list[float]] = {}
        if times[k] is None:
            result.append(draws)
            continue
        whens = [times[k] + step * i for i in range(len(steps))]
        for name in _deferrable_names(steps):
            mine = runs.get(name, [])
            touched = any(
                r[0] <= when < r[0] + step * len(r[2]) or r[1] <= when < r[1] + step * len(r[2])
                for r in mine
                for when in whens
            )
            if not touched:
                continue
            planned = _planned_draw(steps, name)
            draw = []
            for i, when in enumerate(whens):
                value = planned[i]
                for real, naive, profile in mine:
                    if real <= when < real + step * len(profile):
                        value -= profile[(when - real) // step]
                    if naive <= when < naive + step * len(profile):
                        value += profile[(when - naive) // step]
                draw.append(max(0.0, value))
            draws[name] = draw
        result.append(draws)
    return result


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
