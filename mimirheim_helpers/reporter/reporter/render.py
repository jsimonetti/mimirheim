"""HTML report renderer for mimirheim solve dumps.

This module is the single source of truth for all HTML report rendering.
Each visual section is an independent Plotly figure, assembled into a
complete HTML page. Plotly JS is loaded once from a shared ``plotly.min.js``
sidecar file rather than inlined.

What this module does:
    - Produce a complete standalone HTML report from a dump pair.
    - Expose ``build_report_html(inp, out) -> str`` as the sole public function.

What this module does not do:
    - Read or write files.
    - Import from ``mimirheim``.
    - Manage inventory or garbage collection.
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Any

import plotly.graph_objects as go

from reporter._render_helpers import (
    STEP_HOURS,
    STEP_MINUTES,
    _build_data_table,
    _build_device_meta,
    _build_energy_flows_traces,
    _closed_loop_shapes_and_annotations,
    _reconstruct_soc,
    _timestamps,
)
from reporter.metrics import compute_economic_metrics, compute_schedule_metrics

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_report_html(inp: dict, out: dict) -> str:
    """Build a complete HTML report from a mimirheim dump pair.

    Each visual section is an independent Plotly figure, rendered into its
    own ``<div>`` and stitched into a single HTML page.  ``plotly.min.js``
    is expected to be in the same directory (i.e. the caller must pass
    ``include_plotlyjs="directory"`` behaviour — the script tag is emitted
    once in the page ``<head>``).

    The timezone-shift script (``_TZ_SCRIPT``) is injected once at the bottom
    of the page and applied to all Plotly graph divs in the document.

    Args:
        inp: Parsed SolveBundle JSON (the ``*_input.json`` dump).
        out: Parsed SolveResult JSON (the ``*_output.json`` dump).

    Returns:
        A complete HTML document string ready to write to a file.
    """
    schedule = out.get("schedule", [])
    n_prices = len(inp["horizon_prices"])
    times = _timestamps(inp["solve_time_utc"], n_prices)
    import_prices = inp["horizon_prices"]
    export_prices = inp["horizon_export_prices"]

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

    # triggered_at_utc is the wall-clock trigger time (not the floored slot
    # boundary). Use it for the human-visible label so the report title matches
    # the dump filename. Falls back to solve_time_utc for older dump files.
    display_time = inp.get("triggered_at_utc") or inp.get("solve_time_utc", "")
    page_title = (
        f"mimirheim report — {display_time} | "
        f"{out.get('strategy', '?')} | {out.get('solve_status', '?')}"
    )

    sections: list[str] = []

    # ------------------------------------------------------------------
    # Section 1: economic summary (plain HTML table — no Plotly)
    # ------------------------------------------------------------------
    sections.append(_render_summary_html(inp, out, schedule))

    # ------------------------------------------------------------------
    # Section 2: naive energy flows
    # ------------------------------------------------------------------
    naive_traces, _ = _build_energy_flows_traces(inp, out, xs)
    naive_fig = go.Figure()
    for tr in naive_traces:
        naive_fig.add_trace(tr)
    naive_fig.update_layout(
        title="Unoptimised — no storage dispatch (naive)",
        barmode="relative",
        height=760,
        margin=dict(t=50, b=80, l=60, r=20),
        yaxis_title="kWh / step",
        legend=dict(orientation="h", x=0, y=-0.1),
        hovermode="x unified",
    )
    naive_fig.add_hline(y=0, line=dict(color="black", width=0.8))
    sections.append(_fig_to_div(naive_fig, "naive-chart"))

    # ------------------------------------------------------------------
    # Section 3: optimised energy flows
    # ------------------------------------------------------------------
    _, opt_traces = _build_energy_flows_traces(inp, out, xs)
    _opt_eco = compute_economic_metrics(out)
    opt_fig = go.Figure()
    for tr in opt_traces:
        opt_fig.add_trace(tr)
    opt_fig.update_layout(
        title=None,
        annotations=[
            dict(
                text="Optimised — mimirheim schedule",
                xref="paper", yref="paper",
                x=0.0, y=1.16,
                xanchor="left", yanchor="bottom",
                showarrow=False,
                font=dict(size=15, color="#2c3e7a"),
            ),
            dict(
                text=(
                    f"{out.get('strategy', '?')} │ {out.get('solve_status', '?')}"
                ),
                xref="paper", yref="paper",
                x=0.0, y=1.09,
                xanchor="left", yanchor="bottom",
                showarrow=False,
                font=dict(size=11, color="#666666"),
            ),
            dict(
                text=(
                    f"naive {_opt_eco.naive_cost_eur:.4f} € → "
                    f"raw {_opt_eco.optimised_cost_eur:.4f} € − "
                    f"credit {_opt_eco.soc_credit_eur:.4f} € = "
                    f"effective {_opt_eco.effective_cost_eur:.4f} €"
                ),
                xref="paper", yref="paper",
                x=0.0, y=1.03,
                xanchor="left", yanchor="bottom",
                showarrow=False,
                font=dict(size=11, color="#333333"),
            ),
        ],
        barmode="relative",
        height=760,
        margin=dict(t=130, b=80, l=60, r=20),
        yaxis_title="kWh / step",
        legend=dict(orientation="h", x=0, y=-0.1),
        hovermode="x unified",
    )
    opt_fig.add_hline(y=0, line=dict(color="black", width=0.8))
    sections.append(_fig_to_div(opt_fig, "opt-chart"))

    # ------------------------------------------------------------------
    # Section 4: SOC vs prices, one figure per device
    # ------------------------------------------------------------------
    device_colors = {"battery": "#2255cc", "ev_charger": "#9933cc"}
    if device_meta:
        for name, m in device_meta.items():
            color = device_colors.get(m["dtype"], "#555555")
            cap = m["capacity_kwh"]
            soc_kwh_list = soc_histories[name]
            soc_pct = [
                round(v / cap * 100.0, 1) if cap > 0 else 0.0
                for v in soc_kwh_list[1:]
            ]
            # soc_kwh_list[i+1] is the SOC after step i completes, so it
            # belongs at the step-end boundary (xs[i+1]), not the start (xs[i]).
            if xs:
                _last_end = (
                    datetime.fromisoformat(xs[-1].replace("Z", "+00:00"))
                    + timedelta(minutes=STEP_MINUTES)
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                soc_xs = (xs[1:] + [_last_end])[: len(soc_pct)]
            else:
                soc_xs = []

            from plotly.subplots import make_subplots as _msp
            soc_fig = _msp(specs=[[{"secondary_y": True}]])
            soc_fig.add_trace(
                go.Scatter(
                    x=soc_xs,
                    y=soc_pct,
                    name=f"{name} SOC (%)",
                    fill="tozeroy",
                    fillcolor=f"rgba({_hex_to_rgb(color)},0.12)",
                    line=dict(color=color, width=2),
                    hovertemplate="%{y:.1f}%<extra>" + name + " SOC</extra>",
                ),
                secondary_y=False,
            )
            soc_fig.add_trace(
                go.Scatter(
                    x=xs[: len(import_prices)],
                    y=import_prices,
                    name="Import €/kWh",
                    line=dict(color="#cc3300", width=2.5),
                    hovertemplate="%{y:.4f} €/kWh<extra>import</extra>",
                ),
                secondary_y=True,
            )
            soc_fig.add_trace(
                go.Scatter(
                    x=xs[: len(export_prices)],
                    y=export_prices,
                    name="Export €/kWh",
                    line=dict(color="#33aa44", width=2.5),
                    hovertemplate="%{y:.4f} €/kWh<extra>export</extra>",
                ),
                secondary_y=True,
            )

            # R3: closed-loop shading
            xaxis_ref = "x"
            shapes, annotations = _closed_loop_shapes_and_annotations(
                schedule, name, xs, xaxis_ref, 0.0, 1.0
            )
            # Convert paper-coord shapes to data-coord equivalents
            # (for a single-subplot figure, yref=paper works fine)
            for sh in shapes:
                sh["yref"] = "paper"
                sh["y0"] = 0.0
                sh["y1"] = 1.0

            # Compute per-step ZEX and LB flags for legend and tooltip traces.
            zex_active_xs: list[str] = []
            zex_active_ys: list[float] = []
            lb_active_xs: list[str] = []
            lb_active_ys: list[float] = []
            for i, step in enumerate(schedule):
                dev = step.get("devices", {}).get(name, {})
                zex = bool(
                    dev.get("zero_exchange_active")
                    or dev.get("zero_export_mode")
                    or dev.get("exchange_mode")
                )
                lb = bool(dev.get("loadbalance_active"))
                # ZEX/LB are properties of the step's active interval, so the
                # hover marker belongs at the step-start timestamp (xs[i]), not
                # the step-end timestamp (soc_xs[i]). The shading rectangles
                # also anchor at xs[i], so this keeps them in sync.
                if i < len(xs):
                    y = soc_pct[i] if i < len(soc_pct) else 0.0
                    if zex:
                        zex_active_xs.append(xs[i])
                        zex_active_ys.append(y)
                    if lb:
                        lb_active_xs.append(xs[i])
                        lb_active_ys.append(y)

            # Legend dummy traces: square marker matching shading colour.
            # x=[None] keeps them off the visible chart area.
            soc_fig.add_trace(
                go.Scatter(
                    x=[None], y=[None],
                    mode="markers",
                    marker=dict(size=12, color="rgba(34,85,204,0.35)", symbol="square"),
                    name="ZEX — zero-exchange active",
                    showlegend=True,
                    hoverinfo="skip",
                ),
                secondary_y=False,
            )
            soc_fig.add_trace(
                go.Scatter(
                    x=[None], y=[None],
                    mode="markers",
                    marker=dict(size=12, color="rgba(153,51,204,0.35)", symbol="square"),
                    name="LB — load-balance active",
                    showlegend=True,
                    hoverinfo="skip",
                ),
                secondary_y=False,
            )

            # Per-step hover traces: visible markers only at active steps so the
            # x-unified tooltip shows the mode label exactly when it is active.
            if zex_active_xs:
                soc_fig.add_trace(
                    go.Scatter(
                        x=zex_active_xs,
                        y=zex_active_ys,
                        mode="markers",
                        marker=dict(size=7, color="rgba(34,85,204,0.55)", symbol="square"),
                        name="ZEX active",
                        showlegend=False,
                        hovertemplate="ZEX — zero-exchange active<extra></extra>",
                    ),
                    secondary_y=False,
                )
            if lb_active_xs:
                soc_fig.add_trace(
                    go.Scatter(
                        x=lb_active_xs,
                        y=lb_active_ys,
                        mode="markers",
                        marker=dict(size=7, color="rgba(153,51,204,0.55)", symbol="square"),
                        name="LB active",
                        showlegend=False,
                        hovertemplate="LB — load-balance active<extra></extra>",
                    ),
                    secondary_y=False,
                )

            soc_fig.update_layout(
                title=f"{name} — SOC vs prices",
                height=600,
                margin=dict(t=50, b=80, l=60, r=60),
                legend=dict(orientation="h", x=0, y=-0.13),
                hovermode="x unified",
                shapes=shapes,
                annotations=annotations,
                yaxis_title="SOC (%)",
                yaxis2_title="€/kWh",
            )
            sections.append(_fig_to_div(soc_fig, f"soc-{name}"))
    else:
        # No devices: just prices
        price_fig = go.Figure()
        price_fig.add_trace(go.Scatter(
            x=xs[: len(import_prices)],
            y=import_prices,
            name="Import €/kWh",
            line=dict(color="#cc3300", width=2),
        ))
        price_fig.add_trace(go.Scatter(
            x=xs[: len(export_prices)],
            y=export_prices,
            name="Export €/kWh",
            line=dict(color="#33aa44", width=2),
        ))
        price_fig.update_layout(
            title="Prices",
            height=560,
            margin=dict(t=50, b=80, l=60, r=20),
            yaxis_title="€/kWh",
            legend=dict(orientation="h", x=0, y=-0.13),
            hovermode="x unified",
        )
        sections.append(_fig_to_div(price_fig, "price-chart"))

    # ------------------------------------------------------------------
    # Section 5: step-by-step data table
    # ------------------------------------------------------------------
    table_trace = _build_data_table(inp, out, schedule, xs, device_meta, soc_histories)
    table_height = max(400, len(schedule) * 23 + 60)
    table_fig = go.Figure(data=[table_trace])
    table_fig.update_layout(
        title="Step-by-step data",
        height=table_height,
        margin=dict(t=50, b=20, l=10, r=10),
    )
    sections.append(_fig_to_div_with_col_selector(table_fig, "data-table"))

    return _assemble_html(page_title, sections)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _fig_to_div(fig: go.Figure, div_id: str) -> str:
    """Serialise a figure to a self-contained ``<div>`` snippet.

    Does not include the Plotly JS library (that is loaded once in the page
    head).  The ``<div>`` wraps the Plotly output in a section container.

    Args:
        fig: A configured Plotly figure.
        div_id: CSS id for the outer ``<section>`` element.

    Returns:
        An HTML string containing a ``<section>`` with a Plotly chart div.
    """
    inner = fig.to_html(full_html=False, include_plotlyjs=False)
    return f'<section class="chart-section" id="{html.escape(div_id)}">{inner}</section>'


def _fig_to_div_with_col_selector(fig: go.Figure, div_id: str) -> str:
    """Like ``_fig_to_div`` but prepends a column-visibility selector toolbar.

    The selector is a button that opens a panel of checkboxes (one per table
    column).  The panel is populated and wired by ``_build_col_selector_script``
    which runs at the bottom of the page after Plotly has fully initialised.

    Args:
        fig: A Plotly Figure containing exactly one ``go.Table`` trace.
        div_id: CSS id for the outer ``<section>`` element.

    Returns:
        An HTML string containing a ``<section>`` with the selector toolbar
        followed by the Plotly chart div.
    """
    inner = fig.to_html(full_html=False, include_plotlyjs=False)
    selector_html = (
        '<div class="col-selector-bar">'
        '<button class="col-selector-btn" id="col-selector-toggle"'
        ' aria-haspopup="true" aria-expanded="false">'
        'Columns &#9660;</button>'
        '<div class="col-selector-panel" id="col-selector-panel" role="dialog"'
        ' aria-label="Column visibility"></div>'
        '</div>'
    )
    return (
        f'<section class="chart-section" id="{html.escape(div_id)}">'
        f'{selector_html}'
        f'{inner}'
        '</section>'
    )


def _render_summary_html(inp: dict, out: dict, schedule: list[dict]) -> str:
    """Render the economic summary and exchange metrics as a plain HTML table pair.

    Using plain HTML avoids any Plotly domain-allocation issues and renders
    immediately without JavaScript.

    Args:
        inp: Parsed input dump JSON.
        out: Parsed output dump JSON.
        schedule: List of schedule step dicts from the output.

    Returns:
        An HTML string containing a two-column summary section.
    """
    eco = compute_economic_metrics(out)
    naive = eco.naive_cost_eur
    raw_opt = eco.optimised_cost_eur
    credit = eco.soc_credit_eur
    effective = eco.effective_cost_eur
    saving = eco.saving_eur

    n_steps = len(schedule)
    horizon_h = n_steps * STEP_HOURS
    dispatch_sup = bool(out.get("dispatch_suppressed", False))

    m = compute_schedule_metrics(schedule)
    total_import = m.grid_import_kwh
    total_export = m.grid_export_kwh
    pv_kwh = m.pv_total_kwh
    load_kwh = m.load_total_kwh
    self_suf = m.self_sufficiency_pct

    def row(label: str, value: str, highlight: bool = False, tooltip: str = "") -> str:
        bg = ' style="background:#e8f5e9"' if highlight else ""
        tip = f' title="{html.escape(tooltip)}"' if tooltip else ""
        lbl_style = ' style="cursor:help;border-bottom:1px dotted #999"' if tooltip else ""
        return (
            f"<tr{bg}>"
            f'<td class="lbl"{tip}{lbl_style}>{html.escape(label)}</td>'
            f'<td class="val">{html.escape(value)}</td>'
            f"</tr>"
        )

    left = (
        "<table class='summary-tbl'>"
        "<thead><tr><th colspan='2'>Economic summary</th></tr></thead>"
        "<tbody>"
        + row("Solve time (UTC)", str(inp.get("triggered_at_utc") or inp.get("solve_time_utc", "—")))
        + row("Strategy", str(out.get("strategy", "—")))
        + row("Solve status", str(out.get("solve_status", "—")))
        + row("Horizon", f"{n_steps} steps ({horizon_h:.2f} h)")
        + row(
            "Dispatch suppressed",
            "yes" if dispatch_sup else "no",
            tooltip=(
                "When the optimised schedule saves less than the configured minimum gain "
                "over the naive baseline, mimirheim discards it and publishes an idle schedule "
                "(all devices at zero setpoint) instead. "
                "'yes' means this solve cycle was below that threshold."
            ),
        )
        + row("Naive cost", f"{naive:.4f} €")
        + row("Raw optimised", f"{raw_opt:.4f} €")
        + row("SOC credit", f"{credit:.4f} €")
        + row("Effective cost", f"{effective:.4f} €")
        + row("Saving vs naive", f"{saving:.4f} €", highlight=saving > 0)
        + "</tbody></table>"
    )

    right = (
        "<table class='summary-tbl'>"
        "<thead><tr><th colspan='2'>Exchange &amp; self-sufficiency</th></tr></thead>"
        "<tbody>"
        + row("Total import", f"{total_import:.3f} kWh")
        + row("Total export", f"{total_export:.3f} kWh")
        + row("PV generation", f"{pv_kwh:.3f} kWh")
        + row("Load served (estimated)", f"{load_kwh:.3f} kWh")
        + row("Self-sufficiency", f"{self_suf:.1f} %", highlight=self_suf > 50)
        + "</tbody></table>"
    )

    return (
        '<section class="summary-section" id="summary">'
        f'<div class="summary-grid">{left}{right}</div>'
        "</section>"
    )


def _assemble_html(title: str, sections: list[str]) -> str:
    """Assemble the full HTML document from a title and a list of section strings.

    Args:
        title: Page title shown in the browser tab and as an ``<h1>``.
        sections: Ordered list of HTML section strings.

    Returns:
        A complete ``<!DOCTYPE html>`` document string.
    """
    tz_fix = _build_multi_div_tz_script()
    col_selector_js = _build_col_selector_script()
    body = "\n".join(sections)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(title)}</title>
  <script src="plotly.min.js"></script>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 0; padding: 16px; background: #f5f5f5; color: #222; }}
    h1 {{ font-size: 1.1rem; margin-bottom: 12px; color: #333; }}
    .chart-section {{ background: #fff; border: 1px solid #ddd; border-radius: 4px; margin-bottom: 16px; overflow: hidden; }}
    .summary-section {{ margin-bottom: 16px; }}
    .summary-grid {{ display: flex; gap: 16px; flex-wrap: wrap; }}
    .summary-tbl {{ border-collapse: collapse; flex: 1; min-width: 280px; background: #fff; border: 1px solid #ddd; border-radius: 4px; overflow: hidden; }}
    .summary-tbl thead th {{ background: #2255cc; color: #fff; text-align: left; padding: 8px 12px; font-size: 0.9rem; }}
    .summary-tbl td {{ padding: 5px 12px; font-size: 0.85rem; border-bottom: 1px solid #eee; }}
    .summary-tbl td.lbl {{ color: #555; }}
    .summary-tbl td.val {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .col-selector-bar {{ position: relative; padding: 8px 12px 0; }}
    .col-selector-btn {{ padding: 4px 12px; background: #2255cc; color: #fff; border: none; border-radius: 3px; cursor: pointer; font-size: 0.85rem; }}
    .col-selector-btn:hover {{ background: #1144aa; }}
    .col-selector-panel {{ display: none; position: absolute; z-index: 100; top: 100%; left: 12px; background: #fff; border: 1px solid #ddd; border-radius: 4px; padding: 10px 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); max-height: 440px; overflow-y: auto; min-width: 220px; }}
    .col-selector-panel.open {{ display: flex; flex-wrap: wrap; gap: 12px; }}
    .col-group {{ min-width: 150px; }}
    .col-group strong {{ display: block; font-size: 0.75rem; color: #2255cc; text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; padding-bottom: 2px; border-bottom: 1px solid #eee; }}
    .col-group label {{ display: flex; align-items: center; gap: 4px; font-size: 0.8rem; cursor: pointer; padding: 1px 0; white-space: nowrap; }}
    .col-selector-actions {{ width: 100%; padding-top: 8px; margin-top: 4px; border-top: 1px solid #eee; display: flex; gap: 8px; }}
    .col-selector-actions button {{ padding: 3px 10px; font-size: 0.75rem; cursor: pointer; background: #f0f0f0; border: 1px solid #ccc; border-radius: 3px; }}
    .col-selector-actions button:hover {{ background: #e0e0e0; }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  {body}
  <script>
  // Apply timezone shift to every Plotly graph div in the page.
  {tz_fix}
  // Column-visibility selector for the step-by-step data table.
  {col_selector_js}
  </script>
</body>
</html>"""


