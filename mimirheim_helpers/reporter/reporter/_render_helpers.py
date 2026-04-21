"""Rendering library for mimirheim solve-dump analysis pages.

This module is the single source of truth for all HTML report rendering.
It takes a parsed dump pair (input + output JSON) and produces a Plotly
Figure. Writing the HTML file is the caller's responsibility so that the
caller can control the ``include_plotlyjs`` setting (see R6 below).

What this module does:
    - Parse and interpret mimirheim dump structures (energy-flow, SOC, prices,
      closed-loop state).
    - Build a combined Plotly Figure with all subplots assembled into a
      single interactive HTML page.

What this module does not do:
    - Read or write any files.
    - Import from ``mimirheim`` (operates on plain dicts from JSON.
    - Publish MQTT messages.
    - Format CLI output.

Rendering improvements over the original analyse_dump.py:

    R1 — Grid import/export bars + net exchange line overlay added to the
         optimised energy-flow chart.

    R2 — Two-table summary dashboard at the top of the page: economic
         summary on the left, exchange and self-sufficiency on the right.

    R3 — Closed-loop shading bands on each device SOC row. ZEX periods
         are shown in light indigo; LB (load-balance) periods in light
         purple. Both are labelled with short text annotations.

    R4 — ZEX and LB flag columns in the step-by-step data table with
         per-cell colour highlights.

    R5 — Five-tier row colour-coding in the data table: closed-loop
         (indigo), export (green), high-price import (amber), import
         (pink), default alternating.

    R6 — ``build_combined_figure`` returns a ``go.Figure`` without calling
         ``write_html``. Callers must pass ``include_plotlyjs="directory"``
         to ``fig.write_html(...)`` so that a single ``plotly.min.js`` is
         shared across all reports in a directory.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from reporter.metrics import avg_segment_efficiency, compute_economic_metrics, compute_schedule_metrics

# Each solver time step is 15 minutes.
STEP_MINUTES = 15
STEP_HOURS = STEP_MINUTES / 60.0

# JavaScript injected into the HTML output via write_html(post_script=...).
# It runs after Plotly initialises the figure and shifts every trace's UTC
# timestamps to the browser's local timezone, then annotates each x-axis
# title with the IANA timezone name (e.g. "Europe/Amsterdam").
#
# This script is exported so that callers (analyse_dump.py, daemon.py) can
# pass it to ``fig.write_html(post_script=_TZ_SCRIPT)``.
_TZ_SCRIPT = r"""
(function() {
    var gd = document.getElementsByClassName('plotly-graph-div')[0];
    var tzOffsetMs = new Date().getTimezoneOffset() * 60 * 1000;
    if (tzOffsetMs === 0) return;  // already UTC, nothing to do

    var utcRe = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;

    // --- Shift x-arrays of chart traces ---
    var newXArrays = gd.data.map(function(trace) {
        if (!trace.x || !trace.x.length || typeof trace.x[0] !== 'string') return trace.x;
        if (!utcRe.test(trace.x[0])) return trace.x;
        return trace.x.map(function(t) {
            var d = new Date(t);
            if (isNaN(d.getTime())) return t;
            var local = new Date(d.getTime() - tzOffsetMs);
            return local.toISOString().slice(0, 19);  // strip the trailing Z
        });
    });

    Plotly.restyle(gd, {x: newXArrays});

    // --- Shift the Time column of data Table traces ---
    // cells.values is a 2-D array (one sub-array per column). Only the first
    // sub-array (the time column) is replaced with local HH:MM strings.
    gd.data.forEach(function(trace, idx) {
        if (trace.type !== 'table') return;
        var timeCol = trace.cells && trace.cells.values && trace.cells.values[0];
        if (!timeCol || !timeCol.length || !utcRe.test(timeCol[0])) return;
        var localTimes = timeCol.map(function(t) {
            var d = new Date(t);
            if (isNaN(d.getTime())) return t;
            var local = new Date(d.getTime() - tzOffsetMs);
            var hh = String(local.getUTCHours()).padStart(2, '0');
            var mm = String(local.getUTCMinutes()).padStart(2, '0');
            return hh + ':' + mm;
        });
        var newCellValues = trace.cells.values.slice();
        newCellValues[0] = localTimes;
        Plotly.restyle(gd, {'cells.values': [newCellValues]}, [idx]);
    });

    var tzName = Intl.DateTimeFormat().resolvedOptions().timeZone;
    var layoutUpdate = {};
    Object.keys(gd.layout).forEach(function(k) {
        if (!k.match(/^xaxis(\d+)?$/) || !gd.layout[k]) return;
        var existing = (gd.layout[k].title && gd.layout[k].title.text) || '';
        layoutUpdate[k + '.title.text'] = existing
            ? existing + ' (' + tzName + ')'
            : tzName;
    });
    if (Object.keys(layoutUpdate).length) Plotly.relayout(gd, layoutUpdate);
})();
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_combined_figure(inp: dict, out: dict) -> go.Figure:
    """Build a single Plotly Figure from a parsed dump pair.

    Layout (rows from top to bottom):
        Row 1 (2 cols): Economic summary table | Exchange + self-sufficiency table
        Row 2: Unoptimised energy-flow stacked bars (naive baseline)
        Row 3: Optimised energy-flow stacked bars + grid import/export bars
               + net exchange line overlay
        Rows 4..N: One row per dispatchable device — SOC curve + price lines
                   + closed-loop shading bands
        Last row: Step-by-step data table with ZEX/LB flag columns and
                  economic-state row colouring

    The caller is responsible for writing the HTML file. To share a single
    ``plotly.min.js`` across multiple reports in the same directory, use::

        fig.write_html(
            str(output_path),
            include_plotlyjs="directory",
            post_script=_TZ_SCRIPT,
        )

    Args:
        inp: Parsed SolveBundle JSON (the ``*_input.json`` dump).
        out: Parsed SolveResult JSON (the ``*_output.json`` dump).

    Returns:
        A single Plotly Figure ready to write as an HTML file.
    """
    schedule = out.get("schedule", [])
    n_prices = len(inp["horizon_prices"])
    times = _timestamps(inp["solve_time_utc"], n_prices)

    import_prices = inp["horizon_prices"]
    export_prices = inp["horizon_export_prices"]

    # Build x-axis labels from the schedule step timestamps.
    if schedule and isinstance(schedule[0].get("t"), str):
        xs = [s["t"] for s in schedule]
    else:
        ts_fmt = [t.strftime("%Y-%m-%dT%H:%M:%SZ") for t in times]
        xs = [
            s.get("t", ts_fmt[i]) if isinstance(s.get("t"), int) else s.get("t", ts_fmt[i])
            for i, s in enumerate(schedule)
        ]

    device_meta = _build_device_meta(inp)
    soc_histories = _reconstruct_soc(schedule, device_meta)
    n_soc_rows = max(1, len(device_meta))

    # Row layout:
    #   row 1 — 2-column summary tables (R2)
    #   row 2 — naive energy flows
    #   row 3 — optimised energy flows (with R1 grid bars)
    #   rows 4..(3+n_soc_rows) — SOC per device (with R3 shading)
    #   last row — data table (with R4 flags, R5 row colouring)
    n_chart_rows = 2 + n_soc_rows
    n_total_rows = 1 + n_chart_rows + 1  # 1 summary + charts + 1 table

    # Build the subplot specs matrix (cols=2 throughout).
    # Summary row: two table cells side by side.
    # All other rows: colspan=2 to span the full width.
    specs: list[list[dict[str, Any]]] = [
        [{"type": "table"}, {"type": "table"}],               # row 1: summary tables
        [{"secondary_y": False, "colspan": 2}, None],         # row 2: naive energy flows
        [{"secondary_y": False, "colspan": 2}, None],         # row 3: optimised energy flows
        *[[{"secondary_y": True, "colspan": 2}, None]] * n_soc_rows,  # SOC rows
        [{"type": "table", "colspan": 2}, None],              # data table
    ]

    # subplot_titles: flat array, 2*n_total_rows entries, left-to-right top-to-bottom.
    # Only the first column entry is shown for colspan rows.
    opt_title = (
        f"Optimised — mimirheim schedule  "
        f"[{out.get('strategy', '?')} | {out.get('solve_status', '?')} | "
        f"naive {out.get('naive_cost_eur', 0):.4f} \u20ac \u2192 "
        f"raw {out.get('optimised_cost_eur', 0):.4f} \u20ac "
        f"\u2212 credit {out.get('soc_credit_eur', 0):.4f} \u20ac "
        f"= effective {out.get('optimised_cost_eur', 0) - out.get('soc_credit_eur', 0):.4f} \u20ac]"
    )
    subplot_titles: list[str] = [
        "Economic summary", "Exchange \u0026 self-sufficiency",     # row 1
        "Unoptimised \u2014 no storage dispatch (naive)", "",       # row 2
        opt_title, "",                                              # row 3
        *(name for name in (device_meta if device_meta else ["Prices"]) for _ in (0, 1)),
        "Step-by-step data", "",                                    # data table
    ]

    # Row heights: summary and data table are taller; chart rows equal height.
    summary_h = 0.6
    chart_h = 1.0
    table_h = max(2.0, len(schedule) * 0.04)
    total_h = summary_h + n_chart_rows * chart_h + table_h
    row_heights = (
        [summary_h / total_h]
        + [chart_h / total_h] * n_chart_rows
        + [table_h / total_h]
    )

    _VSPACING = 0.035
    fig = make_subplots(
        rows=n_total_rows,
        cols=2,
        shared_xaxes=False,
        subplot_titles=subplot_titles,
        specs=specs,
        row_heights=row_heights,
        vertical_spacing=_VSPACING,
        column_widths=[0.5, 0.5],
    )
    row_domains = _compute_row_domains(row_heights, _VSPACING)

    # ---- Row 1: Summary tables (R2) ----
    left_table, right_table = _build_summary_tables(inp, out, schedule)
    fig.add_trace(left_table, row=1, col=1)
    fig.add_trace(right_table, row=1, col=2)

    # ---- Rows 2 & 3: Energy flows ----
    naive_traces, opt_traces = _build_energy_flows_traces(inp, out, xs)
    # legend numbering: legend = naive (row 2), legend2 = optimised (row 3)
    for tr in naive_traces:
        tr.legend = "legend"
        fig.add_trace(tr, row=2, col=1)
    for tr in opt_traces:
        tr.legend = "legend2"
        fig.add_trace(tr, row=3, col=1)

    # Zero reference lines for the energy flow charts.
    # Use add_shape with xref="paper" to avoid the subplot-not-empty check
    # that fires when one subplot row has a Table trace in col 2.
    fig.add_shape(
        type="line",
        xref="paper",
        yref="y3",
        x0=0,
        x1=1,
        y0=0,
        y1=0,
        line=dict(color="black", width=0.8),
    )
    fig.add_shape(
        type="line",
        xref="paper",
        yref="y5",
        x0=0,
        x1=1,
        y0=0,
        y1=0,
        line=dict(color="black", width=0.8),
    )
    fig.update_yaxes(title_text="kWh / step", row=2, col=1)
    fig.update_yaxes(title_text="kWh / step", row=3, col=1)

    # ---- Rows 4..(3+n_soc_rows): SOC vs prices (with R3 shading) ----
    device_colors = {"battery": "#2255cc", "ev_charger": "#9933cc"}
    soc_trace_indices: dict[str, int] = {}

    if device_meta:
        for idx, (name, m) in enumerate(device_meta.items()):
            row = 4 + idx
            color = device_colors.get(m["dtype"], "#555555")
            cap = m["capacity_kwh"]
            soc_kwh_list = soc_histories[name]
            soc_pct = [
                round(v / cap * 100.0, 1) if cap > 0 else 0.0
                for v in soc_kwh_list[1:]
            ]
            # soc_kwh_list[i+1] is the SOC after step i completes, so it
            # belongs at xs[i+1] (the step-end boundary), not xs[i] (start).
            # Build an end-time list: xs[1:] plus one extra timestamp beyond
            # the last step.
            if xs:
                _last_end = (
                    datetime.fromisoformat(xs[-1].replace("Z", "+00:00"))
                    + timedelta(minutes=STEP_MINUTES)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                soc_xs = (xs[1:] + [_last_end])[: len(soc_pct)]
            else:
                soc_xs = []
            soc_legend = f"legend{3 + idx}"

            soc_trace_indices[name] = len(fig.data)
            fig.add_trace(
                go.Scatter(
                    x=soc_xs,
                    y=soc_pct,
                    name=f"{name} SOC (%)",
                    fill="tozeroy",
                    fillcolor=f"rgba({_hex_to_rgb(color)},0.12)",
                    line=dict(color=color, width=2),
                    legend=soc_legend,
                    hovertemplate="%{y:.1f}%<extra>" + name + " SOC</extra>",
                ),
                secondary_y=False,
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=xs[: len(import_prices)],
                    y=import_prices,
                    name="Import \u20ac/kWh",
                    line=dict(color="#cc3300", width=2.5),
                    legend=soc_legend,
                    hovertemplate="%{y:.4f} \u20ac/kWh<extra>import</extra>",
                ),
                secondary_y=True,
                row=row,
                col=1,
            )
            fig.add_trace(
                go.Scatter(
                    x=xs[: len(export_prices)],
                    y=export_prices,
                    name="Export \u20ac/kWh",
                    line=dict(color="#33aa44", width=2.5),
                    legend=soc_legend,
                    hovertemplate="%{y:.4f} \u20ac/kWh<extra>export</extra>",
                ),
                secondary_y=True,
                row=row,
                col=1,
            )
            fig.update_yaxes(
                title_text="SOC (%)", secondary_y=False, row=row, col=1
            )
            fig.update_yaxes(
                title_text="\u20ac/kWh",
                secondary_y=True,
                showgrid=False,
                row=row,
                col=1,
            )
    else:
        # No dispatchable devices — show prices only.
        row = 4
        fig.add_trace(
            go.Scatter(
                x=xs[: len(import_prices)],
                y=import_prices,
                name="Import \u20ac/kWh",
                line=dict(color="#cc3300", width=2),
                legend="legend3",
            ),
            row=row,
            col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=xs[: len(export_prices)],
                y=export_prices,
                name="Export \u20ac/kWh",
                line=dict(color="#33aa44", width=2),
                legend="legend3",
            ),
            row=row,
            col=1,
        )
        fig.update_yaxes(title_text="\u20ac/kWh", row=row, col=1)

    # ---- R3: Closed-loop shading bands ----
    # Shading requires the xaxis reference that Plotly assigned to each SOC trace.
    # We retrieve it from the trace object after it has been added to the figure.
    all_shapes: list[dict] = []
    all_annotations: list[dict] = []

    if device_meta:
        for idx, (name, m) in enumerate(device_meta.items()):
            soc_row_idx = 1 + idx  # 0-based, within the SOC rows section
            chart_row_number = idx  # 0-based chart row after the energy-flow rows

            # Row index in row_domains: 0=summary, 1=naive, 2=optimised, 3+idx=SOC
            domain_row_idx = 3 + idx
            if domain_row_idx < len(row_domains):
                y0, y1 = row_domains[domain_row_idx]
            else:
                continue

            trace_idx = soc_trace_indices.get(name)
            if trace_idx is None or trace_idx >= len(fig.data):
                continue
            xaxis_ref = fig.data[trace_idx].xaxis or "x"

            shapes, annotations = _closed_loop_shapes_and_annotations(
                schedule, name, xs, xaxis_ref, y0, y1
            )
            all_shapes.extend(shapes)
            all_annotations.extend(annotations)

    # ---- Last row: Data table (R4 flags, R5 row colouring) ----
    table_row = n_total_rows
    fig.add_trace(
        _build_data_table(inp, out, schedule, xs, device_meta, soc_histories),
        row=table_row,
        col=1,
    )

    # ---- Global layout ----
    total_px = 300 + 500 * n_chart_rows + max(400, len(schedule) * 25)

    legend_layout: dict[str, Any] = {}
    # Row 1 is summary (no legend). Rows 2 and 3 are energy flow charts.
    # domain_row_idx for naive=1, optimised=2, SOC starts at 3.
    energy_rows = [1, 2]  # 0-based indices in row_domains
    soc_rows_start = 3

    for i, domain_row_idx in enumerate(energy_rows):
        legend_key = "legend" if i == 0 else f"legend{i + 1}"
        if domain_row_idx < len(row_domains):
            y_bottom = row_domains[domain_row_idx][0]
            legend_layout[legend_key] = dict(
                orientation="h",
                x=0.0,
                y=y_bottom - 0.005,
                yanchor="top",
                xanchor="left",
            )

    for idx in range(len(device_meta) if device_meta else 1):
        legend_key = f"legend{3 + idx}"
        domain_row_idx = soc_rows_start + idx
        if domain_row_idx < len(row_domains):
            y_bottom = row_domains[domain_row_idx][0]
            legend_layout[legend_key] = dict(
                orientation="h",
                x=0.0,
                y=y_bottom - 0.005,
                yanchor="top",
                xanchor="left",
            )

    fig.update_layout(
        barmode="relative",
        title=(
            f"mimirheim analysis \u2014 {inp.get('solve_time_utc', '')} | "
            f"{out.get('strategy', '?')} | {out.get('solve_status', '?')}"
        ),
        height=total_px,
        hovermode="x unified",
        shapes=all_shapes,
        annotations=(list(fig.layout.annotations or []) + all_annotations),
        **legend_layout,
    )
    return fig


