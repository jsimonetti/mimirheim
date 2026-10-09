"""Household view renderer for mimirheim solve dumps.

A second, parallel report aimed at the people who live in the house rather than
the operator. Where ``render.build_report_html`` produces the technical report
(Plotly flows, per-device SOC, data table), this module produces one calm,
editorial page answering the layperson's question: *what is our house doing with
electricity today, and is it smart?* With a plan that reaches further, the same
for tomorrow and the day after, one tab each.

What this module does:
    - Produce a complete, self-contained HTML page from a dump pair, with the
      per-step flows and summaries embedded as a JSON data block and a small
      client script (hand-drawn SVG, no Plotly) that presents it, NL/EN.
    - Split the plan at local midnight into at most three days, each with its
      own summary (see ``_days``). Today also counts its earlier quarter
      hours, read from the first step of today's earlier dump pairs, when the
      caller passes them.
    - Expose ``build_household_html(inp, out, tz=None, earlier=()) -> str`` as
      the sole public function.

What this module does not do:
    - Read or write files, or publish MQTT.
    - Import from ``mimirheim``.
    - Infer anything the solver already decided. Every flow is read off the
      devices in the schedule; the page script only draws what it is given.
    - Invent its own economics. Energy totals come from ``metrics``; money per
      day is the solver's own per-step terms (grid cash flow, the naive
      baseline), and the days add up to the solver's totals.

Device types
------------
Every step's devices are read by ``type``. The schedule balances exactly
(sum of device ``kw`` + grid import - grid export = 0), so reading each device
keeps the arithmetic true by construction:

    ``pv``                  Generation ("sun").
    ``battery``             Storage. Positive ``kw`` is discharge to the house,
                            negative is charging.
    ``hybrid_inverter``     Storage with its own DC panels behind one AC output.
                            Its AC ``kw`` is split into sun and battery by
                            ``metrics.hybrid_splits``, from the planned SOC and
                            the configured efficiencies, the same split the
                            technical report uses. House flows use the AC side of
                            that split; sun generated and sun charging the
                            battery are DC at the MPPT input.
    ``ev_charger``          Consumption while charging (part of house usage,
                            never "the battery filling up"); a positive ``kw``
                            (vehicle-to-home) counts as battery.
    ``static_load``,
    ``deferrable_load``,
    ``thermal_boiler``,
    ``space_heating_hp``,
    ``combi_heat_pump``     Consumption (part of house usage).
    anything else           Not broken out: negative ``kw`` counts as house
                            usage, positive ``kw`` as an "other" source. The
                            page says so in its footer rather than folding it
                            into sun, battery or grid.

Within a step, house usage is met by sun first, then battery, then other
sources, then the grid; storage charging by leftover sun first, then the grid.
That allocation is a presentation choice; every quantity it allocates is read.

The visual design and client presentation logic were ported from a working
standalone prototype; here they are driven by the dump pair server-side.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, tzinfo
from typing import Any

from reporter.metrics import (
    HybridSplit,
    compute_economic_metrics,
    compute_schedule_metrics,
    hybrid_splits,
)

_PV_TYPES = frozenset({"pv"})
_BATTERY_TYPES = frozenset({"battery"})
_HYBRID_TYPES = frozenset({"hybrid_inverter"})
_EV_TYPES = frozenset({"ev_charger"})
_LOAD_TYPES = frozenset(
    {
        "static_load",
        "deferrable_load",
        "thermal_boiler",
        "space_heating_hp",
        "combi_heat_pump",
    }
)

# Below this a flow is treated as idle when labelling what the house is doing.
_MODE_THRESHOLD_KW = 0.03
# Below this a plan saving is "nothing worth mentioning".
_SAVING_THRESHOLD_EUR = 0.005
# The page offers today, tomorrow and the day after, one tab each.
_MAX_DAYS = 3


def build_household_html(
    inp: dict[str, Any],
    out: dict[str, Any],
    tz: tzinfo | None = None,
    earlier: Iterable[tuple[dict[str, Any], dict[str, Any]]] = (),
) -> str:
    """Build the household (layperson) HTML page from a mimirheim dump pair.

    Args:
        inp: Parsed SolveBundle JSON (the ``*_input.json`` dump).
        out: Parsed SolveResult JSON (the ``*_output.json`` dump).
        tz: Time zone whose midnights split the plan into days. Defaults to
            the reporter's own local time zone.
        earlier: Earlier dump pairs from today. The first step of each is what
            the house was doing at that quarter hour, which turns "today" into
            the whole day instead of only the rest of it. Any order; pairs
            from other days or overlapping this plan are ignored.

    Returns:
        A complete standalone HTML document string. Self-contained: no Plotly,
        no network calls. All data is embedded as a JSON block the page's own
        script reads and presents.
    """
    payload = _build_payload(inp, out, tz, earlier)
    data_json = json.dumps(payload, separators=(",", ":"))
    # The payload is numeric, ISO timestamps and fixed labels, so it cannot
    # contain "</script>"; escaping the forward slash is belt-and-braces.
    data_json = data_json.replace("</", "<\\/")
    return _PAGE.replace("__HOUSEHOLD_DATA__", data_json)


def _build_payload(
    inp: dict[str, Any],
    out: dict[str, Any],
    tz: tzinfo | None,
    earlier: Iterable[tuple[dict[str, Any], dict[str, Any]]],
) -> dict[str, Any]:
    """Read every step's devices into explicit per-step flows, a summary of the
    whole plan, and one summary per calendar day (see ``_days``)."""
    schedule = out.get("schedule", []) or []
    eco = compute_economic_metrics(out)
    m = compute_schedule_metrics(schedule, inp)
    step_h = _step_hours(schedule)
    splits = hybrid_splits(schedule, inp)

    keys = (
        "t", "price", "imp", "expo", "load", "sun",
        "sun_home", "batt_home", "other_home", "grid_home",
        "chg_sun", "chg_grid", "mode",
    )
    series: dict[str, list[Any]] = {k: [] for k in keys}
    has_storage = False
    other_kwh = 0.0

    for s, hybrids in zip(schedule, splits, strict=True):
        f = _step_flows(s, hybrids)
        has_storage = has_storage or f["has_storage"]
        other_kwh += (f["other_src"] + f["other_load"]) * step_h
        series["t"].append(s.get("t", ""))
        series["price"].append(_num(s.get("import_price_eur_per_kwh")))
        for k in keys[2:]:
            if k in f:
                series[k].append(round(f[k], 4) if isinstance(f[k], float) else f[k])

    solve = inp.get("triggered_at_utc") or inp.get("solve_time_utc") or (
        series["t"][0] if series["t"] else ""
    )
    summary = {
        "solve": solve,
        "naive": eco.naive_cost_eur,
        # Effective cost (after the SOC terminal credit) is the honest "with
        # plan" figure; saving = naive - effective, as in the economic metrics.
        "opt": eco.effective_cost_eur,
        "saving": round(max(0.0, eco.saving_eur), 4),
        "case": _cost_case(eco.naive_cost_eur, eco.effective_cost_eur, eco.saving_eur),
        "import": m.grid_import_kwh,
        "export": m.grid_export_kwh,
        "pv": m.pv_total_kwh,
        "load": m.load_total_kwh,
        "self": m.self_sufficiency_pct,
        "has_storage": has_storage,
        "other_kwh": round(other_kwh, 3),
    }
    days = _days(inp, schedule, splits, step_h, tz=tz, earlier=earlier)
    return {**series, "summary": summary, "days": days}


def _days(
    inp: dict[str, Any],
    schedule: list[dict[str, Any]],
    splits: list[dict[str, HybridSplit]],
    step_h: float,
    *,
    tz: tzinfo | None,
    earlier: Iterable[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Split the plan at local midnight into at most ``_MAX_DAYS`` days.

    Each day carries its step range ``[i0, i1)`` in the plan, its local date,
    whether the plan reaches that day's midnight (``complete``), the share of
    its steps priced from a prediction rather than a confirmed day-ahead
    price, the stored energy at its start, and a summary.

    The first day's summary also counts today's earlier quarter hours, each
    read from the first step of the plan made at that moment (``earlier``),
    so "today" covers the whole day. ``past_from`` is the first of those.

    Money per day:
        - cash: the plan's grid cash flow over the day's steps, exactly the
          per-step terms of the solver's ``optimised_cost_eur``.
        - ``naive``: the solver's no-storage baseline per step (base load minus
          clipped PV, at the import or export price), so the days add up to
          the solver's own ``naive_cost_eur``. Deliberately not scaled to it:
          a mirror that drifts from the core should show, not be hidden.
        - ``stored``: the value of the energy the day leaves in storage for the
          next one: each battery's and hybrid inverter's cell-side SOC change,
          through its discharge path, at the plan's average import price.
          Without it a day that charges for tomorrow would look like a loss.
          Negative when the day uses energy stored earlier.
        - ``opt`` is the effective cost, cash - stored, and ``saving`` =
          naive - effective. Unlike the whole-plan headline it is not clipped
          at zero, so the days add up: a day that mostly spends energy stored
          the day before can come out negative (``case`` says why, see
          ``_day_case``). ``cash`` is the day's grid cash flow on its own.
    """
    if not schedule:
        return []
    local = [_as_local(s.get("t", ""), tz) for s in schedule]
    starts = [0] + [i for i in range(1, len(local)) if local[i].date() != local[i - 1].date()]
    ranges = list(zip(starts, [*starts[1:], len(schedule)], strict=True))[:_MAX_DAYS]

    naive_steps = _naive_steps(inp, schedule, step_h)
    avg_price = sum(_num(s.get("import_price_eur_per_kwh")) for s in schedule) / len(schedule)
    discharge = _discharge_path(inp)
    capacity = _storage_capacity_kwh(inp)
    total_capacity = sum(capacity.values())
    confidence = inp.get("horizon_confidence") or []
    past = _earlier_today(earlier, local[0], tz)

    days = []
    for k, (i0, i1) in enumerate(ranges):
        tally = _tally(
            schedule[i0:i1], splits[i0:i1], naive_steps[i0:i1],
            _inp_at(inp, schedule, i0), step_h,
        )
        soc_start = _soc_before(inp, schedule, i0)
        soc_end = _soc_before(inp, schedule, i1)
        # What is still to come today: the plan's part alone, before the
        # earlier quarter hours are added in.
        rest = (
            tally["naive"] - tally["cash"]
            + _stored_eur(soc_start, soc_end, discharge, avg_price)
        )
        day_start = soc_start
        if k == 0 and past:
            for step, pinp in past:
                _add(tally, _tally(
                    [step], hybrid_splits([step], pinp),
                    _naive_steps(pinp, [step], step_h), pinp, step_h,
                ))
            day_start = _soc_before(past[0][1], [], 0)
        stored = _stored_eur(day_start, soc_end, discharge, avg_price)
        effective = tally["cash"] - stored
        saving = tally["naive"] - effective
        load, imp = tally["load"], tally["import"]
        predicted = sum(1 for c in confidence[i0:i1] if _num(c, 1.0) < 1.0)
        days.append({
            "i0": i0,
            "i1": i1,
            "date": local[i0].date().isoformat(),
            "complete": i1 < len(schedule) or _ends_at_midnight(local[i1 - 1], step_h),
            "predicted": round(predicted / (i1 - i0), 3),
            "soc_start_pct": (
                round(sum(soc_start.get(name, 0.0) for name in capacity) / total_capacity * 100.0)
                if total_capacity > 0 else None
            ),
            "past_from": (
                _as_local(past[0][0].get("t", ""), tz).strftime("%H:%M")
                if k == 0 and past else None
            ),
            "summary": {
                "naive": round(tally["naive"], 4),
                "opt": round(effective, 4),
                "saving": round(saving, 4),
                "cash": round(tally["cash"], 4),
                "stored": round(stored, 4),
                # Only on a day that includes earlier quarter hours.
                "rest_saving": round(rest, 4) if k == 0 and past else None,
                "case": _day_case(tally["naive"], effective, saving, stored),
                "import": round(imp, 4),
                "export": round(tally["export"], 4),
                "pv": round(tally["pv"], 4),
                "load": round(load, 4),
                # Same definition as metrics.compute_schedule_metrics.
                "self": round(max(0.0, load - imp) / load * 100.0, 1) if load > 0 else 0.0,
                "other_kwh": round(tally["other"], 3),
                "src": {key: round(tally[key], 3) for key in ("sun", "batt", "oth", "grid")},
            },
        })
    return days


