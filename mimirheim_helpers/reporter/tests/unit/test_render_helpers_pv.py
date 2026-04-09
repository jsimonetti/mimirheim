"""Unit tests for PV device rendering in _render_helpers._build_data_table.

A PV device in the schedule (``type == "pv"``) should produce per-array columns
in the schedule data table: scheduled kW, scheduled kWh, production limit
(when ``power_limit_kw`` is present in the setpoint), on/off state (when
``on_off_active`` is present), and ZEX (when ``zero_exchange_active`` is present).

These complement the existing ``PV fc kW`` and ``PV fc kWh`` forecast columns,
which show the solver input rather than the solver decision.

If a schedule contains no PV devices, no extra columns must be added except the
existing fixed forecast columns (regression guard).
"""
from __future__ import annotations

import plotly.graph_objects as go
import pytest

from reporter._render_helpers import _build_data_table

_STEP_HOURS = 0.25  # 15 min
_XS_2 = ["2026-04-03T15:30:00Z", "2026-04-03T15:45:00Z"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_inp(n: int = 2) -> dict:
    """Minimal parsed input dump with n steps and no device config."""
    return {
        "horizon_prices": [0.20] * n,
        "horizon_export_prices": [0.05] * n,
        "horizon_confidence": [1.0] * n,
        "pv_forecast": [3.0] * n,
        "base_load_forecast": [1.0] * n,
        "config": {},
    }


def _make_schedule_with_pv(
    kw: float = 2.5,
    name: str = "roof",
    on_off_active: bool | None = None,
    zero_exchange_active: bool | None = None,
    power_limit_kw: float | None = None,
    n: int = 2,
) -> list[dict]:
    """Schedule with n steps, all containing a pv device at kw."""
    setpoint: dict = {"kw": kw, "type": "pv"}
    if on_off_active is not None:
        setpoint["on_off_active"] = on_off_active
    if zero_exchange_active is not None:
        setpoint["zero_exchange_active"] = zero_exchange_active
    if power_limit_kw is not None:
        setpoint["power_limit_kw"] = power_limit_kw
    return [
        {
            "t": _XS_2[i] if i < len(_XS_2) else f"t{i}",
            "grid_import_kw": 0.0,
            "grid_export_kw": kw,
            "devices": {name: dict(setpoint)},
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Basic kW / kWh columns
# ---------------------------------------------------------------------------


def test_data_table_pv_device_kw_kwh_columns_in_header() -> None:
    """A PV device in the schedule produces {name}<br>kW and {name}<br>kWh headers."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof")
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>kW" in headers, f"Missing 'roof<br>kW' in headers: {headers}"
    assert "roof<br>kWh" in headers, f"Missing 'roof<br>kWh' in headers: {headers}"


def test_data_table_pv_device_kw_values() -> None:
    """The {name}<br>kW column contains the kw values from the schedule."""
    kw = 2.5
    schedule = _make_schedule_with_pv(kw=kw, name="roof", n=2)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>kW")
    col_data = table.cells.values[idx]

    assert col_data == [f"{kw:.3f}", f"{kw:.3f}"]


def test_data_table_pv_device_kwh_values() -> None:
    """The {name}<br>kWh column contains kw * 0.25 from the schedule."""
    kw = 2.5
    schedule = _make_schedule_with_pv(kw=kw, name="roof", n=2)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>kWh")
    col_data = table.cells.values[idx]

    expected = f"{kw * _STEP_HOURS:.3f}"
    assert col_data == [expected, expected]


# ---------------------------------------------------------------------------
# on/off column
# ---------------------------------------------------------------------------


def test_data_table_pv_on_off_column_appears_when_on_off_active_present() -> None:
    """When on_off_active is present in the PV setpoint, a {name}<br>on/off column appears."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof", on_off_active=True)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>on/off" in headers, f"Missing on/off column in headers: {headers}"


def test_data_table_pv_on_off_column_absent_when_not_in_setpoint() -> None:
    """When on_off_active is absent from all PV setpoints, no on/off column is added."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof")  # no on_off_active
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>on/off" not in headers, f"Unexpected on/off column: {headers}"


def test_data_table_pv_on_off_column_shows_on_when_active() -> None:
    """The on/off column cell shows 'on' when on_off_active is True."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof", on_off_active=True, n=2)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>on/off")
    col_data = table.cells.values[idx]

    assert col_data == ["on", "on"], f"Got: {col_data}"


def test_data_table_pv_on_off_column_shows_off_when_inactive() -> None:
    """The on/off column cell shows 'off' when on_off_active is False."""
    schedule = _make_schedule_with_pv(kw=0.0, name="roof", on_off_active=False, n=2)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>on/off")
    col_data = table.cells.values[idx]

    assert col_data == ["off", "off"], f"Got: {col_data}"


def test_data_table_pv_on_off_mixed_steps() -> None:
    """Steps with on_off_active=True show 'on' and steps with False show 'off'."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.0,
            "grid_export_kw": 2.5,
            "devices": {"roof": {"kw": 2.5, "type": "pv", "on_off_active": True}},
        },
        {
            "t": _XS_2[1],
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "devices": {"roof": {"kw": 0.0, "type": "pv", "on_off_active": False}},
        },
    ]
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>on/off")
    col_data = table.cells.values[idx]

    assert col_data == ["on", "off"], f"Got: {col_data}"


# ---------------------------------------------------------------------------
# ZEX column
# ---------------------------------------------------------------------------


def test_data_table_pv_zex_column_appears_when_zero_exchange_active_present() -> None:
    """When zero_exchange_active is present in the PV setpoint, a {name}<br>ZEX column appears."""
    schedule = _make_schedule_with_pv(kw=0.0, name="roof", zero_exchange_active=True)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>ZEX" in headers, f"Missing ZEX column in headers: {headers}"


def test_data_table_pv_zex_column_absent_when_not_in_setpoint() -> None:
    """When zero_exchange_active is absent from all PV setpoints, no ZEX column is added."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof")
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>ZEX" not in headers, f"Unexpected ZEX column: {headers}"


