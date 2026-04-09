"""Unit tests for deferrable load rendering in _render_helpers.

Tests verify that both _build_energy_flows_traces and _build_data_table
correctly handle deferrable_load devices in the schedule:

- Energy flow chart: a Bar trace per deferrable device appears in
  opt_traces with positive kWh values (negated from the negative kw in the
  schedule).
- Data table: {name} kW and {name} kWh columns appear in the table header
  and their data cells contain the correct values for each step.

If a schedule contains no deferrable loads, both functions must produce the
same output as before (no regression for existing reports).
"""
from __future__ import annotations

import plotly.graph_objects as go
import pytest

from reporter._render_helpers import _build_data_table, _build_energy_flows_traces

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STEP_HOURS = 0.25  # 15 min / 60 min

_XS_2 = ["2026-04-03T15:30:00Z", "2026-04-03T15:45:00Z"]


def _make_inp(n: int = 2) -> dict:
    """Minimal parsed input dump with n steps and no device config."""
    return {
        "horizon_prices": [0.20] * n,
        "horizon_export_prices": [0.05] * n,
        "horizon_confidence": [1.0] * n,
        "pv_forecast": [2.0] * n,
        "base_load_forecast": [1.0] * n,
        "config": {},
    }


def _make_schedule_with_deferrable(
    kw: float = -1.5,
    name: str = "wash",
    n: int = 2,
) -> list[dict]:
    """Schedule with n steps, all containing a deferrable_load device at kw."""
    return [
        {
            "t": _XS_2[i] if i < len(_XS_2) else f"t{i}",
            "grid_import_kw": abs(kw),
            "grid_export_kw": 0.0,
            "devices": {name: {"kw": kw, "type": "deferrable_load"}},
        }
        for i in range(n)
    ]


def _make_out_from_schedule(schedule: list[dict]) -> dict:
    return {"schedule": schedule}


# ---------------------------------------------------------------------------
# _build_energy_flows_traces — deferrable load bar
# ---------------------------------------------------------------------------


def test_energy_flows_deferrable_load_produces_bar_trace() -> None:
    """A running deferrable load produces a Bar trace in opt_traces.

    The trace name must match the device name so the operator can identify
    which load is shown in the legend.
    """
    schedule = _make_schedule_with_deferrable(kw=-1.5, name="wash")
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    _, opt_traces = _build_energy_flows_traces(inp, out, _XS_2)

    bar_names = [t.name for t in opt_traces if isinstance(t, go.Bar)]
    assert "wash" in bar_names, (
        f"Expected 'wash' bar in opt_traces; got names: {bar_names}"
    )


def test_energy_flows_deferrable_load_bar_values_are_positive() -> None:
    """Deferrable load bar y-values are positive (negated from negative kw).

    kw = -1.5 kW → kWh per step = 1.5 × 0.25 = 0.375 kWh (positive, demand).
    """
    kw = -1.5
    schedule = _make_schedule_with_deferrable(kw=kw, name="wash")
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    _, opt_traces = _build_energy_flows_traces(inp, out, _XS_2)

    wash_bar = next(
        (t for t in opt_traces if isinstance(t, go.Bar) and t.name == "wash"),
        None,
    )
    assert wash_bar is not None

    expected = -kw * _STEP_HOURS  # 0.375
    for v in wash_bar.y:
        assert v == pytest.approx(expected, abs=1e-9), (
            f"Expected {expected:.4f} kWh per step, got {v}"
        )