def _tally(
    steps: list[dict[str, Any]],
    splits: list[dict[str, HybridSplit]],
    naive: list[float],
    inp: dict[str, Any],
    step_h: float,
) -> dict[str, float]:
    """Energy and money totals over consecutive steps of one plan.

    ``inp`` must carry the storage state just before the first step (see
    ``_inp_at``), so ``metrics`` splits the hybrid inverters correctly.
    """
    m = compute_schedule_metrics(steps, inp)
    flows = [_step_flows(s, h) for s, h in zip(steps, splits, strict=True)]
    return {
        "cash": sum(
            (_num(s.get("grid_import_kw")) * _num(s.get("import_price_eur_per_kwh"))
             - _num(s.get("grid_export_kw")) * _num(s.get("export_price_eur_per_kwh")))
            * step_h
            for s in steps
        ),
        "naive": sum(naive),
        "import": m.grid_import_kwh,
        "export": m.grid_export_kwh,
        "pv": m.pv_total_kwh,
        "load": m.load_total_kwh,
        "other": sum((f["other_src"] + f["other_load"]) * step_h for f in flows),
        "sun": sum(f["sun_home"] * step_h for f in flows),
        "batt": sum(f["batt_home"] * step_h for f in flows),
        "oth": sum(f["other_home"] * step_h for f in flows),
        "grid": sum(f["grid_home"] * step_h for f in flows),
    }


def _add(into: dict[str, float], more: dict[str, float]) -> None:
    """Add one tally to another, key by key."""
    for key, value in more.items():
        into[key] += value