def test_data_table_pv_zex_column_values() -> None:
    """The ZEX column shows 'yes' when active and 'no' when not."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "devices": {"roof": {"kw": 0.0, "type": "pv", "zero_exchange_active": True}},
        },
        {
            "t": _XS_2[1],
            "grid_import_kw": 0.0,
            "grid_export_kw": 2.5,
            "devices": {"roof": {"kw": 2.5, "type": "pv", "zero_exchange_active": False}},
        },
    ]
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>ZEX")
    col_data = table.cells.values[idx]

    assert col_data == ["yes", "no"], f"Got: {col_data}"


# ---------------------------------------------------------------------------
# power_limit_kw column
# ---------------------------------------------------------------------------


def test_data_table_pv_power_limit_column_appears_when_present() -> None:
    """When power_limit_kw is present in the PV setpoint, a {name}<br>lim kW column appears."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof", power_limit_kw=3.0)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>lim kW" in headers, f"Missing lim kW column in headers: {headers}"


def test_data_table_pv_power_limit_column_absent_when_not_in_setpoint() -> None:
    """When power_limit_kw is absent from all PV setpoints, no lim kW column is added."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof")  # no power_limit_kw
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    assert "roof<br>lim kW" not in headers, f"Unexpected lim kW column: {headers}"


def test_data_table_pv_power_limit_column_values() -> None:
    """The lim kW column shows the power_limit_kw value formatted to 3 decimal places."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof", power_limit_kw=3.0, n=2)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>lim kW")
    col_data = table.cells.values[idx]

    assert col_data == ["3.000", "3.000"], f"Got: {col_data}"


def test_data_table_pv_power_limit_column_shows_dash_when_none() -> None:
    """When power_limit_kw is None for some steps, the cell shows an em-dash."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.0,
            "grid_export_kw": 2.5,
            "devices": {"roof": {"kw": 2.5, "type": "pv", "power_limit_kw": 4.0}},
        },
        {
            "t": _XS_2[1],
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            # power_limit_kw absent — inverter is off; no limit setpoint published
            "devices": {"roof": {"kw": 0.0, "type": "pv"}},
        },
    ]
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    idx = headers.index("roof<br>lim kW")
    col_data = table.cells.values[idx]

    assert col_data[0] == "4.000"
    assert col_data[1] == "\u2014"  # em-dash


# ---------------------------------------------------------------------------
# Absence and ordering guards
# ---------------------------------------------------------------------------


def test_data_table_no_pv_device_columns_when_no_pv_in_schedule() -> None:
    """When the schedule contains no PV devices, no per-array columns are added.

    The existing 'PV fc<br>kW' forecast column must still be present.
    """
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.5,
            "grid_export_kw": 0.0,
            "devices": {"bat": {"kw": -1.0, "type": "battery"}},
        }
    ]
    inp = _make_inp(n=1)

    table = _build_data_table(inp, {}, schedule, _XS_2[:1], {}, {})

    headers = list(table.header.values)
    # The aggregate PV forecast column is always present.
    assert "PV fc<br>kW" in headers

    # No per-array PV columns should appear (device names cannot match the
    # fixed header set, so checking for the pattern suffices).
    _fixed = {
        "PV fc<br>kW",
        "PV fc<br>kWh",
        "Load fc<br>kW",
        "Load fc<br>kWh",
        "Grid imp<br>kW",
        "Grid exp<br>kW",
    }
    per_array_pv = [
        h for h in headers
        if ("<br>kW" in h or "<br>kWh" in h or "<br>on/off" in h)
        and h not in _fixed
        # Note: battery/EV device columns use "<br>AC kW" — excluded by exact match.
        and "AC kW" not in h
        and "AC kWh" not in h
        and "lim kW" not in h  # power_limit_kw column; only present when PV is in schedule
    ]
    assert per_array_pv == [], f"Unexpected PV per-array columns: {per_array_pv}"


def test_data_table_grid_columns_appear_after_pv_device_columns() -> None:
    """Grid import and export columns appear after all PV device columns."""
    schedule = _make_schedule_with_pv(kw=2.5, name="roof", on_off_active=True)
    inp = _make_inp()

    table = _build_data_table(inp, {}, schedule, _XS_2, {}, {})

    headers = list(table.header.values)
    pv_kw_idx = headers.index("roof<br>kW")
    grid_imp_idx = headers.index("Grid imp<br>kW")

    assert grid_imp_idx > pv_kw_idx, (
        f"Expected Grid imp column after PV column; "
        f"grid at {grid_imp_idx}, pv at {pv_kw_idx}"
    )


def test_data_table_multiple_pv_arrays() -> None:
    """Two distinct PV arrays each produce separate kW and kWh columns."""
    schedule = [
        {
            "t": _XS_2[0],
            "grid_import_kw": 0.0,
            "grid_export_kw": 5.0,
            "devices": {
                "roof": {"kw": 3.0, "type": "pv"},
                "garage": {"kw": 2.0, "type": "pv"},
            },
        }
    ]
    inp = _make_inp(n=1)

    table = _build_data_table(inp, {}, schedule, _XS_2[:1], {}, {})

    headers = list(table.header.values)
    assert "roof<br>kW" in headers
    assert "roof<br>kWh" in headers
    assert "garage<br>kW" in headers
    assert "garage<br>kWh" in headers