# ---------------------------------------------------------------------------
# R2: Summary dashboard tables
# ---------------------------------------------------------------------------


def _build_summary_tables(
    inp: dict, out: dict, schedule: list[dict]
) -> tuple[go.Table, go.Table]:
    """Build the two summary tables for the top of the report.

    Returns a ``(left_table, right_table)`` tuple.

    The left table is the economic summary: solve time, strategy, status,
    horizon, naive cost, raw optimised cost, SOC credit, effective cost,
    and saving vs naive.

    The right table is the exchange and self-sufficiency summary: totals for
    import, export, self-consumption, and self-sufficiency, plus a row per
    dispatchable device showing charged and discharged energy.

    Args:
        inp: Parsed input dump JSON.
        out: Parsed output dump JSON.
        schedule: List of schedule step dicts from the output.

    Returns:
        A tuple ``(left_table, right_table)`` of ``go.Table`` traces.
    """
    eco = compute_economic_metrics(out)
    naive = eco.naive_cost_eur
    optimised = eco.optimised_cost_eur
    credit = eco.soc_credit_eur
    effective = eco.effective_cost_eur
    saving = eco.saving_eur

    n_steps = len(schedule)
    horizon_h = n_steps * STEP_HOURS

    dispatch_suppressed = out.get("dispatch_suppressed", False)

    left_headers = ["Metric", "Value"]
    left_values = [
        [
            "Solve time",
            "Strategy",
            "Status",
            "Horizon",
            "Naive cost",
            "Raw optimised",
            "SOC credit",
            "Effective cost",
            "Saving vs naive",
        ],
        [
            inp.get("solve_time_utc", "\u2014"),
            out.get("strategy", "\u2014"),
            out.get("solve_status", "\u2014"),
            f"{horizon_h:.2f} h ({n_steps} steps)",
            f"{naive:.4f} \u20ac",
            f"{optimised:.4f} \u20ac",
            f"{credit:.4f} \u20ac",
            f"{effective:.4f} \u20ac",
            f"{saving:.4f} \u20ac",
        ],
    ]
    # Bold the last two rows (effective cost and saving).
    left_bold_mask = [False] * 7 + [True, True]

    left_table = go.Table(
        header=dict(
            values=left_headers,
            fill_color="#2255cc",
            font=dict(color="white", size=11),
            align=["left", "right"],
            line_color="#1144aa",
        ),
        cells=dict(
            values=left_values,
            fill_color=[
                ["#f5f5f5"] * 7 + ["#e8eaf6"] * 2,
                ["#ffffff"] * 7 + ["#e8eaf6"] * 2,
            ],
            font=dict(
                size=11,
                color=[
                    ["#333333"] * 7 + ["#1a237e"] * 2,
                    ["#333333"] * 7 + ["#1a237e"] * 2,
                ],
            ),
            align=["left", "right"],
            height=22,
        ),
    )

    # ------------------------------------------------------------------
    # Right table: exchange and self-sufficiency
    # ------------------------------------------------------------------
    m = compute_schedule_metrics(schedule)
    total_import_kwh = m.grid_import_kwh
    total_export_kwh = m.grid_export_kwh
    pv_total_kwh = m.pv_total_kwh
    self_consumption_pct = m.self_consumption_pct
    self_suf_pct = m.self_sufficiency_pct

    right_rows = [
        ("Total import", f"{total_import_kwh:.3f} kWh"),
        ("Total export", f"{total_export_kwh:.3f} kWh"),
        ("Self-consumption", f"{self_consumption_pct:.1f}%"),
        ("Self-sufficiency", f"{self_suf_pct:.1f}%"),
        (
            "Dispatch suppressed",
            "yes" if dispatch_suppressed else "no",
        ),
    ]

    # Per-device charge/discharge totals.
    device_meta = _build_device_meta(inp)
    for name, m in device_meta.items():
        charge_kwh = sum(
            max(0.0, -sp.get("kw", 0.0)) * STEP_HOURS
            for s in schedule
            for n2, sp in s.get("devices", {}).items()
            if n2 == name
        )
        discharge_kwh = sum(
            max(0.0, sp.get("kw", 0.0)) * STEP_HOURS
            for s in schedule
            for n2, sp in s.get("devices", {}).items()
            if n2 == name
        )
        right_rows.append(
            (f"{name}", f"charge {charge_kwh:.2f} kWh / discharge {discharge_kwh:.2f} kWh")
        )

    right_table = go.Table(
        header=dict(
            values=["Metric", "Value"],
            fill_color="#2255cc",
            font=dict(color="white", size=11),
            align=["left", "right"],
            line_color="#1144aa",
        ),
        cells=dict(
            values=[
                [r[0] for r in right_rows],
                [r[1] for r in right_rows],
            ],
            fill_color=[
                ["#f5f5f5" if i % 2 == 0 else "#ffffff" for i in range(len(right_rows))],
                ["#f5f5f5" if i % 2 == 0 else "#ffffff" for i in range(len(right_rows))],
            ],
            align=["left", "right"],
            font=dict(size=11),
            height=22,
        ),
    )

    return left_table, right_table


