"""Unit tests for multi-array PV and multi static load rendering.

_build_energy_flows_traces aggregates every ``pv`` device in a schedule step
into a single "PV generation" bar, and every ``static_load`` device into a
single "Base load" bar. Both must stay aligned with the x axis: one y value
per schedule step, regardless of how many devices of that type the schedule
contains.

A per-device append produces len(devices) * len(steps) values against
len(steps) x positions, which Plotly silently zips positionally - the series
is stretched and truncated rather than raising.
"""
from __future__ import annotations

import plotly.graph_objects as go

from reporter._render_helpers import _build_energy_flows_traces

_STEP_HOURS = 0.25  # 15 min / 60 min

_XS_3 = [
    "2026-04-03T15:30:00Z",
    "2026-04-03T15:45:00Z",
    "2026-04-03T16:00:00Z",
]


def _make_inp(n: int = 3) -> dict:
    """Minimal parsed input dump with n steps and no device config."""
    return {
        "horizon_prices": [0.20] * n,
        "horizon_export_prices": [0.05] * n,
        "horizon_confidence": [1.0] * n,
        "pv_forecast": [9.0] * n,
        "base_load_forecast": [1.0] * n,
        "config": {},
    }


def _make_out(devices_per_step: list[dict]) -> dict:
    return {
        "schedule": [
            {
                "t": _XS_3[i],
                "grid_import_kw": 0.0,
                "grid_export_kw": 0.0,
                "devices": devices,
            }
            for i, devices in enumerate(devices_per_step)
        ]
    }


def _trace(traces: list[go.BaseTraceType], name: str) -> go.BaseTraceType:
    matches = [t for t in traces if t.name == name]
    assert len(matches) == 1, f"expected exactly one {name!r} trace, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# PV generation
# ---------------------------------------------------------------------------


def test_two_pv_arrays_produce_one_value_per_step() -> None:
    """Two PV arrays must not double the length of the PV trace."""
    kws = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    out = _make_out(
        [
            {
                "solaredge": {"kw": se, "type": "pv"},
                "enphase": {"kw": en, "type": "pv"},
            }
            for se, en in kws
        ]
    )

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    assert len(pv.y) == len(_XS_3)
    assert len(pv.y) == len(pv.x)


def test_two_pv_arrays_are_summed_per_step() -> None:
    """The PV bar carries the negated sum of all arrays in that step."""
    kws = [(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)]
    out = _make_out(
        [
            {
                "solaredge": {"kw": se, "type": "pv"},
                "enphase": {"kw": en, "type": "pv"},
            }
            for se, en in kws
        ]
    )

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    expected = [-((se + en) * _STEP_HOURS) for se, en in kws]
    assert list(pv.y) == [round(v, 10) for v in expected]


def test_pv_peak_reflects_combined_output() -> None:
    """The chart peak must be the combined array peak, not a single array's."""
    out = _make_out(
        [
            {
                "solaredge": {"kw": 2.5, "type": "pv"},
                "enphase": {"kw": 6.5, "type": "pv"},
            }
        ]
        * 3
    )

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    assert min(pv.y) == -(9.0 * _STEP_HOURS)


def test_single_pv_array_unchanged() -> None:
    """A one-array schedule keeps its existing per-step values."""
    out = _make_out([{"solaredge": {"kw": 4.0, "type": "pv"}}] * 3)

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    assert list(pv.y) == [-(4.0 * _STEP_HOURS)] * 3


def test_pv_absent_from_schedule_falls_back_to_forecast() -> None:
    """With no PV device in the schedule the input forecast is plotted."""
    out = _make_out([{"base": {"kw": -1.0, "type": "static_load"}}] * 3)

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    assert list(pv.y) == [-(9.0 * _STEP_HOURS)] * 3


def test_pv_missing_in_some_steps_stays_aligned() -> None:
    """An array absent from a step contributes zero, not a shorter series."""
    out = _make_out(
        [
            {
                "solaredge": {"kw": 1.0, "type": "pv"},
                "enphase": {"kw": 2.0, "type": "pv"},
            },
            {"solaredge": {"kw": 1.0, "type": "pv"}},
            {
                "solaredge": {"kw": 1.0, "type": "pv"},
                "enphase": {"kw": 2.0, "type": "pv"},
            },
        ]
    )

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    pv = _trace(opt_traces, "PV generation")

    assert len(pv.y) == len(_XS_3)
    assert list(pv.y) == [
        -(3.0 * _STEP_HOURS),
        -(1.0 * _STEP_HOURS),
        -(3.0 * _STEP_HOURS),
    ]


# ---------------------------------------------------------------------------
# Base load
# ---------------------------------------------------------------------------


def test_two_static_loads_produce_one_value_per_step() -> None:
    """Two static loads must not double the length of the base load trace."""
    out = _make_out(
        [
            {
                "base": {"kw": -1.0, "type": "static_load"},
                "standby": {"kw": -0.5, "type": "static_load"},
            }
        ]
        * 3
    )

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    base = _trace(opt_traces, "Base load")

    assert len(base.y) == len(_XS_3)
    assert list(base.y) == [1.5 * _STEP_HOURS] * 3


def test_base_load_absent_from_schedule_falls_back_to_forecast() -> None:
    """With no static load in the schedule the input forecast is plotted."""
    out = _make_out([{"solaredge": {"kw": 4.0, "type": "pv"}}] * 3)

    _, opt_traces = _build_energy_flows_traces(_make_inp(), out, _XS_3)
    base = _trace(opt_traces, "Base load")

    assert list(base.y) == [1.0 * _STEP_HOURS] * 3
