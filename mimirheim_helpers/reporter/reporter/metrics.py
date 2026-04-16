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
        pv_total_kwh: Total PV generation over the horizon.
        load_total_kwh: Total energy consumed by static and deferrable loads.
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


def compute_schedule_metrics(schedule: list[dict[str, Any]]) -> ScheduleMetrics:
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

    Load devices (``static_load``, ``deferrable_load``) therefore have
    **negative** ``kw`` values. This function negates them before summing to
    produce positive kWh consumed.

    Args:
        schedule: List of schedule step dicts from a ``SolveResult`` dump.

    Returns:
        A ``ScheduleMetrics`` instance with all derived energy values.
    """
    grid_import_kwh = sum(
        s.get("grid_import_kw", 0.0) * _STEP_HOURS for s in schedule
    )
    grid_export_kwh = sum(
        s.get("grid_export_kw", 0.0) * _STEP_HOURS for s in schedule
    )

    # PV generation: positive kw from PV devices.
    pv_total_kwh = sum(
        max(0.0, sp.get("kw", 0.0)) * _STEP_HOURS
        for s in schedule
        for sp in s.get("devices", {}).values()
        if sp.get("type") == "pv"
    )

    # Load consumption: load device kw is negative (consuming); negate to get
    # positive kWh. Using max(0.0, -kw) guards against any unexpected positive
    # values on a load device without silently distorting the total.
    load_total_kwh = sum(
        max(0.0, -sp.get("kw", 0.0)) * _STEP_HOURS
        for s in schedule
        for sp in s.get("devices", {}).values()
        if sp.get("type") in ("static_load", "deferrable_load")
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
