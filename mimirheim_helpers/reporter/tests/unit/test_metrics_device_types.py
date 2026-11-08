"""Unit tests: compute_schedule_metrics counts every consuming device type.

Load used to be only ``static_load``/``deferrable_load``. A house with an EV or
a heat pump then read as using far less than its schedule showed, and its
self-sufficiency read 0%. Expected values below are worked out by hand from
the step data (15-minute steps, so 1 kW for one step is 0.25 kWh).
"""
from __future__ import annotations

import pytest

from reporter.inventory import _build_entry
from reporter.metrics import compute_schedule_metrics


def _step(devices: dict, imp: float = 0.0, exp: float = 0.0) -> dict:
    return {"grid_import_kw": imp, "grid_export_kw": exp, "devices": devices}


@pytest.mark.parametrize(
    "dtype",
    ["ev_charger", "thermal_boiler", "space_heating_hp", "combi_heat_pump", "heat_store"],
)
def test_consuming_device_types_count_as_load(dtype: str) -> None:
    """A heat-pump (or EV, boiler...) house no longer reads as using 0 kWh."""
    m = compute_schedule_metrics([_step({"d": {"type": dtype, "kw": -2.0}}, imp=1.0)])
    assert m.load_total_kwh == pytest.approx(0.5)
    # 2 kW used, 1 kW imported: half met locally.
    assert m.self_sufficiency_pct == pytest.approx(50.0)


def test_ev_discharge_is_not_load() -> None:
    """Vehicle-to-home (positive kw) is storage discharge, not consumption."""
    m = compute_schedule_metrics([_step({"car": {"type": "ev_charger", "kw": 3.0}})])
    assert m.load_total_kwh == 0.0


def test_storage_is_not_load() -> None:
    m = compute_schedule_metrics([
        _step({"bat": {"type": "battery", "kw": -2.0}}),
        _step({"hyb": {"type": "hybrid_inverter", "kw": -2.0, "soc_kwh": 1.0}}),
    ])
    assert m.load_total_kwh == 0.0


def test_static_and_deferrable_loads_still_count() -> None:
    m = compute_schedule_metrics([_step({
        "base": {"type": "static_load", "kw": -1.0},
        "dish": {"type": "deferrable_load", "kw": -2.0},
    })])
    assert m.load_total_kwh == pytest.approx(0.75)


def test_index_and_report_agree_on_self_sufficiency() -> None:
    """The inventory entry uses the same load definition as the reports."""
    out = {"schedule": [_step({"car": {"type": "ev_charger", "kw": -4.0}}, imp=1.0)]}
    entry = _build_entry("2026-10-01T10:00:00Z", "2026-10-01T10-00-00Z_report.html", out)
    report = compute_schedule_metrics(out["schedule"])
    assert entry["self_sufficiency_pct"] == report.self_sufficiency_pct
    # 4 kW used, 1 kW imported: three quarters met locally.
    assert entry["self_sufficiency_pct"] == pytest.approx(75.0)
