"""Shared energy and economic metrics for mimirheim-reporter.

This module is the single source of truth for all quantitative computations
derived from a solved schedule or a SolveResult output dict. It is a
pure-function library with no I/O or external dependencies beyond the Python
standard library.

What this module does:
    - Compute grid exchange, PV generation, load consumption, self-consumption,
      and self-sufficiency from a raw schedule list.
    - Compute economic performance indicators (naive cost, optimised cost, SOC
      credit, effective cost, saving) from a SolveResult output dict.
    - Compute a representative average efficiency from a device segment or
      piecewise-linear curve model.
    - Split a hybrid inverter's AC exchange into its solar and battery parts.

What this module does not do:
    - Read or write files.
    - Publish MQTT messages.
    - Render HTML or Plotly figures.
    - Import from ``mimirheim`` or any other reporter submodule.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Each solver time step is 15 minutes.
_STEP_HOURS = 15.0 / 60.0


@dataclass(frozen=True)
class ScheduleMetrics:
    """Energy metrics derived from a single solved schedule.

    All energy quantities are in kWh. Percentage values are in the range
    [0.0, 100.0]. Both percentages default to 0.0 when the divisor is zero
    (no PV generation or no load).

    Attributes:
        grid_import_kwh: Total energy imported from the grid over the horizon.
        grid_export_kwh: Total energy exported to the grid over the horizon.
        pv_total_kwh: Total PV generation over the horizon: the output of
            ``pv`` devices plus the solar behind hybrid inverters, the latter
            measured as DC at the MPPT input (see ``split_hybrid_step``).
        load_total_kwh: Total energy consumed by the house: every consuming
            device type (see ``_LOAD_TYPES``), the EV while charging included.
        self_consumption_kwh: PV energy consumed locally (not exported).
        self_consumption_pct: Fraction of PV generation consumed locally,
            as a percentage. Zero when there is no PV generation.
        self_sufficiency_pct: Fraction of load consumption met without
            importing from the grid, as a percentage. Zero when there is
            no load.
    """

    grid_import_kwh: float
    grid_export_kwh: float
    pv_total_kwh: float
    load_total_kwh: float
    self_consumption_kwh: float
    self_consumption_pct: float
    self_sufficiency_pct: float


# Device types whose negative ``kw`` is energy the house consumes. Kept in
# step with ``_LOAD_TYPES`` in ``mimirheim.io.mqtt_publisher`` so the published
# self-sufficiency and the reports agree.
_LOAD_TYPES = frozenset(
    {
        "static_load",
        "deferrable_load",
        "ev_charger",
        "thermal_boiler",
        "heat_store",
        "space_heating_hp",
        "combi_heat_pump",
    }
)


def compute_schedule_metrics(
    schedule: list[dict[str, Any]],
    inp: dict[str, Any] | None = None,
) -> ScheduleMetrics:
    """Compute energy metrics from a raw schedule list.

    Each entry in ``schedule`` is expected to have the structure produced by
    ``SolveResult`` serialisation::

        {
            "grid_import_kw": float,
            "grid_export_kw": float,
            "devices": {
                "<name>": {
                    "type": "<device_type>",
                    "kw": float,
                    ...
                },
                ...
            }
        }

    Device ``kw`` sign convention (as defined by ``DeviceSetpoint``):
        - Positive: device is producing power (PV generation, V2H discharge).
        - Negative: device is consuming power (battery charging, load draw).

    Load is the negated ``kw`` of every consuming device type: static and
    deferrable loads, EV chargers while charging, and the thermal device types
    (boiler, space-heating and combi heat pumps). A vehicle-to-home discharge
    (positive ``kw`` on an EV charger) is not load, and batteries and hybrid
    inverters are storage, not load.

    Generation is the positive ``kw`` of ``pv`` devices plus the solar behind
    each hybrid inverter, split out of its AC ``kw`` by ``hybrid_splits``.

    Args:
        schedule: List of schedule step dicts from a ``SolveResult`` dump.
        inp: Optional parsed ``SolveBundle`` (the ``*_input.json`` dump). Only
            used for hybrid inverters: their starting SOC and configured
            efficiencies. Without it the first hybrid step's solar is unknown
            and the efficiencies default to 1.

    Returns:
        A ``ScheduleMetrics`` instance with all derived energy values.
    """
    grid_import_kwh = sum(
        s.get("grid_import_kw", 0.0) * _STEP_HOURS for s in schedule
    )
    grid_export_kwh = sum(
        s.get("grid_export_kw", 0.0) * _STEP_HOURS for s in schedule
    )

    # PV generation: positive kw from PV devices, plus hybrid inverter solar.
    pv_total_kwh = sum(
        max(0.0, sp.get("kw", 0.0)) * _STEP_HOURS
        for s in schedule
        for sp in s.get("devices", {}).values()
        if sp.get("type") == "pv"
    ) + sum(
        split.pv_dc_kw * _STEP_HOURS
        for step_splits in hybrid_splits(schedule, inp)
        for split in step_splits.values()
    )

    # Load consumption: load device kw is negative (consuming); negate to get
    # positive kWh. Using max(0.0, -kw) guards against any unexpected positive
    # values on a load device without silently distorting the total, and keeps
    # a vehicle-to-home EV discharge out of the load.
    load_total_kwh = sum(
        max(0.0, -sp.get("kw", 0.0)) * _STEP_HOURS
        for s in schedule
        for sp in s.get("devices", {}).values()
        if sp.get("type") in _LOAD_TYPES
    )

    # Self-consumption: PV energy not exported (consumed locally).
    self_consumption_kwh = max(0.0, pv_total_kwh - grid_export_kwh)
    self_consumption_pct = (
        round(self_consumption_kwh / pv_total_kwh * 100.0, 1)
        if pv_total_kwh > 0.0
        else 0.0
    )

    # Self-sufficiency: fraction of load met without importing from the grid.
    load_served_local = max(0.0, load_total_kwh - grid_import_kwh)
    self_sufficiency_pct = (
        round(load_served_local / load_total_kwh * 100.0, 1)
        if load_total_kwh > 0.0
        else 0.0
    )

    return ScheduleMetrics(
        grid_import_kwh=round(grid_import_kwh, 4),
        grid_export_kwh=round(grid_export_kwh, 4),
        pv_total_kwh=round(pv_total_kwh, 4),
        load_total_kwh=round(load_total_kwh, 4),
        self_consumption_kwh=round(self_consumption_kwh, 4),
        self_consumption_pct=self_consumption_pct,
        self_sufficiency_pct=self_sufficiency_pct,
    )


@dataclass(frozen=True)
class HybridSplit:
    """One step of a hybrid inverter, split into its solar and battery parts.

    All values are power in kW and non-negative. ``pv_dc_kw`` and
    ``pv_to_cell_kw`` are DC at the MPPT input, the real generation figure;
    the ``*_ac_kw`` values are AC at the inverter's grid side, where the house
    sees them. When not clamped, ``pv_dc_kw`` equals
    ``pv_ac_kw / inverter_efficiency + pv_to_cell_kw``.

    Attributes:
        pv_dc_kw: Solar generated by the hybrid's own panels.
        pv_ac_kw: Part of the AC output that came from the solar.
        battery_ac_kw: Part of the AC output that came from the battery.
        ac_charge_kw: AC drawn by the hybrid to charge its battery.
        pv_to_cell_kw: Solar sent into the battery.
    """

    pv_dc_kw: float
    pv_ac_kw: float
    battery_ac_kw: float
    ac_charge_kw: float
    pv_to_cell_kw: float


def split_hybrid_step(
    kw: float,
    soc_prev_kwh: float | None,
    soc_now_kwh: float | None,
    step_hours: float = _STEP_HOURS,
    *,
    inverter_efficiency: float = 1.0,
    charge_efficiency: float = 1.0,
    discharge_efficiency: float = 1.0,
) -> HybridSplit:
    """Split one hybrid inverter step into solar and battery, from the plan alone.

    The hybrid's AC ``kw`` mixes its own panels with its battery. The planned
    SOC tells the two apart. Balancing the DC bus between panels, cell and
    inverter gives the solar at the MPPT input::

        charge_dc    = max(cell_kw, 0) / charge_efficiency
        discharge_dc = max(-cell_kw, 0) * discharge_efficiency
        pv_dc        = charge_dc - discharge_dc
                       + ac_out / inverter_efficiency - ac_in * inverter_efficiency

    where ``cell_kw`` is the SOC change over the step. The battery's share of
    the AC output is what leaves the cell, through the inverter; the rest is
    solar. Solar sent into the cell is whatever charge the AC side did not
    supply.

    When either SOC is unknown the split cannot be made, so none of the output
    is counted as solar: the AC exchange is reported as battery.

    Args:
        kw: The hybrid's AC exchange; positive delivers to the house.
        soc_prev_kwh: Cell energy at the start of the step, if known.
        soc_now_kwh: Cell energy at the end of the step, if known.
        step_hours: Step length in hours.
        inverter_efficiency: DC-to-AC (and AC-to-DC) efficiency.
        charge_efficiency: Bus-to-cell efficiency.
        discharge_efficiency: Cell-to-bus efficiency.

    Returns:
        The step's ``HybridSplit``.
    """
    ac_out = max(0.0, kw)
    ac_in = max(0.0, -kw)
    if soc_prev_kwh is None or soc_now_kwh is None or step_hours <= 0.0:
        return HybridSplit(0.0, 0.0, ac_out, ac_in, 0.0)
    inv = inverter_efficiency or 1.0
    cell_kw = (soc_now_kwh - soc_prev_kwh) / step_hours
    charge_dc = max(0.0, cell_kw) / (charge_efficiency or 1.0)
    discharge_dc = max(0.0, -cell_kw) * (discharge_efficiency or 1.0)
    battery_ac = min(ac_out, discharge_dc * inv)
    return HybridSplit(
        pv_dc_kw=max(0.0, charge_dc - discharge_dc + ac_out / inv - ac_in * inv),
        pv_ac_kw=ac_out - battery_ac,
        battery_ac_kw=battery_ac,
        ac_charge_kw=ac_in,
        pv_to_cell_kw=max(0.0, charge_dc - ac_in * inv),
    )


def hybrid_splits(
    schedule: list[dict[str, Any]],
    inp: dict[str, Any] | None = None,
) -> list[dict[str, HybridSplit]]:
    """Split every hybrid inverter in every step, walking the schedule in order.

    Each step's ``soc_kwh`` is the SOC at the end of that step, so the SOC
    change is measured against the previous step. The first step is measured
    against the starting SOC in ``inp["hybrid_inverter_inputs"]``, and the
    efficiencies come from ``inp["config"]["hybrid_inverters"]``.

    Args:
        schedule: List of schedule step dicts from a ``SolveResult`` dump.
        inp: Optional parsed ``SolveBundle``. Without it the first step's SOC
            change is unknown and the efficiencies default to 1.

    Returns:
        One dict per step, mapping each hybrid inverter's name to its split.
    """
    inp = inp or {}
    cfgs = (inp.get("config") or {}).get("hybrid_inverters") or {}
    soc: dict[str, float | None] = {
        name: v.get("soc_kwh")
        for name, v in (inp.get("hybrid_inverter_inputs") or {}).items()
    }
    result: list[dict[str, HybridSplit]] = []
    for s in schedule:
        step_splits: dict[str, HybridSplit] = {}
        for name, sp in s.get("devices", {}).items():
            if sp.get("type") != "hybrid_inverter":
                continue
            cfg = cfgs.get(name) or {}
            now = sp.get("soc_kwh")
            step_splits[name] = split_hybrid_step(
                sp.get("kw", 0.0) or 0.0,
                soc.get(name),
                now,
                inverter_efficiency=cfg.get("inverter_efficiency") or 1.0,
                charge_efficiency=cfg.get("battery_charge_efficiency") or 1.0,
                discharge_efficiency=cfg.get("battery_discharge_efficiency") or 1.0,
            )
            soc[name] = now
        result.append(step_splits)
    return result


@dataclass(frozen=True)
class EconomicMetrics:
    """Economic performance indicators derived from a single SolveResult.

    All monetary values are in EUR.

    Attributes:
        naive_cost_eur: Total cost under the naive (no-storage) baseline.
        optimised_cost_eur: Raw optimised cost before SOC terminal credit.
        soc_credit_eur: SOC terminal-state credit subtracted from the raw cost
            to account for residual battery charge at the horizon end.
        effective_cost_eur: Net optimised cost after deducting the SOC credit
            (``optimised_cost_eur - soc_credit_eur``).
        saving_eur: Absolute cost reduction vs the naive baseline
            (``naive_cost_eur - effective_cost_eur``). Positive means the
            optimised schedule is cheaper.
        saving_pct: Percentage saving vs the naive baseline. Zero when the
            naive cost is zero.
    """

    naive_cost_eur: float
    optimised_cost_eur: float
    soc_credit_eur: float
    effective_cost_eur: float
    saving_eur: float
    saving_pct: float


def compute_economic_metrics(out: dict[str, Any]) -> EconomicMetrics:
    """Compute economic performance indicators from a raw SolveResult dict.

    Args:
        out: Parsed SolveResult JSON (the ``*_output.json`` dump). The
            function reads ``naive_cost_eur``, ``optimised_cost_eur``, and
            ``soc_credit_eur``. Missing or null values are treated as zero.

    Returns:
        An ``EconomicMetrics`` instance with all derived cost values.
    """
    naive = out.get("naive_cost_eur") or 0.0
    optimised = out.get("optimised_cost_eur") or 0.0
    credit = out.get("soc_credit_eur") or 0.0
    effective = optimised - credit
    saving = naive - effective
    saving_pct = round(saving / naive * 100.0, 1) if naive != 0.0 else 0.0

    return EconomicMetrics(
        naive_cost_eur=round(naive, 4),
        optimised_cost_eur=round(optimised, 4),
        soc_credit_eur=round(credit, 4),
        effective_cost_eur=round(effective, 4),
        saving_eur=round(saving, 4),
        saving_pct=saving_pct,
    )


def avg_segment_efficiency(
    segments: list[dict[str, Any]] | None,
    curve: list[dict[str, Any]] | None = None,
) -> float:
    """Return a representative round-trip efficiency for a device power model.

    Two device models are supported:

    Segment model (``segments``):
        A list of power segments each with a fixed ``efficiency`` and a maximum
        power capacity ``power_max_kw``. The returned value is the
        capacity-weighted average across all segments, which correctly weights
        higher-capacity segments more heavily than small ones. A simple
        unweighted average would understate efficiency on devices where most
        power flows through the largest, most efficient segment.

    SOS2 piecewise-linear curve (``curve``):
        A list of breakpoints with ``power_kw`` and ``efficiency`` values. The
        full-load efficiency (the last non-zero breakpoint) is returned as the
        representative value, since the device will typically operate near full
        load during optimised dispatch.

    When neither model is provided, returns 1.0 (lossless fallback).

    Args:
        segments: List of segment dicts with ``power_max_kw`` and
            ``efficiency`` keys. May be None.
        curve: List of breakpoint dicts with ``power_kw`` and ``efficiency``
            keys. May be None.

    Returns:
        A representative efficiency in the range (0, 1]. Never zero.
    """
    if segments:
        total_kw = sum(s["power_max_kw"] for s in segments)
        if total_kw > 0.0:
            return (
                sum(s["efficiency"] * s["power_max_kw"] for s in segments)
                / total_kw
            )
    if curve:
        non_zero = [bp for bp in curve if bp["power_kw"] > 0.0]
        if non_zero:
            return non_zero[-1]["efficiency"]
    return 1.0