def _build_multi_div_tz_script() -> str:
    """Return a timezone-shift script that operates on all Plotly divs in the page.

    The original ``_TZ_SCRIPT`` from ``render.py`` targets a single
    ``plotly-graph-div`` element (index 0). This version iterates all divs.

    Returns:
        A JavaScript string without ``<script>`` wrappers.
    """
    return r"""
(function() {
    var tzOffsetMs = new Date().getTimezoneOffset() * 60 * 1000;
    if (tzOffsetMs === 0) return;
    var utcRe = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/;
    var tzName = Intl.DateTimeFormat().resolvedOptions().timeZone;

    document.querySelectorAll('.plotly-graph-div').forEach(function(gd) {
        if (!gd.data) return;

        var newXArrays = gd.data.map(function(trace) {
            if (!trace.x || !trace.x.length || typeof trace.x[0] !== 'string') return trace.x;
            if (!utcRe.test(trace.x[0])) return trace.x;
            return trace.x.map(function(t) {
                var d = new Date(t);
                if (isNaN(d.getTime())) return t;
                return new Date(d.getTime() - tzOffsetMs).toISOString().slice(0, 19);
            });
        });
        Plotly.restyle(gd, {x: newXArrays});

        // Shift closed-loop shape x0/x1 so they stay aligned with the
        // shifted trace data. Shapes are generated in UTC; without this
        // correction Plotly auto-extends the x-axis to include both the
        // unshifted shape bounds and the shifted trace data, creating a
        // visible gap at the start of the chart.
        if (gd.layout.shapes && gd.layout.shapes.length) {
            var newShapes = gd.layout.shapes.map(function(sh) {
                var s = Object.assign({}, sh);
                if (s.x0 && typeof s.x0 === 'string' && utcRe.test(s.x0)) {
                    var d = new Date(s.x0);
                    if (!isNaN(d.getTime()))
                        s.x0 = new Date(d.getTime() - tzOffsetMs).toISOString().slice(0, 19) + 'Z';
                }
                if (s.x1 && typeof s.x1 === 'string' && utcRe.test(s.x1)) {
                    var d = new Date(s.x1);
                    if (!isNaN(d.getTime()))
                        s.x1 = new Date(d.getTime() - tzOffsetMs).toISOString().slice(0, 19) + 'Z';
                }
                return s;
            });
            Plotly.relayout(gd, {shapes: newShapes});
        }

        // Shift annotation x-coordinates for the ZEX/LB text labels
        // that are positioned on the time axis (string x, not paper x).
        if (gd.layout.annotations && gd.layout.annotations.length) {
            var newAnnotations = gd.layout.annotations.map(function(ann) {
                var a = Object.assign({}, ann);
                if (a.x && typeof a.x === 'string' && utcRe.test(a.x)) {
                    var d = new Date(a.x);
                    if (!isNaN(d.getTime()))
                        a.x = new Date(d.getTime() - tzOffsetMs).toISOString().slice(0, 19) + 'Z';
                }
                return a;
            });
            Plotly.relayout(gd, {annotations: newAnnotations});
        }

        gd.data.forEach(function(trace, idx) {
            if (trace.type !== 'table') return;
            var timeCol = trace.cells && trace.cells.values && trace.cells.values[0];
            if (!timeCol || !timeCol.length || !utcRe.test(timeCol[0])) return;
            var localTimes = timeCol.map(function(t) {
                var d = new Date(t);
                if (isNaN(d.getTime())) return t;
                var local = new Date(d.getTime() - tzOffsetMs);
                return String(local.getUTCHours()).padStart(2,'0') + ':' +
                       String(local.getUTCMinutes()).padStart(2,'0');
            });
            var newCellValues = trace.cells.values.slice();
            newCellValues[0] = localTimes;
            Plotly.restyle(gd, {'cells.values': [newCellValues]}, [idx]);
        });

        var layoutUpdate = {};
        Object.keys(gd.layout).forEach(function(k) {
            if (!k.match(/^xaxis(\d+)?$/) || !gd.layout[k]) return;
            var existing = (gd.layout[k].title && gd.layout[k].title.text) || '';
            layoutUpdate[k + '.title.text'] = existing
                ? existing + ' (' + tzName + ')' : tzName;
        });
        if (Object.keys(layoutUpdate).length) Plotly.relayout(gd, layoutUpdate);
    });
})();
"""