# ---------------------------------------------------------------------------
# R1: Energy flow traces with grid bars and net exchange line
# ---------------------------------------------------------------------------


def _build_energy_flows_traces(
    inp: dict,
    out: dict,
    xs: list[str],
) -> tuple[list[go.BaseTraceType], list[go.BaseTraceType]]:
    """Build chart traces for the naive and optimised energy-flow subplots.

    R1 additions to the optimised chart:
        - ``Grid import`` bar (positive, colour #cc3300): grid_import_kw * STEP_HOURS.
        - ``Grid export`` bar (negative, colour #33aa44): -(grid_export_kw * STEP_HOURS).
        - Net exchange line overlay (colour #ff6600): grid_import - grid_export per step.

    The naive chart does not include grid bars. With no storage dispatch the
    grid balance is determined entirely by PV minus base load; explicit grid
    bars would add no information and would clutter the chart.

    Args:
        inp: Parsed input dump JSON.
        out: Parsed output dump JSON.
        xs: Timestamp strings aligned to the schedule steps.

    Returns:
        A tuple ``(naive_traces, optimised_traces)``, each a list of Plotly
        traces ready to be added to their respective subplot rows.
    """
    schedule = out.get("schedule", [])
    n = len(inp["horizon_prices"])

    pv_kwh = [v * STEP_HOURS for v in inp["pv_forecast"]]
    base_kwh = [v * STEP_HOURS for v in inp["base_load_forecast"]]

    battery_discharge: dict[str, list[float]] = {}
    battery_charge: dict[str, list[float]] = {}
    ev_discharge: dict[str, list[float]] = {}
    ev_charge: dict[str, list[float]] = {}
    # Deferrable load consumption per device (positive kWh = load is running).
    # kw in the schedule is negative when consuming; we negate to get a
    # positive value for the bar chart so it stacks with the other demand bars.
    deferrable_kwh: dict[str, list[float]] = {}
    opt_pv: list[float] = []
    opt_base: list[float] = []
    grid_import_kwh: list[float] = []
    grid_export_kwh: list[float] = []

    for step in schedule:
        g_imp = step.get("grid_import_kw", 0.0) * STEP_HOURS
        g_exp = step.get("grid_export_kw", 0.0) * STEP_HOURS
        grid_import_kwh.append(g_imp)
        grid_export_kwh.append(g_exp)
        for name, sp in step.get("devices", {}).items():
            kw = sp.get("kw", 0.0)
            kwh = kw * STEP_HOURS
            dtype = sp.get("type", "")
            if dtype == "pv":
                opt_pv.append(kwh)
            elif dtype == "static_load":
                opt_base.append(kwh)
            elif dtype == "battery":
                battery_discharge.setdefault(name, []).append(max(0.0, kwh))
                battery_charge.setdefault(name, []).append(min(0.0, kwh))
            elif dtype == "ev_charger":
                ev_discharge.setdefault(name, []).append(max(0.0, kwh))
                ev_charge.setdefault(name, []).append(min(0.0, kwh))
            elif dtype == "deferrable_load":
                # kw is negative while the load is running; negate it so the
                # bar represents the positive consumption in kWh.
                deferrable_kwh.setdefault(name, []).append(max(0.0, -kwh))

    if not opt_pv:
        opt_pv = list(pv_kwh[: len(xs)])
    if not opt_base:
        opt_base = [-b for b in base_kwh[: len(xs)]]

    opt_base_pos = [-v for v in opt_base]

    battery_colors = ["#2255cc", "#5588ff", "#88aaff"]
    ev_colors = ["#9933cc", "#cc66ff"]

    naive_traces: list[go.BaseTraceType] = [
        go.Bar(
            x=xs[:n],
            y=base_kwh[:n],
            name="Base load",
            marker_color="#aaaaaa",
        ),
        go.Bar(
            x=xs[:n],
            y=[-v for v in pv_kwh[:n]],
            name="PV generation",
            marker_color="#f0a500",
        ),
    ]

    opt_traces: list[go.BaseTraceType] = [
        go.Bar(
            x=xs[: len(opt_base_pos)],
            y=opt_base_pos,
            name="Base load",
            marker_color="#aaaaaa",
        ),
    ]

    for i, (name, vals) in enumerate(battery_charge.items()):
        color = battery_colors[i % len(battery_colors)]
        opt_traces.append(
            go.Bar(
                x=xs[: len(vals)],
                y=[-v for v in vals],
                name=f"{name} charge",
                marker_color=color,
                legendgroup=f"bat_{name}",
            )
        )
    for i, (name, vals) in enumerate(ev_charge.items()):
        color = ev_colors[i % len(ev_colors)]
        opt_traces.append(
            go.Bar(
                x=xs[: len(vals)],
                y=[-v for v in vals],
                name=f"{name} charge",
                marker_color=color,
                legendgroup=f"ev_{name}",
            )
        )
    opt_traces.append(
        go.Bar(
            x=xs,
            y=[-v for v in opt_pv],
            name="PV generation",
            marker_color="#f0a500",
        )
    )
    for i, (name, vals) in enumerate(battery_discharge.items()):
        color = battery_colors[i % len(battery_colors)]
        opt_traces.append(
            go.Bar(
                x=xs[: len(vals)],
                y=[-v for v in vals],
                name=f"{name} discharge",
                marker_color=color,
                legendgroup=f"bat_{name}",
            )
        )
    for i, (name, vals) in enumerate(ev_discharge.items()):
        color = ev_colors[i % len(ev_colors)]
        opt_traces.append(
            go.Bar(
                x=xs[: len(vals)],
                y=[-v for v in vals],
                name=f"{name} discharge",
                marker_color=color,
                legendgroup=f"ev_{name}",
            )
        )

    # Deferrable load consumption bars (positive = consuming, stacks with
    # other demand bars). Each device gets its own bar trace at a distinct
    # amber/orange shade so the operator can see which load is running and when.
    deferrable_colors = ["#e67e22", "#f39c12", "#d35400"]
    for i, (name, vals) in enumerate(deferrable_kwh.items()):
        color = deferrable_colors[i % len(deferrable_colors)]
        opt_traces.append(
            go.Bar(
                x=xs[: len(vals)],
                y=vals,
                name=name,
                marker_color=color,
                legendgroup=f"dl_{name}",
            )
        )

    # R1: Net exchange line (grid_import - grid_export). Shown as a solid black
    # line rather than bars so it does not compete visually with the energy-flow
    # stacks. Grid import and export bars are omitted; the net value is the
    # operationally relevant quantity.
    #
    # line.shape="spline" draws cubic spline curves through the exact data
    # points, rounding corners without altering the underlying values.
    if grid_import_kwh and grid_export_kwh:
        n_net = min(len(grid_import_kwh), len(grid_export_kwh))
        net_kwh = [
            grid_import_kwh[i] - grid_export_kwh[i] for i in range(n_net)
        ]
        opt_traces.append(
            go.Scatter(
                x=xs[:n_net],
                y=net_kwh,
                name="Net exchange",
                line=dict(color="#F80000", width=2, shape="spline", smoothing=1.0, dash="dot"),
                mode="lines",
                showlegend=True,
                hovertemplate="%{y:.3f} kWh<extra>net exchange</extra>",
            )
        )

    return naive_traces, opt_traces