def _earlier_today(
    earlier: Iterable[tuple[dict[str, Any], dict[str, Any]]],
    first: datetime,
    tz: tzinfo | None,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Today's quarter hours before this plan, as ``(step, inp)`` in time order.

    Each earlier plan contributes its first step: what the house was doing at
    the quarter hour it was made. When two plans share a first step, the later
    one wins, as it is the one that was acted on.
    """
    by_t: dict[datetime, tuple[str, dict[str, Any], dict[str, Any]]] = {}
    for pinp, pout in earlier:
        sched = pout.get("schedule") or []
        if not sched:
            continue
        t = _as_local(sched[0].get("t", ""), tz)
        if t >= first or t.date() != first.date():
            continue
        made = str(pinp.get("triggered_at_utc") or "")
        if t not in by_t or made > by_t[t][0]:
            by_t[t] = (made, sched[0], pinp)
    return [(step, pinp) for _, (_, step, pinp) in sorted(by_t.items())]


def _ends_at_midnight(last_step: datetime, step_h: float) -> bool:
    """True when a step starting at ``last_step`` runs to local midnight."""
    minutes = last_step.hour * 60 + last_step.minute + step_h * 60
    return minutes >= 24 * 60


def _as_local(t: str, tz: tzinfo | None) -> datetime:
    """Parse a schedule timestamp and convert it to ``tz`` (None = local)."""
    return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(tz)


def _naive_steps(
    inp: dict[str, Any],
    schedule: list[dict[str, Any]],
    step_h: float,
) -> list[float]:
    """The solver's no-storage baseline cost, step by step.

    Mirrors ``mimirheim.core.model_builder``'s ``_compute_naive_cost`` and the
    PV series ``build_and_solve`` hands it: base load minus each PV array's
    forecast clipped into ``[0, max_deliverable_kw]`` and each hybrid
    inverter's own panels clipped into ``[0, max_pv_kw]`` and taken through
    its inverter, bought at the import price when short and sold at the
    export price when over. With neither configured, the dump's summed
    ``pv_forecast`` is used as it stands, and so is it for an older dump with
    several arrays and no per-array series; one array without its own series
    takes the sum, clipped.
    """
    base = inp.get("base_load_forecast") or []
    summed = inp.get("pv_forecast") or []
    cfg = inp.get("config") or {}
    arrays = cfg.get("pv_arrays") or {}
    per_array = inp.get("pv_forecasts") or {}
    hybrid_inputs = inp.get("hybrid_inverter_inputs") or {}
    hybrids = cfg.get("hybrid_inverters") or {}
    # (forecast, ceiling, factor); a ceiling of None takes the forecast as it stands.
    series: list[tuple[list[Any], float | None, float]] = []
    if arrays and (per_array or len(arrays) == 1):
        series += [
            (per_array.get(name, summed if len(arrays) == 1 else []), _deliverable_kw(c), 1.0)
            for name, c in arrays.items()
        ]
    elif arrays or not hybrids:
        series.append((summed, None, 1.0))
    series += [
        (hybrid_inputs[name]["pv_forecast_kw"], float(c["max_pv_kw"]), float(c["inverter_efficiency"]))
        for name, c in hybrids.items()
    ]
    result = []
    for t, s in enumerate(schedule):
        pv = 0.0
        for forecast, ceiling, factor in series:
            kw = _num(forecast[t]) if t < len(forecast) else 0.0
            pv += kw if ceiling is None else min(max(0.0, kw), ceiling) * factor
        net = (_num(base[t]) if t < len(base) else 0.0) - pv
        price = s.get("import_price_eur_per_kwh") if net >= 0 else s.get("export_price_eur_per_kwh")
        result.append(net * _num(price) * step_h)
    return result


def _deliverable_kw(array: dict[str, Any]) -> float:
    """An array's AC ceiling: ``max_power_kw``, or its highest production
    stage when that is lower (``PvDevice.max_deliverable_kw``)."""
    peak = float(array["max_power_kw"])
    stages = array.get("production_stages")
    return min(peak, _num(stages[-1], peak)) if stages else peak


def _storage_capacity_kwh(inp: dict[str, Any]) -> dict[str, float]:
    """Usable capacity of each battery and hybrid inverter, from config: the
    house's own storage, which the page's battery level describes."""
    cfg = inp.get("config") or {}
    return {
        name: float(c["capacity_kwh"])
        for group in ("batteries", "hybrid_inverters")
        for name, c in (cfg.get(group) or {}).items()
    }


def _soc_before(
    inp: dict[str, Any],
    schedule: list[dict[str, Any]],
    i: int,
) -> dict[str, float]:
    """Cell-side SOC in kWh of every storage device just before step ``i``,
    keyed by device name: batteries, hybrid inverters, and EVs that are
    plugged in. Empty when there is none.

    A step's ``soc_kwh`` is the SOC at its end, so before step ``i`` is the end
    of step ``i - 1``; before step 0 it is the starting SOC from the inputs.
    An unplugged EV is left out, as the solver leaves it out of its credit:
    its planned SOC is not the vehicle's.
    """
    start = {
        name: float(v["soc_kwh"])
        for group in ("battery_inputs", "hybrid_inverter_inputs", "ev_inputs")
        for name, v in (inp.get(group) or {}).items()
        if group != "ev_inputs" or v.get("available")
    }
    if i == 0:
        return start
    devices = schedule[i - 1].get("devices") or {}
    return {
        name: _num((devices.get(name) or {}).get("soc_kwh"), kwh)
        for name, kwh in start.items()
    }


def _discharge_path(inp: dict[str, Any]) -> dict[str, float]:
    """Fraction of a cell-side kWh that reaches the AC side, per configured
    storage device, as ``mimirheim.core.model_builder``'s ``_compute_soc_credit``
    prices it.

    A hybrid inverter: its battery's discharge efficiency times the inverter.
    A battery: its discharge segments or efficiency curve, an EV charger its
    discharge segments, averaged as ``_avg_discharge_efficiency`` does.
    """
    cfg = inp.get("config") or {}
    path = {
        name: float(c["battery_discharge_efficiency"]) * float(c["inverter_efficiency"])
        for name, c in (cfg.get("hybrid_inverters") or {}).items()
    }
    for name, c in (cfg.get("batteries") or {}).items():
        path[name] = _avg_efficiency(
            c.get("discharge_segments"), c.get("discharge_efficiency_curve")
        )
    for name, c in (cfg.get("ev_chargers") or {}).items():
        path[name] = _avg_efficiency(c.get("discharge_segments"), None)
    return path


def _avg_efficiency(segments: list[Any] | None, curve: list[Any] | None) -> float:
    """``_avg_discharge_efficiency``: power-weighted over the segments (a plain
    mean when they carry no power), else the curve's mean, else 1."""
    if segments:
        total_kw = sum(float(seg["power_max_kw"]) for seg in segments)
        if total_kw == 0.0:
            return sum(float(seg["efficiency"]) for seg in segments) / len(segments)
        return sum(float(seg["efficiency"]) * float(seg["power_max_kw"]) for seg in segments) / total_kw
    if curve:
        return sum(float(bp["efficiency"]) for bp in curve) / len(curve)
    return 1.0


def _stored_eur(
    before: dict[str, float],
    after: dict[str, float],
    discharge: dict[str, float],
    price: float,
) -> float:
    """Value in EUR of the change in stored energy from ``before`` to ``after``:
    each configured device's SOC change, through its discharge path, at
    ``price``. A device without config is not valued, as in the solver."""
    return price * sum(
        (after.get(name, kwh) - kwh) * discharge[name]
        for name, kwh in before.items()
        if name in discharge
    )


def _inp_at(inp: dict[str, Any], schedule: list[dict[str, Any]], i: int) -> dict[str, Any]:
    """``inp`` with each hybrid's starting SOC moved to just before step ``i``,
    so ``metrics`` can split a day's slice of the schedule correctly."""
    if i == 0:
        return inp
    devices = schedule[i - 1].get("devices") or {}
    hybrids = {
        name: {**v, "soc_kwh": (devices.get(name) or {}).get("soc_kwh", v.get("soc_kwh"))}
        for name, v in (inp.get("hybrid_inverter_inputs") or {}).items()
    }
    return {**inp, "hybrid_inverter_inputs": hybrids}


def _step_flows(
    step: dict[str, Any],
    hybrids: dict[str, HybridSplit],
) -> dict[str, Any]:
    """Read one step's devices into AC-side flows (kW) and a mode label.

    ``hybrids`` is this step's split of each hybrid inverter, from
    ``metrics.hybrid_splits``.
    """
    sun = 0.0          # sun delivered on the AC side (pv + hybrid panels)
    sun_gen = 0.0      # all sun generated (hybrid sun as DC at the MPPT input)
    batt = 0.0         # storage delivering to the house
    other_src = 0.0    # unknown device types delivering power
    load = 0.0         # everything the house consumes, the car included
    other_load = 0.0   # the part of load from unknown device types
    ac_charge = 0.0    # storage drawing AC power to charge
    dc_solar = 0.0     # hybrid sun going straight into its cell (DC)
    has_storage = False

    for name, d in (step.get("devices") or {}).items():
        kind = d.get("type")
        kw = _num(d.get("kw"))
        if kind in _PV_TYPES:
            sun += max(0.0, kw)
            sun_gen += max(0.0, kw)
        elif kind in _BATTERY_TYPES:
            has_storage = True
            batt += max(0.0, kw)
            ac_charge += max(0.0, -kw)
        elif kind in _HYBRID_TYPES:
            has_storage = True
            h = hybrids[name]
            sun += h.pv_ac_kw
            sun_gen += h.pv_dc_kw
            batt += h.battery_ac_kw
            ac_charge += h.ac_charge_kw
            dc_solar += h.pv_to_cell_kw
        elif kind in _EV_TYPES:
            load += max(0.0, -kw)
            batt += max(0.0, kw)
        elif kind in _LOAD_TYPES:
            load += max(0.0, -kw)
        else:
            other_src += max(0.0, kw)
            other_load += max(0.0, -kw)
            load += max(0.0, -kw)

    imp = max(0.0, _num(step.get("grid_import_kw")))
    expo = max(0.0, _num(step.get("grid_export_kw")))

    sun_home = min(sun, load)
    batt_home = min(batt, load - sun_home)
    other_home = min(other_src, load - sun_home - batt_home)
    grid_home = max(0.0, load - sun_home - batt_home - other_home)
    sun_to_charge = min(max(0.0, sun - sun_home), ac_charge)
    chg_sun = sun_to_charge + dc_solar
    chg_grid = max(0.0, min(ac_charge - sun_to_charge, imp - grid_home))

    if batt_home > _MODE_THRESHOLD_KW:
        mode = "battery"
    elif chg_grid > _MODE_THRESHOLD_KW:
        mode = "grid_charge"
    elif chg_sun > _MODE_THRESHOLD_KW:
        mode = "solar_charge"
    elif load > 0.0 and grid_home <= _MODE_THRESHOLD_KW:
        mode = "sun"
    else:
        mode = "grid"

    return {
        "imp": imp,
        "expo": expo,
        "load": load,
        "sun": sun_gen,
        "sun_home": sun_home,
        "batt_home": batt_home,
        "other_home": other_home,
        "grid_home": grid_home,
        "chg_sun": chg_sun,
        "chg_grid": chg_grid,
        "mode": mode,
        "other_src": other_src,
        "other_load": other_load,
        "has_storage": has_storage,
    }


def _cost_case(naive: float, opt: float, saving: float) -> str:
    """Classify the day for wording: ``cost``, ``flip``, ``earn`` or ``none``.

    ``cost``: the house pays and the plan pays less. ``flip``: the plan turns a
    cost into income. ``earn``: the house earns money either way and the plan
    earns more. ``none``: the plan adds nothing worth mentioning.
    """
    if saving <= _SAVING_THRESHOLD_EUR:
        return "none"
    if naive > 0 and opt >= 0:
        return "cost"
    if naive > 0:
        return "flip"
    return "earn"


def _day_case(naive: float, opt: float, saving: float, stored: float) -> str:
    """``_cost_case`` for one day, plus the two ways a day's saving can be
    negative: ``carry`` when the day spends energy stored before it (the cost
    was paid then), ``extra`` when the plan simply costs more that day.
    """
    if saving < -_SAVING_THRESHOLD_EUR:
        return "carry" if stored < -_SAVING_THRESHOLD_EUR else "extra"
    return _cost_case(naive, opt, saving)


def _step_hours(schedule: list[dict[str, Any]]) -> float:
    """Step length in hours from the schedule's own timestamps (default 15 min)."""
    if len(schedule) >= 2:
        try:
            a = datetime.fromisoformat(schedule[0]["t"].replace("Z", "+00:00"))
            b = datetime.fromisoformat(schedule[1]["t"].replace("Z", "+00:00"))
            hours = (b - a).total_seconds() / 3600.0
            if hours > 0:
                return hours
        except (KeyError, ValueError, AttributeError):
            pass
    return 0.25


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a dump value to float, treating missing/None as ``default``."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default



# ---------------------------------------------------------------------------
# The page. Self-contained: CSS + body + a JSON data block + a presenter script.
# __HOUSEHOLD_DATA__ is replaced with the embedded payload at render time.
# ---------------------------------------------------------------------------
_PAGE = r"""<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<!-- Reload periodically so a left-open tablet / embedded frame tracks new solves. -->
<meta http-equiv="refresh" content="300">
<title>Ons huis vandaag — energieplan</title>
<style>
:root{
  --paper:#F5EFE3; --card:#FFFBF3; --ink:#1E2733; --soft:#5E6673; --line:#E4DACA;
  --sun:#F0A92E; --sun2:#FFD27A; --batt:#2E8C80; --batt2:#9AD3C9; --grid:#7D879A; --grid2:#C9CFDA;
  --cheap:#6E9E5C; --mid:#E2A93B; --dear:#D25F3A;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;
  --sans:"Avenir Next",Avenir,"Segoe UI","Helvetica Neue",sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);-webkit-font-smoothing:antialiased}
body{background:
  radial-gradient(1200px 500px at 85% -10%, rgba(240,169,46,.22), transparent 60%),
  radial-gradient(900px 500px at -10% 30%, rgba(46,140,128,.10), transparent 60%),
  var(--paper);}
.wrap{max-width:1120px;margin:0 auto;padding:28px 24px 64px}
header{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.kicker{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--soft);font-weight:600}
h1{font-family:var(--serif);font-weight:400;font-size:clamp(34px,5vw,56px);line-height:1.04;margin:8px 0 0;letter-spacing:-.01em}
h1 em{font-style:italic;color:var(--batt)}
h2{font-family:var(--serif);font-weight:400;font-size:28px;margin:0 0 4px}
.sub{color:var(--soft);font-size:15px;margin:0 0 18px;line-height:1.5}
.lang{display:flex;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:4px;flex-shrink:0}
.lang button{border:0;background:transparent;font:600 13px var(--sans);padding:7px 14px;border-radius:999px;cursor:pointer;color:var(--soft)}
.lang button.on{background:var(--ink);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:22px;padding:26px;box-shadow:0 1px 0 rgba(0,0,0,.02),0 12px 30px -18px rgba(60,40,10,.25)}
.hero{display:grid;grid-template-columns:1.25fr 1fr;gap:20px;margin-top:26px}
.save{position:relative;overflow:hidden;background:linear-gradient(135deg,#21463F 0%,#2E8C80 100%);color:#F6FFFC;border:0}
.save .big{font-family:var(--serif);font-size:clamp(64px,9vw,104px);line-height:1;margin:14px 0 6px;letter-spacing:-.02em}
.save .big small{font-size:.45em;opacity:.8;margin-right:4px}
.save p{margin:0;font-size:17px;line-height:1.5;max-width:30em;opacity:.95}
.save .kicker{color:#BFE7E0}
.save svg.deco{position:absolute;right:-40px;bottom:-40px;opacity:.13}
.compare{margin-top:22px;display:grid;gap:10px}
.cbar{display:grid;grid-template-columns:150px 1fr 130px;align-items:center;gap:12px;font-size:14px}
.cbar .track{height:14px;background:rgba(255,255,255,.12);border-radius:8px;overflow:hidden}
.cbar .fill{height:100%;border-radius:8px;width:0;transition:width 1.2s cubic-bezier(.2,.7,.2,1)}
.cbar .v{white-space:nowrap;text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.now{display:flex;flex-direction:column;gap:14px}
.now .state{display:flex;gap:16px;align-items:center}
.icon{width:58px;height:58px;border-radius:18px;display:grid;place-items:center;flex-shrink:0}
.now .what{font-family:var(--serif);font-size:25px;line-height:1.2}
.now .why{color:var(--soft);font-size:15px;line-height:1.55}
.next{border-top:1px dashed var(--line);padding-top:14px;display:flex;gap:12px;align-items:center;font-size:15px}
.next .icon{width:38px;height:38px;border-radius:12px}
.next b{font-weight:600}
section{margin-top:22px}
.tl{position:relative;margin-top:8px}
.tl-bar{display:flex;height:64px;border-radius:16px;overflow:hidden;border:1px solid var(--line)}
.tl-seg{position:relative;display:flex;align-items:center;justify-content:center;cursor:pointer;transition:filter .2s,transform .2s;transform-origin:bottom}
.tl-seg:hover,.tl-seg.sel{filter:brightness(1.06) saturate(1.1)}
.tl-seg svg{opacity:.95}
.tl-ticks{position:relative;height:22px;margin-top:6px;font-size:12px;color:var(--soft);font-variant-numeric:tabular-nums}
.tl-ticks span{position:absolute;transform:translateX(-50%)}
.legend{display:flex;flex-wrap:wrap;gap:16px;margin-top:10px;font-size:14px;color:var(--soft)}
.legend i{display:inline-block;width:12px;height:12px;border-radius:4px;margin-right:7px;vertical-align:-1px}
.steps{list-style:none;margin:22px 0 0;padding:0;display:grid;gap:2px}
.steps li{display:grid;grid-template-columns:110px 46px 1fr auto;align-items:center;gap:16px;padding:12px 14px;border-radius:14px;cursor:pointer;transition:background .2s}
.steps li:hover,.steps li.sel{background:#F3EBDD}
.steps .time{font-variant-numeric:tabular-nums;font-weight:600;font-size:15px}
.steps .icon{width:42px;height:42px;border-radius:13px}
.steps .t1{font-size:16px;font-weight:600}
.steps .t2{font-size:14px;color:var(--soft);margin-top:2px;line-height:1.45}
.pill{font-size:13px;font-weight:600;padding:5px 11px;border-radius:999px;white-space:nowrap;font-variant-numeric:tabular-nums}
.two{display:grid;grid-template-columns:1.5fr 1fr;gap:20px}
.chart{position:relative;width:100%}
.chart svg{display:block;width:100%;overflow:visible}
.tip{position:absolute;pointer-events:none;background:var(--ink);color:#fff;font-size:13px;padding:8px 11px;border-radius:10px;line-height:1.45;white-space:nowrap;opacity:0;transition:opacity .15s;transform:translate(-50%,-110%);z-index:5}
.tip b{font-size:15px}
.tips{display:grid;gap:12px;margin-top:6px}
.tipcard{display:flex;gap:14px;align-items:flex-start;padding:14px;border-radius:16px;background:#F6EFE2}
.tipcard .icon{width:44px;height:44px;border-radius:13px}
.tipcard b{display:block;font-size:16px;margin-bottom:2px}
.tipcard span{font-size:14px;color:var(--soft);line-height:1.5}
.srcbar{display:flex;height:46px;border-radius:14px;overflow:hidden;margin:18px 0 14px}
.srcbar div{display:flex;align-items:center;justify-content:center;color:#fff;font-weight:600;font-size:14px;white-space:nowrap;overflow:hidden}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.stat{padding:16px;border-radius:16px;background:#F6EFE2}
.stat .n{font-family:var(--serif);font-size:32px;line-height:1.1}
.stat .n small{font-family:var(--sans);font-size:14px;color:var(--soft);margin-left:3px}
.stat .l{font-size:13px;color:var(--soft);margin-top:4px;line-height:1.4}
footer{margin-top:30px;color:var(--soft);font-size:12.5px;line-height:1.6;text-align:center}
.days{display:flex;gap:6px;background:var(--card);border:1px solid var(--line);border-radius:999px;padding:5px;margin-top:22px;width:max-content;max-width:100%}
.days button{border:0;background:transparent;font:600 15px var(--sans);padding:8px 20px;border-radius:999px;cursor:pointer;color:var(--soft);display:flex;flex-direction:column;align-items:center;line-height:1.2}
.days button small{font-weight:500;font-size:11.5px;opacity:.85}
.days button.on{background:var(--ink);color:#fff}
.days button:disabled{opacity:.38;cursor:default}
.badge{display:inline-block;margin-top:12px;font-size:13px;font-weight:600;padding:6px 12px;border-radius:999px;background:#FDEFD2;color:#9A6512}
.save .stored{margin-top:14px;font-size:14px;opacity:.85}
.wait{padding:70px 20px;text-align:center;color:var(--soft);font-family:var(--serif);font-size:24px}
@media (max-width:860px){.hero,.two{grid-template-columns:1fr}.stats{grid-template-columns:repeat(2,1fr)}.steps li{grid-template-columns:86px 42px 1fr}.steps .pill{display:none}.cbar{grid-template-columns:110px 1fr 110px}}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div><div class="kicker" id="dateline"></div><h1 id="title"></h1><div id="badge"></div></div>
    <div class="lang"><button data-l="nl" class="on">NL</button><button data-l="en">EN</button></div>
  </header>
  <nav class="days" id="days"></nav>
  <div class="hero">
    <div class="card save">
      <svg class="deco" width="320" height="320" viewBox="0 0 100 100"><circle cx="50" cy="50" r="20" fill="#fff"/><g stroke="#fff" stroke-width="5" stroke-linecap="round"><path d="M50 8v12M50 80v12M8 50h12M80 50h12M20 20l8 8M72 72l8 8M80 20l-8 8M28 72l-8 8"/></g></svg>
      <div class="kicker" id="saveK"></div><div class="big" id="saveBig"></div><p id="saveP"></p>
      <div class="compare">
        <div class="cbar"><span id="cb1"></span><div class="track"><div class="fill" id="f1" style="background:#F2B9A3"></div></div><span class="v" id="v1"></span></div>
        <div class="cbar"><span id="cb2"></span><div class="track"><div class="fill" id="f2" style="background:#FFE3A3"></div></div><span class="v" id="v2"></span></div>
      </div>
      <p class="stored" id="stored"></p>
    </div>
    <div class="card now">
      <div class="kicker" id="nowK"></div>
      <div class="state"><div class="icon" id="nowIcon"></div><div class="what" id="nowWhat"></div></div>
      <div class="why" id="nowWhy"></div>
      <div class="next" id="nextRow"><div class="icon" id="nextIcon"></div><div id="nextTxt"></div></div>
      <div class="next" id="socRow"><div class="icon" id="socIcon"></div><div id="socTxt"></div></div>
    </div>
  </div>
  <section class="card"><h2 id="planH"></h2><p class="sub" id="planS"></p>
    <div class="tl"><div class="tl-bar" id="tlbar"></div><div class="tl-ticks" id="tlticks"></div></div>
    <div class="legend" id="legend"></div><ul class="steps" id="steps"></ul></section>
  <div class="two">
    <section class="card"><h2 id="priceH"></h2><p class="sub" id="priceS"></p><div class="chart" id="priceChart"><div class="tip"></div></div></section>
    <section class="card"><h2 id="tipsH"></h2><p class="sub" id="tipsS"></p><div class="tips" id="tips"></div></section>
  </div>
  <section class="card"><h2 id="flowH"></h2><p class="sub" id="flowS"></p><div class="chart" id="flowChart"><div class="tip"></div></div><div class="legend" id="flowLegend"></div></section>
  <section class="card"><h2 id="srcH"></h2><p class="sub" id="srcS"></p><div class="srcbar" id="srcbar"></div><div class="stats" id="stats"></div></section>
  <footer id="foot"></footer>
</div>

<script id="household-data" type="application/json">__HOUSEHOLD_DATA__</script>
<script>
const ALL = JSON.parse(document.getElementById('household-data').textContent);
const TZ = (Intl.DateTimeFormat().resolvedOptions().timeZone) || 'UTC';
// Language and day survive the 5-minute reload. Storage can be blocked inside a
// sandboxed frame, so it is optional.
const store={get:k=>{try{return localStorage.getItem('mimirheim-household-'+k);}catch(e){return null;}},
  set:(k,v)=>{try{localStorage.setItem('mimirheim-household-'+k,v);}catch(e){}}};
let L = store.get('lang')==='en'?'en':'nl';
// Step duration straight from the data (fallback 15 min for a 0/1-step plan).
const STEP_MS = ALL.t.length>1 ? (new Date(ALL.t[1])-new Date(ALL.t[0])) : 900000;
// A later day needs at least this much plan to be worth a tab of its own.
const MIN_DAY_STEPS = Math.round(3*3600000/STEP_MS);
let DAY = 0, D = ALL;

const IC = {
  sun:  c=>`<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4.2" fill="${c}"/><path d="M12 2.5v2.2M12 19.3v2.2M2.5 12h2.2M19.3 12h2.2M5.3 5.3l1.6 1.6M17.1 17.1l1.6 1.6M18.7 5.3l-1.6 1.6M6.9 17.1l-1.6 1.6"/></svg>`,
  batt: c=>`<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"><rect x="3" y="7" width="16" height="10" rx="2.5"/><path d="M21 10.5v3" stroke-linecap="round"/><rect x="5.5" y="9.5" width="7" height="5" rx="1" fill="${c}" stroke="none"/></svg>`,
  charge:c=>`<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2" stroke-linejoin="round"><rect x="3" y="7" width="16" height="10" rx="2.5"/><path d="M21 10.5v3" stroke-linecap="round"/><path d="M12 8.5l-3 4h3l-1.5 3.5 4-4.6h-3l1.5-2.9z" fill="${c}" stroke="none"/></svg>`,
  grid: c=>`<svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2.5L7 21.5M12 2.5l5 19M8.6 15.5h6.8M9.7 10.5h4.6M5 6h14M8 6l4 4.5L16 6"/></svg>`,
  wash: c=>`<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2"><rect x="4" y="3" width="16" height="18" rx="3"/><circle cx="12" cy="13" r="4.5"/><path d="M7.5 6.5h2" stroke-linecap="round"/></svg>`,
  coin: c=>`<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="${c}" stroke-width="2"><circle cx="12" cy="12" r="8.5"/><path d="M14.8 9a3.5 3.5 0 100 6M8 11h5M8 13.2h5" stroke-linecap="round"/></svg>`
};
const MODE = {
  battery:{col:'#2E8C80',bg:'#DDF1EC',ic:'batt'}, grid_charge:{col:'#3E6FB0',bg:'#DEE8F6',ic:'charge'},
  solar_charge:{col:'#E39A1F',bg:'#FDEFD2',ic:'charge'}, sun:{col:'#F0A92E',bg:'#FDEFD2',ic:'sun'}, grid:{col:'#8A93A5',bg:'#E8EBF0',ic:'grid'}
};
const cap = w=>w[0].toUpperCase()+w.slice(1);
const T = {
 nl:{tabs:['Vandaag','Morgen','Overmorgen'],dw:['vandaag','morgen','overmorgen'],
  title:(dw,fut)=>fut?`Wat gaat ons huis <em>${dw}</em> doen met stroom?`:'Wat doet ons huis <em>vandaag</em> met stroom?',
  badge:'Verwachting, kan nog veranderen',pPred:'prijzen voorspeld',pPart:'prijzen deels voorspeld',until:t=>`tot ${t}`,
  saveK:(dw,fut,b)=>`${fut?'Verwachte besparing':'Besparing'} ${dw} dankzij ${b?'de thuisbatterij':'het slimme plan'}`,
  gainK:(dw,fut,b)=>`${fut?'Verwacht extra':'Extra'} opgeleverd ${dw} dankzij ${b?'de thuisbatterij':'het slimme plan'}`,
  how:b=>b?'slim te laden als stroom goedkoop is en de batterij te gebruiken als stroom duur is':'apparaten op de goedkoopste momenten te laten draaien',
  saveP:(p,h,dw)=>`Door ${h}, betalen we ${dw} zo'n <b>${p}%</b> minder.`,
  saveFlip:(h,dw)=>`Door ${h}, verdienen we ${dw} geld in plaats van dat we betalen.`,
  saveEarn:(e,h,dw)=>`Onze zonnepanelen leveren ${dw} geld op. Door ${h} verdienen we extra: in totaal <b>€ ${e}</b>.`,
  saveNone:dw=>`${cap(dw)} levert slim plannen niets extra op. We houden het simpel.`,
  rest:(e,end)=>`Daarvan nog € ${e} van nu tot ${end||'middernacht'}.`,
  cashK:(dw,fut)=>`${fut?'Verwacht bespaard':'Bespaard'} op wat we ${dw} kopen`,
  carryP:(c,u,dw)=>`${cap(dw)} gebruiken we vooral stroom die eerder al in de batterij zat. Wat we van het net kopen is € ${c} goedkoper dan zonder slim plan; de € ${u} aan stroom uit de batterij was eerder al betaald.`,
  carry0P:(u,dw)=>`${cap(dw)} gebruiken we vooral stroom die eerder al in de batterij zat, voor € ${u}. Die was eerder al betaald.`,
  extraK:(dw,fut)=>`${fut?'Verwachte extra kosten':'Extra kosten'} ${dw}`,
  extraP:dw=>`${cap(dw)} kost het slimme plan iets meer dan zonder. Het plan kijkt naar meerdere dagen tegelijk, niet naar één dag.`,
  usedSt:e=>`Inclusief € ${e} aan stroom uit de batterij, gerekend tegen de gemiddelde prijs.`,
  keptSt:e=>`Er gaat voor € ${e} aan stroom de batterij in voor later. Dat telt hier niet als kosten.`,
  pay:'betalen',earn:'verdienen',
  cb1:'Zonder slim plan',cb2:'Met slim plan',nowK:t=>`Nu, om ${t}`,startK:dw=>`Zo begint ${dw}`,nextPre:'Daarna',nextAt:'vanaf',
  socNow:p=>`De batterij is nu <b>${p}%</b> vol.`,socStart:p=>`De batterij begint de dag <b>${p}%</b> vol.`,
  planH:(dw,fut,end)=>(fut?`Het plan voor ${dw}`:'Het plan voor de rest van de dag')+(end?`, tot ${end}`:''),
  planS:'Elke kleur is een stukje van de dag. Klik op een blok of een regel om te zien wat er gebeurt en waarom.',
  m:{battery:{a:'Huis draait op de batterij',b:c=>`Stroom is dan duur (rond ${c} cent). We gebruiken wat we eerder hebben opgeslagen.`},
   grid_charge:{a:'Batterij tankt goedkope stroom',b:c=>`Een goedkoop moment (rond ${c} cent). We vullen de batterij voor de dure uren.`},
   solar_charge:{a:'De zon vult de batterij',b:()=>`De panelen leveren meer dan het huis nodig heeft. Het overschot gaat de batterij in.`},
   sun:{a:'Huis draait op de zon',b:()=>`De zonnepanelen leveren genoeg voor het hele huis.`},
   grid:{a:'Gewoon stroom van het net',b:c=>`Stroom is dan redelijk goedkoop (rond ${c} cent). We sparen de batterij voor later.`}},
  priceH:dw=>`Wat kost stroom ${dw}?`,priceS:'Prijs per kWh, per kwartier. Groen is goedkoop, rood is duur.',
  cheapest:'goedkoopst',dearest:'duurst',cent:'cent',tipsH:'Handig om te weten',tipsS:'Zelf iets aanzetten? Dan ben je hier het voordeligst uit.',
  tWash:(a,b,c,fut)=>[`Was of vaatwasser: ${a} - ${b}`,`De goedkoopste twee uur ${fut?'van de dag':'van nu af'}, gemiddeld ${c} cent per kWh.`],
  tAvoid:(a,b,c)=>[`Liever niet: ${a} - ${b}`,`De duurste uren, tot ${c} cent per kWh. De batterij vangt dit zoveel mogelijk op.`],
  tSun:(a,b)=>[`Meeste zon: ${a} - ${b}`,`Dan maken de panelen het meeste stroom.`],
  flowH:'Waar komt onze stroom vandaan, per uur?',flowS:'Boven de lijn: wat het huis gebruikt en waar dat vandaan komt. Onder de lijn: stroom die de batterij in gaat.',
  lg:{sun:'Zon',batt:'Batterij',grid:'Net',oth:'Overig',cs:'Batterij laden (zon)',cg:'Batterij laden (net)'},house:'Huisverbruik',
  srcH:'De dag in cijfers',srcS:(fut,end,from)=>from?`De hele dag vanaf ${from}: tot nu zoals het plan het op dat moment zag, daarna het plan${end?` tot ${end}`:''}.`:end?`Opgeteld ${fut?'':'van nu '}tot ${end}, daar stopt het plan nog.`:(fut?'Opgeteld over de hele dag.':'Opgeteld van nu tot middernacht.'),
  st:{use:'stroom die het huis gebruikt',sun:'opgewekt door de zonnepanelen',imp:'gekocht van het net',self:'van ons verbruik hoefden we niet te kopen'},
  foot:(t,h)=>`Plan berekend om ${t} voor de komende ${h} uur. Bedragen zijn verwachtingen, de werkelijkheid kan iets afwijken.<br>Alle cijfers komen rechtstreeks uit het plan van Mimirheim.`,
  other:k=>`Inclusief ${k} kWh van apparaten die deze pagina niet apart toont.`,
  dateFmt:'nl-NL',wait:'Nog geen plan van Mimirheim.'},
 en:{tabs:['Today','Tomorrow','Day after'],dw:['today','tomorrow','the day after'],
  title:(dw,fut)=>fut?`What will our house do with power <em>${dw}</em>?`:'What is our house doing with <em>power</em> today?',
  badge:'Forecast, may still change',pPred:'prices predicted',pPart:'prices partly predicted',until:t=>`until ${t}`,
  saveK:(dw,fut,b)=>`${fut?'Expected saving':'Saved'} ${dw} thanks to ${b?'the home battery':'the smart plan'}`,
  gainK:(dw,fut,b)=>`${fut?'Expected gain':'Gained'} ${dw} thanks to ${b?'the home battery':'the smart plan'}`,
  how:b=>b?'charging when power is cheap and using the battery when it is expensive':'running appliances at the cheapest moments',
  saveP:(p,h,dw)=>`By ${h}, we pay about <b>${p}%</b> less ${dw}.`,
  saveFlip:(h,dw)=>`By ${h}, we earn money ${dw} instead of paying.`,
  saveEarn:(e,h,dw)=>`Our solar panels earn money ${dw}. By ${h}, we earn extra: <b>€ ${e}</b> in total.`,
  saveNone:dw=>`Smart planning does not add anything ${dw}, so we keep it simple.`,
  rest:(e,end)=>`Of that, € ${e} is still to come between now and ${end||'midnight'}.`,
  cashK:(dw,fut)=>`${fut?'Expected saving':'Saved'} on what we buy ${dw}`,
  carryP:(c,u,dw)=>`${cap(dw)}, we mostly use power that was already in the battery. What we buy from the grid costs € ${c} less than without the smart plan; the € ${u} of power from the battery was paid for earlier.`,
  carry0P:(u,dw)=>`${cap(dw)}, we mostly use power that was already in the battery, worth € ${u}. It was paid for earlier.`,
  extraK:(dw,fut)=>`${fut?'Expected extra cost':'Extra cost'} ${dw}`,
  extraP:dw=>`${cap(dw)}, the smart plan costs a little more than without it. The plan looks at several days at once, not at one day.`,
  usedSt:e=>`Includes € ${e} of power from the battery, valued at the average price.`,
  keptSt:e=>`€ ${e} of power goes into the battery for later. That does not count as a cost here.`,
  pay:'pay',earn:'earn',
  cb1:'Without smart plan',cb2:'With smart plan',nowK:t=>`Right now, at ${t}`,startK:dw=>`How ${dw} starts`,nextPre:'Next',nextAt:'from',
  socNow:p=>`The battery is <b>${p}%</b> full now.`,socStart:p=>`The battery starts the day <b>${p}%</b> full.`,
  planH:(dw,fut,end)=>(fut?`The plan for ${dw}`:'The plan for the rest of the day')+(end?`, until ${end}`:''),
  planS:'Each colour is a part of the day. Click a block or a row to see what happens and why.',
  m:{battery:{a:'House runs on the battery',b:c=>`Power is expensive then (around ${c} cents). We use what we stored earlier.`},
   grid_charge:{a:'Battery fills up with cheap power',b:c=>`A cheap moment (around ${c} cents). We top up the battery for the expensive hours.`},
   solar_charge:{a:'The sun fills the battery',b:()=>`The panels make more than the house needs. The extra goes into the battery.`},
   sun:{a:'House runs on sunshine',b:()=>`The solar panels make enough for the whole house.`},
   grid:{a:'Normal power from the grid',b:c=>`Power is fairly cheap then (around ${c} cents). We save the battery for later.`}},
  priceH:dw=>`What does power cost ${dw}?`,priceS:'Price per kWh, per quarter hour. Green is cheap, red is expensive.',
  cheapest:'cheapest',dearest:'most expensive',cent:'cents',tipsH:'Good to know',tipsS:'Switching something on yourself? These are the best times.',
  tWash:(a,b,c,fut)=>[`Laundry or dishwasher: ${a} - ${b}`,`The cheapest two hours ${fut?'of the day':'from now'}, on average ${c} cents per kWh.`],
  tAvoid:(a,b,c)=>[`Better avoid: ${a} - ${b}`,`The most expensive hours, up to ${c} cents per kWh. The battery covers as much as it can.`],
  tSun:(a,b)=>[`Most sunshine: ${a} - ${b}`,`When the panels make the most power.`],
  flowH:'Where does our power come from, hour by hour?',flowS:'Above the line: what the house uses and where it comes from. Below the line: power going into the battery.',
  lg:{sun:'Sun',batt:'Battery',grid:'Grid',oth:'Other',cs:'Battery charging (sun)',cg:'Battery charging (grid)'},house:'House usage',
  srcH:'The day in numbers',srcS:(fut,end,from)=>from?`The whole day from ${from}: until now as the plan saw it at the time, then the plan${end?` until ${end}`:''}.`:end?`Added up ${fut?'':'from now '}until ${end}, where the plan stops for now.`:(fut?'Added up over the whole day.':'Added up from now until midnight.'),
  st:{use:'power used by the house',sun:'made by the solar panels',imp:'bought from the grid',self:'of our usage we did not have to buy'},
  foot:(t,h)=>`Plan calculated at ${t} for the next ${h} hours. Amounts are forecasts, reality may differ a little.<br>All numbers come straight from Mimirheim's plan.`,
  other:k=>`Includes ${k} kWh from devices this page does not show separately.`,
  dateFmt:'en-GB',wait:'No plan from Mimirheim yet.'}
};

// Today, tomorrow, the day after, as far as the plan reaches. Today can always
// be opened; a later day only with enough plan to be worth a tab of its own.
const DAYS = ALL.days;
const usable = k=>k===0||(k<DAYS.length&&DAYS[k].i1-DAYS[k].i0>=MIN_DAY_STEPS);
// Slice the per-step series down to one day; the day's own summary replaces
// the whole-plan one (has_storage is a property of the house, not the day).
function view(k){
  const d=DAYS[k];
  const v={summary:{...d.summary,has_storage:ALL.summary.has_storage,solve:ALL.summary.solve}};
  Object.keys(ALL).forEach(key=>{if(Array.isArray(ALL[key])&&key!=='days')v[key]=ALL[key].slice(d.i0,d.i1);});
  return v;
}
let N = 0;
let steps=[], blocks=[], endTime=null, solveT=null, pmin=0, pmax=0, wash=null, avoid=null, sunBest=null;
function prep(){
  // Every flow and mode comes from the server; this script only presents them.
  D=view(DAY);N=D.t.length;
  steps = D.t.map((t,i)=>({t:new Date(t),price:D.price[i],Ld:D.load[i],
    sunHome:D.sun_home[i],battHome:D.batt_home[i],otherHome:D.other_home[i],gridHome:D.grid_home[i],
    chgSun:D.chg_sun[i],chgGrid:D.chg_grid[i],m:D.mode[i]}));
  for(let k=0;k<2;k++)for(let i=1;i<N-1;i++){if(steps[i].m!==steps[i-1].m&&steps[i].m!==steps[i+1].m&&steps[i].m!=='grid_charge')steps[i].m=steps[i-1].m;}
  blocks=[];
  steps.forEach((s,i)=>{const last=blocks[blocks.length-1];if(last&&last.m===s.m)last.end=i;else blocks.push({m:s.m,start:i,end:i});});
  blocks=blocks.reduce((acc,b,k)=>{const prev=acc[acc.length-1];if(prev&&(prev.m===b.m||(b.end===b.start&&k<blocks.length-1)))prev.end=b.end;else acc.push(b);return acc;},[]);
  blocks.forEach(b=>{const sl=steps.slice(b.start,b.end+1);b.avg=sl.reduce((a,s)=>a+s.price,0)/sl.length;b.from=steps[b.start].t;b.to=new Date(steps[b.end].t.getTime()+STEP_MS);});
  endTime=new Date(steps[N-1].t.getTime()+STEP_MS);
  solveT=new Date(ALL.summary.solve);
  pmin=Math.min(...D.price);pmax=Math.max(...D.price);
  const wb=(len,dir)=>{let best=null;for(let i=0;i+len<=N;i++){const a=D.price.slice(i,i+len).reduce((x,y)=>x+y,0)/len;if(best===null||dir*a<dir*best.a)best={i,a};}return best;};
  const wlen=Math.min(8,N);
  wash=wb(wlen,1);avoid=wb(wlen,-1);
  sunBest=null;for(let i=0;i+wlen<=N;i++){const s=D.sun.slice(i,i+wlen).reduce((x,y)=>x+y,0);if(!sunBest||s>sunBest.s)sunBest={i,s};}
}
const hm=d=>d.toLocaleTimeString(T[L].dateFmt,{hour:'2-digit',minute:'2-digit',timeZone:TZ});
const hr=d=>+d.toLocaleString('en-GB',{hour:'2-digit',hour12:false,timeZone:TZ});
const nf=(v,dg=1)=>new Intl.NumberFormat(T[L].dateFmt,{minimumFractionDigits:dg,maximumFractionDigits:dg}).format(v);
const eur=v=>'€ '+nf(v,2);
const cents=v=>Math.round(v*100);
const tAt=i=>i>=N?endTime:steps[i].t;
const WLEN=()=>Math.min(8,N);

function render(){
  const t=T[L];document.documentElement.lang=L;
  const day=DAYS[DAY],fut=DAY>0,dw=t.dw[DAY];
  // Where the plan stops inside this day, if it does not reach midnight.
  const end=day.complete?null:hm(endTime);
  document.querySelectorAll('.lang button').forEach(b=>b.classList.toggle('on',b.dataset.l===L));
  const dfmt=(d,o)=>d.toLocaleDateString(t.dateFmt,{...o,timeZone:TZ});
  document.getElementById('dateline').textContent=dfmt(steps[0].t,{weekday:'long',day:'numeric',month:'long'});
  document.getElementById('title').innerHTML=t.title(dw,fut);
  const pr=day.predicted,badge=[fut?t.badge:'',pr>=.99?t.pPred:pr>0?t.pPart:''].filter(Boolean).join(' · ');
  document.getElementById('badge').innerHTML=badge?`<span class="badge">${badge}</span>`:'';
  document.getElementById('days').innerHTML=usable(1)?t.tabs.map((name,k)=>{const d=DAYS[k];
    const sub=!d?'&nbsp;':k>0&&!d.complete?t.until(hm(new Date(new Date(ALL.t[d.i1-1]).getTime()+STEP_MS))):dfmt(new Date(ALL.t[d.i0]),{weekday:'short',day:'numeric',month:'short'});
    return `<button data-k="${k}" class="${k===DAY?'on':''}" ${usable(k)?'':'disabled'}>${name}<small>${sub}</small></button>`;}).join(''):'';
  document.querySelectorAll('#days button[data-k]').forEach(b=>b.onclick=()=>{DAY=+b.dataset.k;store.set('day',DAYS[DAY].date);prep();render();});
  const s=D.summary,how=t.how(s.has_storage);
  // A day that mostly spends energy stored before it ("carry") is told by
  // what it buys from the grid; the stored energy was paid for earlier.
  const carry=s.case==='carry',extra=s.case==='extra',cashSave=Math.max(0,s.naive-s.cash);
  document.getElementById('saveK').textContent=carry?t.cashK(dw,fut):extra?t.extraK(dw,fut):
    (s.case==='earn'||s.case==='flip')?t.gainK(dw,fut,s.has_storage):t.saveK(dw,fut,s.has_storage);
  document.getElementById('saveBig').innerHTML=`<small>€</small>${nf(carry?cashSave:Math.abs(extra?s.saving:Math.max(0,s.saving)),2)}`;
  document.getElementById('saveP').innerHTML=
    carry?(cashSave>0.005?t.carryP(nf(cashSave,2),nf(-s.stored,2),dw):t.carry0P(nf(-s.stored,2),dw)):
    extra?t.extraP(dw):
    s.case==='cost'?t.saveP(Math.round(s.saving/s.naive*100),how,dw):
    s.case==='flip'?t.saveFlip(how,dw):
    s.case==='earn'?t.saveEarn(nf(-s.opt,2),how,dw):t.saveNone(dw);
  if(s.rest_saving>0.005&&['cost','flip','earn'].includes(s.case))document.getElementById('saveP').innerHTML+=`<br><b>${t.rest(nf(s.rest_saving,2),end)}</b>`;
  document.getElementById('stored').textContent=carry?'':s.stored<-0.005?t.usedSt(nf(-s.stored,2)):s.stored>0.005?t.keptSt(nf(s.stored,2)):'';
  // Never show a minus sign: say "pay" or "earn". Bars scale by size; colour says which.
  const money=v=>`${v>0?t.pay:t.earn} € ${nf(Math.abs(v),2)}`;
  // The second bar is what the card's number is about: the effective cost,
  // or for a carry day what it buys from the grid.
  const withPlan=carry?s.cash:s.opt;
  const big=Math.max(Math.abs(s.naive),Math.abs(withPlan))||1;
  const barW=(id,v)=>{document.getElementById(id).style.background=v>0?'#F2B9A3':'#9FE3CC';return Math.abs(v)/big*100;};
  document.getElementById('cb1').textContent=t.cb1;document.getElementById('cb2').textContent=t.cb2;
  document.getElementById('v1').textContent=money(s.naive);document.getElementById('v2').textContent=money(withPlan);
  const w1=barW('f1',s.naive),w2=barW('f2',withPlan);
  requestAnimationFrame(()=>{document.getElementById('f1').style.width=w1+'%';document.getElementById('f2').style.width=w2+'%';});
  // With one block there is no "next": the day stays as it is.
  const cur=blocks[0],nxt=blocks[1]||blocks[0],mc=MODE[cur.m],mn=MODE[nxt.m];
  document.getElementById('nextRow').style.display=blocks.length>1?'':'none';
  document.getElementById('nowK').textContent=fut?t.startK(dw):t.nowK(hm(solveT));
  const soc=day.soc_start_pct,socRow=document.getElementById('socRow');
  socRow.style.display=soc==null?'none':'';
  if(soc!=null){const mb=MODE.battery;document.getElementById('socIcon').style.background=mb.bg;document.getElementById('socIcon').innerHTML=IC.batt(mb.col);
    document.getElementById('socTxt').innerHTML=fut?t.socStart(soc):t.socNow(soc);}
  document.getElementById('nowIcon').style.background=mc.bg;document.getElementById('nowIcon').innerHTML=IC[mc.ic](mc.col);
  document.getElementById('nowWhat').textContent=t.m[cur.m].a;document.getElementById('nowWhy').textContent=t.m[cur.m].b(cents(cur.avg));
  document.getElementById('nextIcon').style.background=mn.bg;document.getElementById('nextIcon').innerHTML=IC[mn.ic](mn.col);
  document.getElementById('nextTxt').innerHTML=`${t.nextPre}, ${t.nextAt} <b>${hm(nxt.from)}</b>: ${t.m[nxt.m].a.toLowerCase()}`;
  document.getElementById('planH').textContent=t.planH(dw,fut,end);document.getElementById('planS').textContent=t.planS;
  const bar=document.getElementById('tlbar'),ul=document.getElementById('steps');bar.innerHTML='';ul.innerHTML='';
  blocks.forEach((b,k)=>{const m=MODE[b.m],n=b.end-b.start+1;
    const seg=document.createElement('div');seg.className='tl-seg';seg.style.flex=n;seg.style.background=m.col;seg.dataset.k=k;
    seg.title=`${hm(b.from)} - ${hm(b.to)}  ${t.m[b.m].a}`;if(n>=3)seg.innerHTML=IC[m.ic]('#fff');bar.appendChild(seg);
    const li=document.createElement('li');li.dataset.k=k;
    li.innerHTML=`<span class="time">${hm(b.from)} - ${hm(b.to)}</span><div class="icon" style="background:${m.bg}">${IC[m.ic](m.col)}</div><div><div class="t1">${t.m[b.m].a}</div><div class="t2">${t.m[b.m].b(cents(b.avg))}</div></div><span class="pill" style="background:${m.bg};color:${m.col}">± ${cents(b.avg)} ${t.cent}</span>`;
    ul.appendChild(li);});
  const sel=k=>document.querySelectorAll('.tl-seg,.steps li').forEach(e=>e.classList.toggle('sel',e.dataset.k==k));
  bar.onclick=ul.onclick=e=>{const el=e.target.closest('[data-k]');if(el){sel(el.dataset.k);if(el.classList.contains('tl-seg')){const r=ul.querySelector(`li[data-k="${el.dataset.k}"]`);if(r)r.scrollIntoView({behavior:'smooth',block:'nearest'});}}};
  bar.onmouseover=e=>{const el=e.target.closest('[data-k]');if(el)sel(el.dataset.k);};
  const ticks=document.getElementById('tlticks');ticks.innerHTML='';
  steps.forEach((st,i)=>{if(st.t.getUTCMinutes()===0&&hr(st.t)%2===0){const sp=document.createElement('span');sp.style.left=(i/N*100)+'%';sp.textContent=hm(st.t);ticks.appendChild(sp);}});
  const used=[...new Set(blocks.map(b=>b.m))];
  document.getElementById('legend').innerHTML=used.map(m=>`<span><i style="background:${MODE[m].col}"></i>${t.m[m].a}</span>`).join('');
  document.getElementById('tipsH').textContent=t.tipsH;document.getElementById('tipsS').textContent=t.tipsS;
  const tc=(ic,col,bg,arr)=>`<div class="tipcard"><div class="icon" style="background:${bg}">${IC[ic](col)}</div><div><b>${arr[0]}</b><span>${arr[1]}</span></div></div>`;
  const wl=WLEN(),avoidMax=Math.max(...D.price.slice(avoid.i,avoid.i+wl));
  document.getElementById('tips').innerHTML=tc('wash','#4E8A3E','#E2EFDB',t.tWash(hm(tAt(wash.i)),hm(tAt(wash.i+wl)),cents(wash.a),fut))+
    tc('coin','#C2502C','#F8E0D6',t.tAvoid(hm(tAt(avoid.i)),hm(tAt(avoid.i+wl)),cents(avoidMax)))+
    // A sun tip only when the best window holds real sun (0.1 kWh, quarter steps).
    (sunBest.s*STEP_MS/36e5>=0.1?tc('sun','#D9901A','#FDEFD2',t.tSun(hm(tAt(sunBest.i)),hm(tAt(sunBest.i+wl)))):'');
  document.getElementById('priceH').textContent=t.priceH(dw);document.getElementById('priceS').textContent=t.priceS;
  document.getElementById('flowH').textContent=t.flowH;document.getElementById('flowS').textContent=t.flowS;
  const lg=t.lg;
  const hasSt=D.summary.has_storage,hasOth=D.summary.other_kwh>0;
  document.getElementById('flowLegend').innerHTML=[['var(--sun)',lg.sun,1],['var(--batt)',lg.batt,hasSt],['var(--grid2)',lg.grid,1],['#B7A99A',lg.oth,hasOth],['var(--sun2)',lg.cs,hasSt],['#9DB7DE',lg.cg,hasSt]].filter(x=>x[2]).map(([c,n])=>`<span><i style="background:${c}"></i>${n}</span>`).join('')+`<span><i style="background:none;border-top:2px dashed var(--ink);border-radius:0;height:0;width:16px"></i>${t.house}</span>`;
  document.getElementById('srcH').textContent=t.srcH;document.getElementById('srcS').textContent=t.srcS(fut,end,day.past_from);
  const sunT=s.src.sun,battT=s.src.batt,gridT=s.src.grid,othT=s.src.oth,tot=sunT+battT+gridT+othT||1;
  const seg2=(v,c,n)=>`<div style="flex:${v};background:${c}">${v/tot>.12?`${n} ${Math.round(v/tot*100)}%`:''}</div>`;
  document.getElementById('srcbar').innerHTML=seg2(sunT,'var(--sun)',lg.sun)+seg2(battT,'var(--batt)',lg.batt)+seg2(othT,'#B7A99A',lg.oth)+seg2(gridT,'var(--grid)',lg.grid);
  const stt=t.st;
  document.getElementById('stats').innerHTML=[[nf(s.load,1),'kWh',stt.use],[nf(s.pv,1),'kWh',stt.sun],[nf(s.import,1),'kWh',stt.imp],[nf(s.self,0),'%',stt.self]]
    .map(([n,u,l])=>`<div class="stat"><div class="n">${n}<small>${u}</small></div><div class="l">${l}</div></div>`).join('');
  const planEnd=new Date(new Date(ALL.t[ALL.t.length-1]).getTime()+STEP_MS);
  document.getElementById('foot').innerHTML=t.foot(hm(solveT),nf((planEnd-new Date(ALL.t[0]))/36e5,0))+(D.summary.other_kwh>0?'<br>'+t.other(nf(D.summary.other_kwh,1)):'');
  drawPrice();drawFlow();
}

function drawPrice(){
  const box=document.getElementById('priceChart'),tip=box.querySelector('.tip');box.querySelectorAll('svg').forEach(e=>e.remove());
  const W=box.clientWidth||600,H=270,ml=34,mr=10,mt=30,mb=28;
  const lo=Math.floor(pmin*20)/20-0.02,hi=Math.ceil(pmax*20)/20||0.05;
  const x=i=>ml+(i/(Math.max(1,N-1)))*(W-ml-mr),y=p=>mt+(1-(p-lo)/((hi-lo)||1))*(H-mt-mb);
  let path='';D.price.forEach((p,i)=>{path+=(i?'L':'M')+x(i).toFixed(1)+','+y(p).toFixed(1);});
  const yl=y(pmax),yh=y(pmin);
  let g=`<defs><linearGradient id="pg" gradientUnits="userSpaceOnUse" x1="0" y1="${yl}" x2="0" y2="${yh}"><stop offset="0" stop-color="#D25F3A"/><stop offset=".5" stop-color="#E2A93B"/><stop offset="1" stop-color="#6E9E5C"/></linearGradient><linearGradient id="pa" gradientUnits="userSpaceOnUse" x1="0" y1="${yl}" x2="0" y2="${H-mb}"><stop offset="0" stop-color="#D25F3A" stop-opacity=".22"/><stop offset=".6" stop-color="#E2A93B" stop-opacity=".12"/><stop offset="1" stop-color="#6E9E5C" stop-opacity=".02"/></linearGradient></defs>`;
  for(let v=Math.ceil(lo*20)/20;v<=hi+1e-9;v+=0.05){g+=`<line x1="${ml}" x2="${W-mr}" y1="${y(v)}" y2="${y(v)}" stroke="#EADFCD"/><text x="${ml-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="#8B8578">${Math.round(v*100)}</text>`;}
  g+=`<text x="${ml-6}" y="${mt-14}" text-anchor="end" font-size="11" fill="#8B8578">${T[L].cent}</text>`;
  steps.forEach((st,i)=>{if(st.t.getUTCMinutes()===0&&hr(st.t)%3===2)g+=`<text x="${x(i)}" y="${H-8}" text-anchor="middle" font-size="11" fill="#8B8578">${hm(st.t)}</text>`;});
  g+=`<path d="${path}L${x(N-1)},${H-mb}L${x(0)},${H-mb}Z" fill="url(#pa)"/><path d="${path}" fill="none" stroke="url(#pg)" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/>`;
  const imin=D.price.indexOf(pmin),imax=D.price.indexOf(pmax);
  const lab=(i,txt,c,up)=>`<circle cx="${x(i)}" cy="${y(D.price[i])}" r="5.5" fill="#fff" stroke="${c}" stroke-width="3"/><text x="${x(i)}" y="${y(D.price[i])+(up?-12:22)}" text-anchor="middle" font-size="12" font-weight="600" fill="${c}">${txt}</text>`;
  g+=lab(imin,`${T[L].cheapest} ${hm(steps[imin].t)} · ${cents(pmin)}c`,'#4E8A3E',false);
  g+=lab(imax,`${T[L].dearest} ${hm(steps[imax].t)} · ${cents(pmax)}c`,'#C2502C',true);
  g+=`<line id="pcur" y1="${mt}" y2="${H-mb}" stroke="#1E2733" stroke-width="1" stroke-dasharray="3 3" opacity="0"/><circle id="pdot" r="5" fill="#1E2733" opacity="0"/><rect x="${ml}" y="0" width="${W-ml-mr}" height="${H}" fill="transparent" id="phit"/>`;
  box.insertAdjacentHTML('afterbegin',`<svg height="${H}" viewBox="0 0 ${W} ${H}">${g}</svg>`);
  const hit=box.querySelector('#phit'),cur=box.querySelector('#pcur'),dot=box.querySelector('#pdot');
  hit.onmousemove=e=>{const r=box.getBoundingClientRect();const i=Math.max(0,Math.min(N-1,Math.round((e.clientX-r.left-ml)/(W-ml-mr)*(N-1))));const p=D.price[i];
    cur.setAttribute('x1',x(i));cur.setAttribute('x2',x(i));cur.setAttribute('opacity',1);dot.setAttribute('cx',x(i));dot.setAttribute('cy',y(p));dot.setAttribute('opacity',1);
    tip.style.left=x(i)+'px';tip.style.top=y(p)+'px';tip.style.opacity=1;tip.innerHTML=`${hm(steps[i].t)}<br><b>${cents(p)} ${T[L].cent}</b> / kWh`;};
  hit.onmouseleave=()=>{tip.style.opacity=0;cur.setAttribute('opacity',0);dot.setAttribute('opacity',0);};
}

function drawFlow(){
  const box=document.getElementById('flowChart'),tip=box.querySelector('.tip');box.querySelectorAll('svg').forEach(e=>e.remove());
  const H={},order=[];
  steps.forEach(s=>{const k=hr(s.t);if(!(k in H)){H[k]={h:k,sun:0,batt:0,oth:0,grid:0,cs:0,cg:0,use:0,price:0,n:0};order.push(k);}const o=H[k];o.sun+=s.sunHome;o.batt+=s.battHome;o.oth+=s.otherHome;o.grid+=s.gridHome;o.cs+=s.chgSun;o.cg+=s.chgGrid;o.use+=s.Ld;o.price+=s.price;o.n++;});
  const hrs=order.map(k=>H[k]);
  hrs.forEach(o=>{o.sun*=0.25;o.batt*=0.25;o.oth*=0.25;o.grid*=0.25;o.cs*=0.25;o.cg*=0.25;o.use*=0.25;});
  const W=box.clientWidth||600,Ht=320,ml=40,mr=10,mt=16,mb=30;
  const up=Math.max(...hrs.map(o=>o.sun+o.batt+o.oth+o.grid),0.1),dn=Math.max(...hrs.map(o=>o.cs+o.cg),0.1);
  const top=Math.ceil(up*4)/4,bot=Math.ceil(dn*4)/4,y=v=>mt+(top-v)/((top+bot)||1)*(Ht-mt-mb);
  const bw=(W-ml-mr)/hrs.length,pad=Math.max(3,bw*.18);let g='';
  for(let v=-bot;v<=top+1e-9;v+=0.25){g+=`<line x1="${ml}" x2="${W-mr}" y1="${y(v)}" y2="${y(v)}" stroke="${v===0?'#B9AE9A':'#EFE6D6'}" ${v===0?'stroke-width="1.5"':''}/>`;if(Math.abs(v*2-Math.round(v*2))<1e-6)g+=`<text x="${ml-6}" y="${y(v)+4}" text-anchor="end" font-size="11" fill="#8B8578">${nf(Math.abs(v),1)}</text>`;}
  g+=`<text x="${ml-6}" y="${mt-4}" text-anchor="end" font-size="11" fill="#8B8578">kWh</text>`;
  const rect=(x0,v0,v1,c,r)=>{const a=y(Math.max(v0,v1)),b=y(Math.min(v0,v1));return b-a<0.5?'':`<rect x="${x0}" y="${a}" width="${bw-2*pad}" height="${b-a}" fill="${c}" rx="${r}"/>`;};
  let linePts=[];
  hrs.forEach((o,i)=>{const x0=ml+i*bw+pad;let acc=0;g+=rect(x0,acc,acc+=o.sun,'#F0A92E',2);g+=rect(x0,acc,acc+=o.batt,'#2E8C80',2);g+=rect(x0,acc,acc+=o.oth,'#B7A99A',2);g+=rect(x0,acc,acc+=o.grid,'#C9CFDA',2);
    let d=0;g+=rect(x0,d,d-=o.cs,'#FFD27A',2);g+=rect(x0,d,d-=o.cg,'#9DB7DE',2);linePts.push([ml+i*bw+bw/2,y(o.use)]);
    g+=`<text x="${ml+i*bw+bw/2}" y="${Ht-10}" text-anchor="middle" font-size="11" fill="#8B8578">${String(o.h).padStart(2,'0')}${bw>40?':00':''}</text>`;});
  g+=`<polyline points="${linePts.map(p=>p.join(',')).join(' ')}" fill="none" stroke="#1E2733" stroke-width="1.6" stroke-dasharray="4 4"/><rect id="fhl" y="${mt}" height="${Ht-mt-mb}" width="${bw}" fill="#1E2733" opacity="0" rx="8"/><rect x="${ml}" y="0" width="${W-ml-mr}" height="${Ht}" fill="transparent" id="fhit"/>`;
  box.insertAdjacentHTML('afterbegin',`<svg height="${Ht}" viewBox="0 0 ${W} ${Ht}">${g}</svg>`);
  const hit=box.querySelector('#fhit'),hl=box.querySelector('#fhl'),lg=T[L].lg;
  hit.onmousemove=e=>{const r=box.getBoundingClientRect();const i=Math.max(0,Math.min(hrs.length-1,Math.floor((e.clientX-r.left-ml)/bw)));const o=hrs[i];
    hl.setAttribute('x',ml+i*bw);hl.setAttribute('opacity',.05);
    const row=(c,n,v)=>v>0.005?`<span style="color:${c}">■</span> ${n}: ${nf(v,2)} kWh<br>`:'';
    tip.innerHTML=`<b>${String(o.h).padStart(2,'0')}:00 - ${String((o.h+1)%24).padStart(2,'0')}:00</b> · ${cents(o.price/o.n)} ${T[L].cent}<br>`+row('#F0A92E',lg.sun,o.sun)+row('#5FC2B4',lg.batt,o.batt)+row('#B7A99A',lg.oth,o.oth)+row('#C9CFDA',lg.grid,o.grid)+row('#FFD27A',lg.cs,o.cs)+row('#9DB7DE',lg.cg,o.cg);
    tip.style.left=Math.min(Math.max(ml+i*bw+bw/2,110),W-110)+'px';tip.style.top=(y(Math.max(o.sun+o.batt+o.oth+o.grid,o.use))-6)+'px';tip.style.opacity=1;};
  hit.onmouseleave=()=>{tip.style.opacity=0;hl.setAttribute('opacity',0);};
}

function boot(){
  if(!ALL.t.length){document.querySelector('.wrap').innerHTML=`<div class="wait">${T[L].wait}</div>`;return;}
  // Reopen the day that was being looked at, if the plan still offers it.
  const kept=DAYS.findIndex(d=>d.date===store.get('day'));
  DAY=kept>0&&usable(kept)?kept:0;
  prep();
  document.querySelectorAll('.lang button').forEach(b=>b.onclick=()=>{L=b.dataset.l;store.set('lang',L);render();});
  let rt;window.addEventListener('resize',()=>{clearTimeout(rt);rt=setTimeout(()=>{drawPrice();drawFlow();},120);});
  render();
}
boot();
</script>
</body>
</html>
"""