def test_energy_flows_no_deferrable_load_no_extra_bar() -> None:
    """When no deferrable load is in the schedule, no extra bar is added."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.5,
            "grid_export_kw": 0.0,
            "devices": {"bat": {"kw": -1.0, "type": "battery"}},
        }
    ]
    inp = _make_inp(n=1)
    out = _make_out_from_schedule(schedule)

    _, opt_traces = _build_energy_flows_traces(inp, out, _XS_2[:1])

    bar_names = [t.name for t in opt_traces if isinstance(t, go.Bar)]
    # Only the base load bar (and possibly battery bars) should be present.
    assert "wash" not in bar_names


def test_energy_flows_multiple_deferrable_loads_produce_separate_bars() -> None:
    """Two distinct deferrable load devices each produce a separate Bar trace."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 2.0,
            "grid_export_kw": 0.0,
            "devices": {
                "wash": {"kw": -1.5, "type": "deferrable_load"},
                "dishwasher": {"kw": -0.5, "type": "deferrable_load"},
            },
        }
    ]
    inp = _make_inp(n=1)
    out = _make_out_from_schedule(schedule)

    _, opt_traces = _build_energy_flows_traces(inp, out, _XS_2[:1])

    bar_names = [t.name for t in opt_traces if isinstance(t, go.Bar)]
    assert "wash" in bar_names
    assert "dishwasher" in bar_names


# ---------------------------------------------------------------------------
# _build_data_table — deferrable load columns
# ---------------------------------------------------------------------------


def test_data_table_deferrable_load_columns_in_header() -> None:
    """Data table headers include {name} kW and {name} kWh columns for each
    deferrable load found in the schedule."""
    schedule = _make_schedule_with_deferrable(kw=-1.5, name="wash")
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    table = _build_data_table(inp, out, schedule, _XS_2, {}, {})

    headers = table.header.values
    assert "wash<br>kW" in headers, f"Headers: {headers}"
    assert "wash<br>kWh" in headers, f"Headers: {headers}"


def test_data_table_deferrable_load_kw_values() -> None:
    """Data table kW column for a deferrable load contains the schedule kw values."""
    kw = -1.5
    schedule = _make_schedule_with_deferrable(kw=kw, name="wash", n=2)
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    table = _build_data_table(inp, out, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    kw_idx = headers.index("wash<br>kW")
    col_data = table.cells.values[kw_idx]

    assert col_data == [f"{kw:.3f}", f"{kw:.3f}"]


def test_data_table_deferrable_load_kwh_values() -> None:
    """Data table kWh column for a deferrable load contains kw × 0.25."""
    kw = -1.5
    schedule = _make_schedule_with_deferrable(kw=kw, name="wash", n=2)
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    table = _build_data_table(inp, out, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    kwh_idx = headers.index("wash<br>kWh")
    col_data = table.cells.values[kwh_idx]

    expected = f"{kw * _STEP_HOURS:.3f}"
    assert col_data == [expected, expected]


def test_data_table_no_deferrable_load_columns_when_absent() -> None:
    """When the schedule contains no deferrable loads, no kW/kWh columns are
    added and the table has the same structure as before."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.5,
            "grid_export_kw": 0.0,
            "devices": {"bat": {"kw": -1.0, "type": "battery"}},
        }
    ]
    inp = _make_inp(n=1)
    out = _make_out_from_schedule(schedule)

    table = _build_data_table(inp, out, schedule, _XS_2[:1], {}, {})

    headers = list(table.header.values)
    # Deferrable load columns follow the pattern "{name}<br>kW" where the name
    # is not one of the fixed columns. Exclude the known fixed headers that
    # happen to contain "kW".
    _fixed = {
        "PV fc<br>kW", "PV fc<br>kWh", "Load fc<br>kW", "Load fc<br>kWh",
        "Grid imp<br>kW", "Grid exp<br>kW",
    }
    deferrable_headers = [
        h for h in headers
        if ("<br>kW" in h or "<br>kWh" in h) and h not in _fixed
    ]
    assert deferrable_headers == [], (
        f"Unexpected deferrable columns when no deferrable in schedule: {deferrable_headers}"
    )


def test_data_table_grid_columns_still_present_with_deferrable_load() -> None:
    """'Grid imp kW' and 'Grid exp kW' columns appear after the deferrable
    load columns — they must not be displaced."""
    schedule = _make_schedule_with_deferrable(kw=-1.5, name="wash")
    inp = _make_inp()
    out = _make_out_from_schedule(schedule)

    table = _build_data_table(inp, out, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "Grid imp<br>kW" in headers
    assert "Grid exp<br>kW" in headers
    # Grid columns must appear after the deferrable load columns.
    assert headers.index("Grid imp<br>kW") > headers.index("wash<br>kWh")