# ---------------------------------------------------------------------------
# R3: Closed-loop shading bands
# ---------------------------------------------------------------------------


def _closed_loop_shapes_and_annotations(
    schedule: list[dict],
    name: str,
    xs: list[str],
    xaxis_ref: str,
    y0_paper: float,
    y1_paper: float,
) -> tuple[list[dict], list[dict]]:
    """Build closed-loop shading rectangles and annotations for one device row.

    Emits one rectangle per contiguous run of closed-loop steps. Two modes
    are handled:

    ZEX (zero-exchange) mode: any truthy value of ``zero_exchange_active``,
    ``zero_export_mode`` (legacy field), or ``exchange_mode`` in the device's
    step entry. Shaded light indigo, labelled ``"ZEX"``.

    LB (load-balance) mode: ``loadbalance_active=True`` in the device's step
    entry. Shaded light purple, labelled ``"LB"``.

    Shapes use ``xref=xaxis_ref`` so that x-coordinates match the axis data
    (timestamp strings). The y domain uses ``yref="paper"`` so that shading
    fills the full height of the row regardless of the current y-axis range.
    A small inset is applied so the shading does not bleed into adjacent rows.

    Args:
        schedule: List of schedule step dicts from the output.
        name: Device name to extract from each step's ``devices`` dict.
        xs: Timestamp strings aligned to the schedule steps.
        xaxis_ref: The Plotly axis reference string (e.g. ``"x3"``) for
            the SOC subplot row.
        y0_paper: Bottom paper-coordinate of the subplot row (0 to 1).
        y1_paper: Top paper-coordinate of the subplot row (0 to 1).

    Returns:
        A tuple ``(shapes, annotations)`` ready to merge into
        ``fig.layout.shapes`` and ``fig.layout.annotations``.
    """
    # Margin within the row to prevent shading bleeding into adjacent rows.
    inset = (y1_paper - y0_paper) * 0.04
    y0 = y0_paper + inset
    y1 = y1_paper - inset

    shapes: list[dict] = []
    annotations: list[dict] = []

    # Build per-step flag arrays.
    zex_flags: list[bool] = []
    lb_flags: list[bool] = []
    for step in schedule:
        dev = step.get("devices", {}).get(name, {})
        zex = bool(
            dev.get("zero_exchange_active")
            or dev.get("zero_export_mode")
            or dev.get("exchange_mode")
        )
        lb = bool(dev.get("loadbalance_active"))
        zex_flags.append(zex)
        lb_flags.append(lb)

    def _emit_runs(
        flags: list[bool],
        fill_color: str,
        label: str,
    ) -> None:
        """Scan flags for contiguous True runs and emit one shape per run."""
        n = len(flags)
        i = 0
        while i < n:
            if not flags[i]:
                i += 1
                continue
            # Start of a run.
            start = i
            while i < n and flags[i]:
                i += 1
            end = i - 1  # inclusive
            x0_ts = xs[start] if start < len(xs) else None
            # For x1 we want the end of the last step's interval.
            # Each step is STEP_MINUTES wide. If the end step has a timestamp
            # string, shift it by one step forward; otherwise use the same ts.
            if end < len(xs):
                try:
                    dt_end = datetime.fromisoformat(
                        xs[end].replace("Z", "+00:00")
                    )
                    dt_x1 = dt_end + timedelta(minutes=STEP_MINUTES)
                    x1_ts = dt_x1.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, AttributeError):
                    x1_ts = xs[end]
            else:
                x1_ts = xs[-1] if xs else None

            if x0_ts is None or x1_ts is None:
                continue

            shapes.append(
                dict(
                    type="rect",
                    xref=xaxis_ref,
                    yref="paper",
                    x0=x0_ts,
                    x1=x1_ts,
                    y0=y0,
                    y1=y1,
                    fillcolor=fill_color,
                    layer="below",
                    line_width=0,
                )
            )

            # Midpoint x for annotation (use start step).
            mid_idx = (start + end) // 2
            if mid_idx < len(xs):
                annotations.append(
                    dict(
                        x=xs[mid_idx],
                        y=y1 - (y1 - y0) * 0.08,
                        xref=xaxis_ref,
                        yref="paper",
                        text=label,
                        showarrow=False,
                        font=dict(size=9, color="#444444"),
                        xanchor="center",
                        yanchor="top",
                    )
                )

    _emit_runs(zex_flags, "rgba(34,85,204,0.10)", "ZEX")
    _emit_runs(lb_flags, "rgba(153,51,204,0.10)", "LB")

    return shapes, annotations