def _hex_to_rgb(hex_color: str) -> str:
    """Convert a hex colour string (``#rrggbb``) to a comma-separated RGB string.

    Args:
        hex_color: Hex colour string with leading ``#``.

    Returns:
        A string of the form ``"r,g,b"``, e.g. ``"34,85,204"``.
    """
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"{r},{g},{b}"


def _build_col_selector_script() -> str:
    """Return the JavaScript that powers the column-visibility selector.

    This script runs AFTER the timezone-shift script so that the snapshotted
    time-column values are already in local time.

    The script:
    - Reads all column headers from the ``go.Table`` trace inside the
      ``#data-table`` section.
    - Groups columns by category (Core, PV forecast, Load forecast, per-device,
      Grid) and populates the ``#col-selector-panel`` with checkboxes.
    - On checkbox change calls ``Plotly.restyle`` to show only the selected
      columns and saves the selection to a cookie (``hioo_col_sel``, 365-day
      expiry) keyed by the sorted list of column labels.
    - On page load, restores a previously saved selection from the cookie if
      the column set matches the current report.
    - Provides *Select all* / *Deselect all* convenience buttons.

    Returns:
        A raw JavaScript string (no ``<script>`` wrapper).
    """
    return r"""
(function () {
    var section = document.getElementById('data-table');
    if (!section) return;
    var gd = section.querySelector('.plotly-graph-div');
    if (!gd || !gd.data || !gd.data[0] || gd.data[0].type !== 'table') return;
    var trace = gd.data[0];

    // Snapshot column data AFTER the TZ script has shifted the time column.
    var origHeaders = trace.header.values.slice();
    var origCells   = trace.cells.values.map(function (col) { return col.slice(); });
    var rawFill     = (trace.cells.fill && trace.cells.fill.color) || [];
    var origFills   = rawFill.map(function (col) {
        return Array.isArray(col) ? col.slice() : col;
    });
    var origAligns  = Array.isArray(trace.cells.align)
        ? trace.cells.align.slice()
        : origHeaders.map(function () { return 'right'; });

    // ------------------------------------------------------------------
    // Cookie helpers.
    // ------------------------------------------------------------------
    var COOKIE_NAME = 'hioo_col_sel';

    // The cookie value is a JSON object:
    //   { key: <sorted label fingerprint>, hidden: [<label>, ...] }
    // "key" lets us discard a saved selection when the column set changes
    // (different device names, new columns added, etc.).
    function cookieFingerprint() {
        return origHeaders.map(function (h) { return cleanHdr(h); }).sort().join('|');
    }

    function saveCookie(hiddenLabels) {
        var val = JSON.stringify({ key: cookieFingerprint(), hidden: hiddenLabels });
        var exp = new Date();
        exp.setFullYear(exp.getFullYear() + 1);
        document.cookie = COOKIE_NAME + '=' + encodeURIComponent(val) +
            '; expires=' + exp.toUTCString() + '; path=/; SameSite=Lax';
    }

    function loadCookie() {
        var match = document.cookie.split('; ').reduce(function (found, pair) {
            var parts = pair.split('=');
            return parts[0] === COOKIE_NAME ? decodeURIComponent(parts.slice(1).join('=')) : found;
        }, null);
        if (!match) return null;
        try {
            var parsed = JSON.parse(match);
            if (parsed.key !== cookieFingerprint()) return null;  // column set changed
            return parsed.hidden || [];
        } catch (e) { return null; }
    }

    // ------------------------------------------------------------------
    // Group columns into labelled categories.
    // ------------------------------------------------------------------
    // Known device-column metric suffixes (longest first to avoid partial hits).
    var KNOWN_SUFFIXES = [
        'AC kW', 'AC kWh', 'cell kWh', 'eff %', 'SOC kWh', 'SOC %', 'ZEX', 'LB',
        'kW', 'kWh'
    ];

    function cleanHdr(hdr) {
        return hdr.replace(/<br>/gi, ' ').replace(/<[^>]+>/g, '').trim();
    }

    function getGroup(label) {
        if (/^(Time|Import|Export|Conf\.)/.test(label)) return 'Core';
        if (/^PV/.test(label))   return 'PV forecast';
        if (/^Load/.test(label)) return 'Load forecast';
        if (/^Grid/.test(label)) return 'Grid';
        // Device column — strip known metric suffix to recover device name.
        for (var k = 0; k < KNOWN_SUFFIXES.length; k++) {
            var sfx = ' ' + KNOWN_SUFFIXES[k];
            if (label.length > sfx.length && label.slice(-sfx.length) === sfx)
                return label.slice(0, -sfx.length);
        }
        return label;
    }

    var groupMap   = {};
    var groupOrder = [];
    origHeaders.forEach(function (hdr, idx) {
        var label = cleanHdr(hdr);
        var g     = getGroup(label);
        if (!groupMap[g]) { groupMap[g] = []; groupOrder.push(g); }
        groupMap[g].push({ idx: idx, label: label });
    });

    // ------------------------------------------------------------------
    // Populate the panel with checkboxes, restoring cookie state.
    // ------------------------------------------------------------------
    function escHtml(s) {
        return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    var savedHidden = loadCookie() || [];

    var panel = document.getElementById('col-selector-panel');
    var rows  = [];
    groupOrder.forEach(function (g) {
        rows.push('<div class="col-group">');
        rows.push('<strong>' + escHtml(g) + '</strong>');
        groupMap[g].forEach(function (col) {
            var checked = savedHidden.indexOf(col.label) === -1 ? ' checked' : '';
            rows.push(
                '<label><input type="checkbox" class="col-toggle" data-idx="' +
                col.idx + '" data-label="' + escHtml(col.label) + '"' + checked + '> ' +
                escHtml(col.label) + '</label>'
            );
        });
        rows.push('</div>');
    });
    rows.push(
        '<div class="col-selector-actions">' +
        '<button id="col-sel-all">Select all</button>' +
        '<button id="col-desel-all">Deselect all</button>' +
        '</div>'
    );
    panel.innerHTML = rows.join('');

    // Apply saved cookie state immediately (without saving back).
    if (savedHidden.length > 0) applyFilter(false);

    // ------------------------------------------------------------------
    // Toggle button opens / closes the panel.
    // ------------------------------------------------------------------
    var toggleBtn = document.getElementById('col-selector-toggle');
    toggleBtn.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = panel.classList.toggle('open');
        toggleBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    document.addEventListener('click', function (e) {
        if (!section.contains(e.target)) {
            panel.classList.remove('open');
            toggleBtn.setAttribute('aria-expanded', 'false');
        }
    });

    // ------------------------------------------------------------------
    // Select all / deselect all.
    // ------------------------------------------------------------------
    panel.addEventListener('click', function (e) {
        var id = e.target.id;
        if (id === 'col-sel-all') {
            panel.querySelectorAll('.col-toggle').forEach(function (cb) { cb.checked = true; });
            applyFilter(true);
        } else if (id === 'col-desel-all') {
            panel.querySelectorAll('.col-toggle').forEach(function (cb) {
                if (parseInt(cb.dataset.idx, 10) !== 0) cb.checked = false;  // always keep Time
            });
            applyFilter(true);
        }
    });

    // ------------------------------------------------------------------
    // Apply column filter on each checkbox change.
    // ------------------------------------------------------------------
    panel.addEventListener('change', function (e) {
        if (e.target.classList.contains('col-toggle')) applyFilter(true);
    });

    function applyFilter(persist) {
        var checked  = [];
        var hidden   = [];
        panel.querySelectorAll('.col-toggle').forEach(function (cb) {
            var idx = parseInt(cb.dataset.idx, 10);
            if (cb.checked) {
                checked.push(idx);
            } else {
                hidden.push(cb.dataset.label);
            }
        });
        checked.sort(function (a, b) { return a - b; });
        Plotly.restyle(gd, {
            'header.values':    [checked.map(function (i) { return origHeaders[i]; })],
            'cells.values':     [checked.map(function (i) { return origCells[i]; })],
            'cells.fill.color': [checked.map(function (i) { return origFills[i]; })],
            'cells.align':      [checked.map(function (i) { return origAligns[i]; })]
        }, [0]);
        if (persist) saveCookie(hidden);
    }
})();
"""