# ---------------------------------------------------------------------------
# R4 + R5: Data table with flag columns and row colour-coding
# ---------------------------------------------------------------------------


def _build_data_table(
    inp: dict,
    out: dict,
    schedule: list[dict],
    xs: list[str],
    device_meta: dict[str, dict],
    soc_histories: dict[str, list[float]],
) -> go.Table:
    """Build a Plotly Table trace with one row per schedule step.

    R4 addition: ZEX and LB flag columns per dispatchable device.
    ``{name} ZEX`` and ``{name} LB`` columns appear after the SOC columns
    for each device. Cells with ``"yes"`` are highlighted (#c8e6c9 for ZEX,
    #e1bee7 for LB).

    R5 addition: Row colour-coding by economic state (5-tier priority):
        1. Any device in closed-loop mode (ZEX or LB) — light indigo #e8eaf6
        2. Grid export > 0.05 kW — light green #e8f5e9
        3. Grid import > 0.05 kW, price >= 75th percentile — light amber #fff3e0
        4. Grid import > 0.05 kW (otherwise) — light pink #fce4ec
        5. Default — alternating #f9f9f9 / #ffffff

    The 75th-percentile import price threshold is computed from
    ``inp["horizon_prices"]`` at render time.

    Args:
        inp: Parsed input dump JSON.
        out: Parsed output dump JSON.
        schedule: List of schedule step dicts from the output.
        xs: Human-readable x-axis labels aligned to schedule steps.
        device_meta: Per-device metadata from ``_build_device_meta``.
        soc_histories: Per-device SOC histories from ``_reconstruct_soc``.

    Returns:
        A ``go.Table`` trace ready to add to the figure.
    """
    import_prices = inp["horizon_prices"]
    export_prices = inp["horizon_export_prices"]
    confidence = inp.get("horizon_confidence", [None] * len(import_prices))
    pv_kw = inp.get("pv_forecast", [0.0] * len(import_prices))
    base_kw = inp.get("base_load_forecast", [0.0] * len(import_prices))

    # Compute the 75th-percentile import price threshold for R5 colouring.
    valid_prices = [p for p in import_prices if p is not None]
    if len(valid_prices) >= 4:
        sorted_prices = sorted(valid_prices)
        p75_idx = int(len(sorted_prices) * 0.75)
        price_p75 = sorted_prices[min(p75_idx, len(sorted_prices) - 1)]
    else:
        price_p75 = float("inf")  # no high-price colouring with < 4 steps

    # Column headers.
    headers = [
        "Time",
        "Import<br>\u20ac/kWh",
        "Export<br>\u20ac/kWh",
        "Conf.",
        "PV fc<br>kW",
        "PV fc<br>kWh",
        "Load fc<br>kW",
        "Load fc<br>kWh",
    ]
    for name, m in device_meta.items():
        headers += [
            f"{name}<br>AC kW",
            f"{name}<br>AC kWh",
            f"{name}<br>cell kWh",
            f"{name}<br>eff %",
            f"{name}<br>SOC kWh",
            f"{name}<br>SOC %",
            f"{name}<br>ZEX",   # R4
        ]
        if m["dtype"] == "ev_charger":
            headers.append(f"{name}<br>LB")  # R4: EV only
    # Collect deferrable load names from the schedule. We discover them from the
    # schedule itself rather than from the config so that only devices that
    # actually appear in the solved horizon get columns.
    dl_names: list[str] = []
    for _step in schedule:
        for _name, _sp in _step.get("devices", {}).items():
            if _sp.get("type") == "deferrable_load" and _name not in dl_names:
                dl_names.append(_name)

    # Discover PV array names from the schedule, and identify which arrays
    # have on_off or zero_export capability by inspecting the setpoint fields
    # actually present in the solved steps. Discovery from the schedule (rather
    # than from the config) ensures only arrays that were active in this horizon
    # receive columns, and avoids an import dependency on the config schema.
    pv_names: list[str] = []
    pv_has_power_limit: set[str] = set()
    pv_has_on_off: set[str] = set()
    pv_has_zex: set[str] = set()
    pv_has_curtailed: set[str] = set()
    for _step in schedule:
        for _name, _sp in _step.get("devices", {}).items():
            if _sp.get("type") == "pv":
                if _name not in pv_names:
                    pv_names.append(_name)
                if _sp.get("power_limit_kw") is not None:
                    pv_has_power_limit.add(_name)
                if _sp.get("on_off_active") is not None:
                    pv_has_on_off.add(_name)
                if _sp.get("zero_exchange_active") is not None:
                    pv_has_zex.add(_name)
                if _sp.get("pv_is_curtailed") is not None:
                    pv_has_curtailed.add(_name)

    for name in dl_names:
        headers += [f"{name}<br>kW", f"{name}<br>kWh"]
    for name in pv_names:
        headers += [f"{name}<br>kW", f"{name}<br>kWh"]
        if name in pv_has_power_limit:
            headers.append(f"{name}<br>lim kW")
        if name in pv_has_on_off:
            headers.append(f"{name}<br>on/off")
        if name in pv_has_zex:
            headers.append(f"{name}<br>ZEX")
        if name in pv_has_curtailed:
            headers.append(f"{name}<br>curt")
    headers += ["Grid imp<br>kW", "Grid exp<br>kW"]

    # Build column data lists.
    col_time: list[str] = []
    col_import: list[str] = []
    col_export: list[str] = []
    col_conf: list[str] = []
    col_pv_kw: list[str] = []
    col_pv_kwh: list[str] = []
    col_base_kw: list[str] = []
    col_base_kwh: list[str] = []
    dev_cols: dict[str, dict[str, list]] = {
        name: {
            "ac_kw": [],
            "ac_kwh": [],
            "cell_kwh": [],
            "eff_pct": [],
            "soc_kwh": [],
            "soc_pct": [],
            "zex": [],  # R4
            "lb": [],   # R4 — only populated for ev_charger devices
        }
        for name in device_meta
    }
    col_grid_imp: list[str] = []
    col_grid_exp: list[str] = []
    dl_cols: dict[str, dict[str, list]] = {
        name: {"kw": [], "kwh": []}
        for name in dl_names
    }
    # Per-array PV scheduled output columns.
    pv_sched_cols: dict[str, dict[str, list]] = {
        name: {"kw": [], "kwh": [], "lim_kw": [], "on_off": [], "zex": [], "is_curtailed": []}
        for name in pv_names
    }
    # Cell fill colours for PV flag columns: non-empty string when the flag
    # is active so the row-fill fallback logic can apply the right colour.
    pv_on_off_fill: dict[str, list[str]] = {n: [] for n in pv_has_on_off}
    pv_zex_fill: dict[str, list[str]] = {n: [] for n in pv_has_zex}
    pv_curtailed_fill: dict[str, list[str]] = {n: [] for n in pv_has_curtailed}

    # Per-column cell fill colours for R4 flag columns.
    # These are populated per-row for each device.
    dev_zex_fill: dict[str, list[str]] = {n: [] for n in device_meta}
    # LB fill is only tracked for EV chargers; batteries never have loadbalance_active.
    dev_lb_fill: dict[str, list[str]] = {
        n: [] for n, m in device_meta.items() if m["dtype"] == "ev_charger"
    }

    # R5: per-row base fill colours.
    row_fill: list[str] = []

    for i, step in enumerate(schedule):
        raw_t = step.get("t", xs[i] if i < len(xs) else "")
        try:
            dt = datetime.fromisoformat(str(raw_t).replace("Z", "+00:00"))
            time_label = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        except (ValueError, TypeError):
            time_label = str(raw_t)

        col_time.append(time_label)

        p_imp = import_prices[i] if i < len(import_prices) else None
        p_exp = export_prices[i] if i < len(export_prices) else None
        conf = confidence[i] if i < len(confidence) else None

        col_import.append(f"{p_imp:.4f}" if p_imp is not None else "\u2014")
        col_export.append(f"{p_exp:.4f}" if p_exp is not None else "\u2014")
        col_conf.append(f"{conf:.2f}" if conf is not None else "\u2014")

        pv_val = pv_kw[i] if i < len(pv_kw) else 0.0
        base_val = base_kw[i] if i < len(base_kw) else 0.0
        col_pv_kw.append(f"{pv_val:.3f}")
        col_pv_kwh.append(f"{pv_val * STEP_HOURS:.3f}")
        col_base_kw.append(f"{base_val:.3f}")
        col_base_kwh.append(f"{base_val * STEP_HOURS:.3f}")

        devices_at_step = step.get("devices", {})

        # Determine if any device is in closed-loop mode this step (for R5).
        any_closed_loop = False

        for name, m in device_meta.items():
            kw = devices_at_step.get(name, {}).get("kw", 0.0)
            kwh_ac = kw * STEP_HOURS

            if kw > 0.0:
                eff = m["discharge_eff"]
                cell_kwh = -(kw / eff) * STEP_HOURS
            elif kw < 0.0:
                eff = m["charge_eff"]
                cell_kwh = (-kw) * eff * STEP_HOURS
            else:
                eff = m["charge_eff"]
                cell_kwh = 0.0

            cap = m["capacity_kwh"]
            soc_kwh_val = soc_histories[name][i + 1]
            soc_pct_val = round(soc_kwh_val / cap * 100.0, 1) if cap > 0 else 0.0

            dev_entry = devices_at_step.get(name, {})
            zex_active = bool(
                dev_entry.get("zero_exchange_active")
                or dev_entry.get("zero_export_mode")
                or dev_entry.get("exchange_mode")
            )
            lb_active = bool(dev_entry.get("loadbalance_active"))

            if zex_active or lb_active:
                any_closed_loop = True

            dc = dev_cols[name]
            dc["ac_kw"].append(f"{kw:+.3f}")
            dc["ac_kwh"].append(f"{kwh_ac:+.3f}")
            dc["cell_kwh"].append(
                f"{cell_kwh:+.3f}" if abs(cell_kwh) > 1e-9 else "0.000"
            )
            dc["eff_pct"].append(
                f"{eff * 100.0:.1f}" if abs(kw) > 1e-9 else "\u2014"
            )
            dc["soc_kwh"].append(f"{soc_kwh_val:.3f}")
            dc["soc_pct"].append(f"{soc_pct_val:.1f}")

            # R4: flag values and cell fill colours.
            zex_text = "yes" if zex_active else "no"
            dc["zex"].append(zex_text)
            dev_zex_fill[name].append("#c8e6c9" if zex_active else "")
            if m["dtype"] == "ev_charger":
                lb_text = "yes" if lb_active else "no"
                dc["lb"].append(lb_text)
                dev_lb_fill[name].append("#e1bee7" if lb_active else "")

        for dl_name in dl_names:
            dl_kw = devices_at_step.get(dl_name, {}).get("kw", 0.0)
            dl_cols[dl_name]["kw"].append(f"{dl_kw:.3f}")
            dl_cols[dl_name]["kwh"].append(f"{dl_kw * STEP_HOURS:.3f}")

        for pv_name in pv_names:
            pv_entry = devices_at_step.get(pv_name, {})
            pv_kw_sched = pv_entry.get("kw", 0.0)
            pv_sched_cols[pv_name]["kw"].append(f"{pv_kw_sched:.3f}")
            pv_sched_cols[pv_name]["kwh"].append(f"{pv_kw_sched * STEP_HOURS:.3f}")
            if pv_name in pv_has_power_limit:
                lim = pv_entry.get("power_limit_kw")
                # power_limit_kw is None when the array is off (on_off mode)
                # or when no limit setpoint was computed for this step.
                pv_sched_cols[pv_name]["lim_kw"].append(
                    f"{lim:.3f}" if lim is not None else "\u2014"
                )
            if pv_name in pv_has_on_off:
                on_off_val = pv_entry.get("on_off_active")
                # on_off_active is True when the array is producing, False when
                # mimirheim has switched it off. The text mirrors the MQTT payload.
                on_off_text = "on" if on_off_val else "off"
                pv_sched_cols[pv_name]["on_off"].append(on_off_text)
                # Highlight the cell when the array is off (curtailed).
                pv_on_off_fill[pv_name].append("#ffcdd2" if not on_off_val else "")
            if pv_name in pv_has_zex:
                zex_val = bool(pv_entry.get("zero_exchange_active"))
                pv_sched_cols[pv_name]["zex"].append("yes" if zex_val else "no")
                pv_zex_fill[pv_name].append("#c8e6c9" if zex_val else "")
                if zex_val:
                    any_closed_loop = True
            if pv_name in pv_has_curtailed:
                curt_val = pv_entry.get("pv_is_curtailed")
                # pv_is_curtailed is None for fixed-mode arrays (never reached
                # here because fixed arrays are excluded from pv_has_curtailed).
                # True means mimirheim is limiting output; False means free.
                is_curt = bool(curt_val)
                pv_sched_cols[pv_name]["is_curtailed"].append("yes" if is_curt else "no")
                pv_curtailed_fill[pv_name].append("#ffcdd2" if is_curt else "")

        g_imp = step.get("grid_import_kw", 0.0)
        g_exp = step.get("grid_export_kw", 0.0)
        col_grid_imp.append(f"{g_imp:.3f}")
        col_grid_exp.append(f"{g_exp:.3f}")

        # R5: row colour priority.
        p_imp_val = import_prices[i] if i < len(import_prices) else 0.0
        if any_closed_loop:
            row_fill.append("#e8eaf6")
        elif g_exp > 0.05:
            row_fill.append("#e8f5e9")
        elif g_imp > 0.05 and (p_imp_val is not None and p_imp_val >= price_p75):
            row_fill.append("#fff3e0")
        elif g_imp > 0.05:
            row_fill.append("#fce4ec")
        else:
            row_fill.append("#f9f9f9" if i % 2 == 0 else "#ffffff")

    # Assemble cell data in column order.
    cells_data: list[list] = [
        col_time,
        col_import,
        col_export,
        col_conf,
        col_pv_kw,
        col_pv_kwh,
        col_base_kw,
        col_base_kwh,
    ]
    # Per-column fill colour arrays: most columns use the row_fill base colour;
    # R4 flag columns use their own per-cell colour (falling back to row fill
    # when the flag is inactive, i.e. the per-cell colour is "").
    col_fills: list[list[str]] = [row_fill] * 8

    for name, m in device_meta.items():
        dc = dev_cols[name]
        cells_data += [
            dc["ac_kw"],
            dc["ac_kwh"],
            dc["cell_kwh"],
            dc["eff_pct"],
            dc["soc_kwh"],
            dc["soc_pct"],
            dc["zex"],
        ]
        col_fills += [row_fill] * 6

        # R4: ZEX flag column fill — use highlight when active, else row_fill.
        zex_col_fill = [
            dev_zex_fill[name][i] if dev_zex_fill[name][i] else row_fill[i]
            for i in range(len(row_fill))
        ]
        col_fills += [zex_col_fill]

        # LB column is EV-only.
        if m["dtype"] == "ev_charger":
            cells_data.append(dc["lb"])
            lb_col_fill = [
                dev_lb_fill[name][i] if dev_lb_fill[name][i] else row_fill[i]
                for i in range(len(row_fill))
            ]
            col_fills.append(lb_col_fill)

    for name in dl_names:
        cells_data += [dl_cols[name]["kw"], dl_cols[name]["kwh"]]
        col_fills += [row_fill, row_fill]

    for name in pv_names:
        cells_data += [pv_sched_cols[name]["kw"], pv_sched_cols[name]["kwh"]]
        col_fills += [row_fill, row_fill]
        if name in pv_has_power_limit:
            cells_data.append(pv_sched_cols[name]["lim_kw"])
            col_fills.append(row_fill)
        if name in pv_has_on_off:
            cells_data.append(pv_sched_cols[name]["on_off"])
            on_off_col_fill = [
                pv_on_off_fill[name][i] if pv_on_off_fill[name][i] else row_fill[i]
                for i in range(len(row_fill))
            ]
            col_fills.append(on_off_col_fill)
        if name in pv_has_zex:
            cells_data.append(pv_sched_cols[name]["zex"])
            zex_col_fill = [
                pv_zex_fill[name][i] if pv_zex_fill[name][i] else row_fill[i]
                for i in range(len(row_fill))
            ]
            col_fills.append(zex_col_fill)
        if name in pv_has_curtailed:
            cells_data.append(pv_sched_cols[name]["is_curtailed"])
            curt_col_fill = [
                pv_curtailed_fill[name][i] if pv_curtailed_fill[name][i] else row_fill[i]
                for i in range(len(row_fill))
            ]
            col_fills.append(curt_col_fill)

    cells_data += [col_grid_imp, col_grid_exp]
    col_fills += [row_fill, row_fill]

    n_data_cols = len(cells_data)
    return go.Table(
        header=dict(
            values=headers,
            fill_color="#2255cc",
            font=dict(color="white", size=11),
            align="center",
            line_color="#1144aa",
        ),
        cells=dict(
            values=cells_data,
            fill_color=col_fills,
            align=["center"] + ["right"] * (n_data_cols - 1),
            font=dict(size=10),
            height=22,
        ),
    )


# ---------------------------------------------------------------------------
# Shared utility helpers
# ---------------------------------------------------------------------------


def _timestamps(solve_time_utc: str, n_steps: int) -> list[datetime]:
    """Build a list of UTC datetimes, one per 15-minute step.

    Args:
        solve_time_utc: ISO 8601 UTC datetime string from the input dump.
        n_steps: Number of 15-minute steps in the horizon.

    Returns:
        A list of ``datetime`` objects, one per step, starting at
        ``solve_time_utc``.
    """
    t0 = datetime.fromisoformat(solve_time_utc.replace("Z", "+00:00"))
    return [t0 + timedelta(minutes=STEP_MINUTES * i) for i in range(n_steps)]


def _hex_to_rgb(hex_color: str) -> str:
    """Convert a ``#rrggbb`` hex string to a ``r,g,b`` string for rgba() CSS.

    Args:
        hex_color: Hex colour string, e.g. ``"#2255cc"``.

    Returns:
        A ``"R,G,B"`` string suitable for use inside ``rgba(R,G,B,A)`` in CSS.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{r},{g},{b}"


def _build_device_meta(inp: dict) -> dict[str, dict]:
    """Return per-device metadata extracted from the embedded config section.

    Returns a dict keyed by device name. Each value is a dict with:

    - ``dtype``: ``"battery"`` or ``"ev_charger"``.
    - ``capacity_kwh``: Total cell capacity.
    - ``min_soc_kwh``: Hard lower SOC limit.
    - ``charge_eff``: Representative charge efficiency (0–1).
    - ``discharge_eff``: Representative discharge efficiency (0–1).
    - ``initial_soc``: SOC at solve time from the inputs section (kWh).

    Only EV chargers that are marked available in the inputs are included.

    Args:
        inp: Parsed input dump JSON.

    Returns:
        A dict keyed by device name.
    """
    cfg = inp.get("config", {})
    meta: dict[str, dict] = {}

    for name, bat in cfg.get("batteries", {}).items():
        initial = inp.get("battery_inputs", {}).get(name, {}).get("soc_kwh", 0.0)
        meta[name] = {
            "dtype": "battery",
            "capacity_kwh": bat["capacity_kwh"],
            "min_soc_kwh": bat.get("min_soc_kwh", 0.0),
            "charge_eff": avg_segment_efficiency(
                bat.get("charge_segments"), bat.get("charge_efficiency_curve")
            ),
            "discharge_eff": avg_segment_efficiency(
                bat.get("discharge_segments"), bat.get("discharge_efficiency_curve")
            ),
            "initial_soc": initial,
        }

    for name, ev in cfg.get("ev_chargers", {}).items():
        state = inp.get("ev_inputs", {}).get(name, {})
        if not state.get("available", False):
            continue
        meta[name] = {
            "dtype": "ev_charger",
            "capacity_kwh": ev["capacity_kwh"],
            "min_soc_kwh": ev.get("min_soc_kwh", 0.0),
            "charge_eff": avg_segment_efficiency(ev.get("charge_segments"), None),
            "discharge_eff": avg_segment_efficiency(ev.get("discharge_segments"), None),
            "initial_soc": state.get("soc_kwh", 0.0),
        }

    return meta


def _reconstruct_soc(
    schedule: list[dict], device_meta: dict[str, dict]
) -> dict[str, list[float]]:
    """Reconstruct per-device SOC trajectory using efficiency-corrected energy.

    Sign convention: positive kw = discharging (SOC decreases), negative kw =
    charging (SOC increases).

    Returns a dict of SOC lists, one entry per device. Each list has
    ``len(schedule) + 1`` entries: the initial SOC followed by the
    end-of-step SOC for each step.

    Args:
        schedule: List of schedule step dicts from the output.
        device_meta: Per-device metadata from ``_build_device_meta``.

    Returns:
        A dict keyed by device name. Each value is a list of SOC values in kWh.
    """
    histories: dict[str, list[float]] = {}
    for name, m in device_meta.items():
        eff_c = m["charge_eff"]
        eff_d = m["discharge_eff"]
        soc = m["initial_soc"]
        history = [soc]
        for step in schedule:
            kw = step["devices"].get(name, {}).get("kw", 0.0)
            if kw > 0.0:
                soc -= kw / eff_d * STEP_HOURS
            else:
                soc += (-kw) * eff_c * STEP_HOURS
            history.append(soc)
        histories[name] = history
    return histories


def _compute_row_domains(
    row_heights: list[float], vertical_spacing: float
) -> list[tuple[float, float]]:
    """Return per-row ``(bottom, top)`` paper-coordinate domains, top-to-bottom.

    Rows are ordered top-to-bottom (index 0 is the topmost subplot), matching
    Plotly's ``row=1`` convention.

    Args:
        row_heights: Normalised row heights summing to 1.0, as passed to
            ``make_subplots``.
        vertical_spacing: Fractional gap between rows, as passed to
            ``make_subplots``.

    Returns:
        A list of ``(bottom, top)`` tuples in paper coordinates [0, 1].
    """
    n = len(row_heights)
    available = 1.0 - vertical_spacing * (n - 1)
    actual = [h * available for h in row_heights]
    domains: list[tuple[float, float]] = []
    y_top = 1.0
    for h in actual:
        y_bottom = y_top - h
        domains.append((y_bottom, y_top))
        y_top = y_bottom - vertical_spacing
    return domains
