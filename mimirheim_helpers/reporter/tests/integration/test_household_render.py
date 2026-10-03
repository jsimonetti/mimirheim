"""Integration tests: render the household view against dump pairs.

The household view is the layperson-facing report. These tests confirm that
``build_household_html`` produces a well-formed, self-contained HTML document,
that it does NOT depend on Plotly, that every flow it embeds is read off the
schedule's devices rather than inferred, and that its totals agree with the
shared ``metrics`` module.

They mirror ``test_render_against_fixture.py`` and assert only through the
public ``build_household_html`` seam, never against internals. Expected values
come from the dump itself (device ``kw``, planned SOC) or from ``metrics``.
"""
from __future__ import annotations

import copy
import json
import re

import pytest

from reporter.household import build_household_html
from reporter.metrics import compute_economic_metrics, compute_schedule_metrics

_FLOW_KEYS = (
    "t", "price", "imp", "expo", "load", "sun",
    "sun_home", "batt_home", "other_home", "grid_home",
    "chg_sun", "chg_grid", "mode",
)


def _payload(inp: dict, out: dict) -> dict:
    """Render and extract the JSON data payload the page embeds."""
    html = build_household_html(inp, out)
    m = re.search(
        r'<script id="household-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "household page must embed a household-data JSON script block"
    return json.loads(m.group(1).replace("<\\/", "</"))


def _step(t: str, devices: dict, imp: float = 0.0, exp: float = 0.0) -> dict:
    return {
        "t": t,
        "import_price_eur_per_kwh": 0.30,
        "export_price_eur_per_kwh": 0.05,
        "grid_import_kw": imp,
        "grid_export_kw": exp,
        "devices": devices,
    }


# ---------------------------------------------------------------------------
# Page shape and agreement with metrics
# ---------------------------------------------------------------------------


def test_household_produces_self_contained_html(fixture_inp: dict, fixture_out: dict) -> None:
    """Returns a complete, non-empty HTML document that does not need Plotly."""
    result = build_household_html(fixture_inp, fixture_out)
    assert isinstance(result, str)
    assert len(result) > 1000
    assert "<!DOCTYPE html>" in result
    assert "</html>" in result
    assert "plotly" not in result.lower()


def test_household_saving_matches_economic_metrics(fixture_inp: dict, fixture_out: dict) -> None:
    """Embedded savings equals the shared economic-metrics value, not a re-derivation."""
    d = _payload(fixture_inp, fixture_out)
    eco = compute_economic_metrics(fixture_out)
    assert d["summary"]["saving"] == round(max(0.0, eco.saving_eur), 4)
    assert d["summary"]["naive"] == eco.naive_cost_eur


def test_household_totals_match_schedule_metrics(fixture_inp: dict, fixture_out: dict) -> None:
    """Day-in-numbers totals equal the shared schedule-metrics values."""
    d = _payload(fixture_inp, fixture_out)
    m = compute_schedule_metrics(fixture_out["schedule"])
    assert d["summary"]["import"] == m.grid_import_kwh
    assert d["summary"]["pv"] == m.pv_total_kwh
    assert d["summary"]["load"] == m.load_total_kwh
    assert d["summary"]["self"] == m.self_sufficiency_pct


def test_household_series_align_with_schedule(fixture_inp: dict, fixture_out: dict) -> None:
    """Every per-step array has one entry per schedule step, aligned by timestamp."""
    d = _payload(fixture_inp, fixture_out)
    schedule = fixture_out["schedule"]
    for key in _FLOW_KEYS:
        assert len(d[key]) == len(schedule), key
    assert d["t"][0] == schedule[0]["t"]
    assert d["t"][-1] == schedule[-1]["t"]


# ---------------------------------------------------------------------------
# Review point 2: flows are read off the devices, so the arithmetic holds
# ---------------------------------------------------------------------------


def test_house_usage_is_fully_accounted_each_step(fixture_inp: dict, fixture_out: dict) -> None:
    """Sun + battery + other + grid to the house adds up to house usage, every step."""
    d = _payload(fixture_inp, fixture_out)
    for i in range(len(d["t"])):
        served = d["sun_home"][i] + d["batt_home"][i] + d["other_home"][i] + d["grid_home"][i]
        assert served == pytest.approx(d["load"][i], abs=1e-3), f"step {i}"


def test_battery_charging_equals_the_battery_device(fixture_inp: dict, fixture_out: dict) -> None:
    """Battery charging shown on the page is the battery device's own kW."""
    d = _payload(fixture_inp, fixture_out)
    for i, step in enumerate(fixture_out["schedule"]):
        charging = sum(
            max(0.0, -dev["kw"])
            for dev in step["devices"].values()
            if dev.get("type") == "battery"
        )
        shown = d["chg_sun"][i] + d["chg_grid"][i]
        assert shown == pytest.approx(charging, abs=1e-3), f"step {i}"


def test_ev_charger_is_never_shown_as_battery_charging(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """The reviewer's experiment: swap every battery to an EV charger.

    Before the fix this produced a byte-identical payload and the page claimed
    the battery was filling up while the car was charging.
    """
    out = copy.deepcopy(fixture_out)
    for step in out["schedule"]:
        for dev in step["devices"].values():
            if dev.get("type") == "battery":
                dev["type"] = "ev_charger"
    original = _payload(fixture_inp, fixture_out)
    swapped = _payload(fixture_inp, out)

    assert swapped != original
    assert swapped["summary"]["has_storage"] is False
    assert all(v == 0 for v in swapped["chg_sun"])
    assert all(v == 0 for v in swapped["chg_grid"])
    assert not {"grid_charge", "solar_charge"} & set(swapped["mode"])
    # The car's charging is house usage instead.
    for i, step in enumerate(out["schedule"]):
        car = sum(
            max(0.0, -dev["kw"])
            for dev in step["devices"].values()
            if dev.get("type") == "ev_charger"
        )
        assert swapped["load"][i] >= car - 1e-3, f"step {i}"


def test_hybrid_inverter_split_follows_planned_soc() -> None:
    """A hybrid's AC output is its sun while the cell fills, its battery while it empties."""
    inp = {
        "triggered_at_utc": "2026-10-01T10:00:00Z",
        "hybrid_inverter_inputs": {"hyb": {"soc_kwh": 5.0}},
        "config": {"hybrid_inverters": {"hyb": {
            "inverter_efficiency": 1.0,
            "battery_charge_efficiency": 1.0,
            "battery_discharge_efficiency": 1.0,
        }}},
    }
    load = {"type": "static_load", "kw": -1.0}
    out = {"schedule": [
        # Cell gains 0.25 kWh in 15 min (= 1 kW) while the AC side delivers 1 kW: DC sun.
        _step("2026-10-01T10:00:00Z", {"hyb": {"type": "hybrid_inverter", "kw": 1.0, "soc_kwh": 5.25}, "base": load}),
        # Cell loses 0.25 kWh (= 1 kW) and the AC side delivers 1 kW: battery.
        _step("2026-10-01T10:15:00Z", {"hyb": {"type": "hybrid_inverter", "kw": 1.0, "soc_kwh": 5.0}, "base": load}),
    ]}
    d = _payload(inp, out)
    assert d["sun_home"][0] == pytest.approx(1.0)
    assert d["batt_home"][0] == pytest.approx(0.0)
    assert d["chg_sun"][0] == pytest.approx(1.0)
    assert d["batt_home"][1] == pytest.approx(1.0)
    assert d["sun_home"][1] == pytest.approx(0.0)
    assert d["mode"] == ["solar_charge", "battery"]
    assert d["summary"]["has_storage"] is True


# ---------------------------------------------------------------------------
# Review point 1: device types the view does not break out degrade honestly
# ---------------------------------------------------------------------------


def test_unknown_device_type_is_reported_as_other_not_folded_in() -> None:
    """A device type the view does not know shows up as 'other', and the sum still holds."""
    out = {"schedule": [
        _step("2026-10-01T10:00:00Z", {
            "base": {"type": "static_load", "kw": -1.0},
            "mystery": {"type": "fuel_cell", "kw": 0.5},
        }, imp=0.5),
    ]}
    d = _payload({}, out)
    assert d["other_home"][0] == pytest.approx(0.5)
    assert d["grid_home"][0] == pytest.approx(0.5)
    assert d["sun_home"][0] == 0 and d["batt_home"][0] == 0
    assert d["summary"]["other_kwh"] == pytest.approx(0.125)


def test_heat_pump_counts_as_house_usage() -> None:
    """Thermal device types are consumption, not an unknown source."""
    out = {"schedule": [
        _step("2026-10-01T10:00:00Z", {"hp": {"type": "space_heating_hp", "kw": -2.0}}, imp=2.0),
    ]}
    d = _payload({}, out)
    assert d["load"][0] == pytest.approx(2.0)
    assert d["grid_home"][0] == pytest.approx(2.0)
    assert d["summary"]["other_kwh"] == 0


# ---------------------------------------------------------------------------
# Review point 3: no home battery
# ---------------------------------------------------------------------------


def test_no_storage_is_flagged_for_the_wording(fixture_inp: dict, fixture_out: dict) -> None:
    """Without battery/hybrid devices the page is told there is no storage."""
    out = copy.deepcopy(fixture_out)
    for step in out["schedule"]:
        step["devices"] = {
            n: dev for n, dev in step["devices"].items()
            if dev.get("type") not in ("battery", "hybrid_inverter")
        }
    d = _payload(fixture_inp, out)
    assert d["summary"]["has_storage"] is False
    # Nothing left that could be "the battery filling up".
    assert all(v == 0 for v in d["chg_sun"])
    assert all(v == 0 for v in d["chg_grid"])


def test_fixture_with_battery_is_flagged_as_storage(fixture_inp: dict, fixture_out: dict) -> None:
    assert _payload(fixture_inp, fixture_out)["summary"]["has_storage"] is True


# ---------------------------------------------------------------------------
# Review point 4: negative costs get their own case
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("naive", "opt", "case"),
    [
        (2.0, 1.0, "cost"),     # pay, plan pays less
        (1.0, -0.5, "flip"),    # plan turns a cost into income
        (-1.88, -3.35, "earn"), # the reviewer's sunny August day
        (1.0, 1.0, "none"),     # plan adds nothing
    ],
)
def test_cost_case_classification(
    fixture_inp: dict, fixture_out: dict, naive: float, opt: float, case: str
) -> None:
    out = copy.deepcopy(fixture_out)
    out["naive_cost_eur"] = naive
    out["optimised_cost_eur"] = opt
    out["soc_credit_eur"] = 0.0
    assert _payload(fixture_inp, out)["summary"]["case"] == case


def test_page_never_formats_a_negative_euro_amount(fixture_inp: dict, fixture_out: dict) -> None:
    """The template words money as pay/earn; there is no signed-euro formatter left."""
    html = build_household_html(fixture_inp, fixture_out)
    assert "eur(s.naive)" not in html and "eur(s.opt)" not in html
    assert "t.pay" in html and "t.earn" in html


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_household_empty_schedule_renders(fixture_inp: dict, fixture_out: dict) -> None:
    """An empty schedule renders a valid page rather than raising."""
    out = copy.deepcopy(fixture_out)
    out["schedule"] = []
    result = build_household_html(fixture_inp, out)
    assert "<!DOCTYPE html>" in result
    assert "</html>" in result


def test_household_no_pv_no_battery_renders(fixture_inp: dict, fixture_out: dict) -> None:
    """A schedule with no PV and no battery devices still renders a valid page."""
    out = copy.deepcopy(fixture_out)
    for step in out["schedule"]:
        step["devices"] = {
            name: dev for name, dev in step["devices"].items()
            if dev.get("type") not in ("pv", "battery")
        }
    d = _payload(fixture_inp, out)
    assert all(v == 0 for v in d["sun"])
    assert d["summary"]["has_storage"] is False


def test_household_zero_naive_cost_renders(fixture_inp: dict, fixture_out: dict) -> None:
    """Zero naive cost does not divide by zero; the day reads as 'none'."""
    out = copy.deepcopy(fixture_out)
    out["naive_cost_eur"] = 0.0
    out["optimised_cost_eur"] = 0.0
    out["soc_credit_eur"] = 0.0
    d = _payload(fixture_inp, out)
    assert d["summary"]["saving"] == 0.0
    assert d["summary"]["case"] == "none"


# ---------------------------------------------------------------------------
# The page's own script must at least parse
# ---------------------------------------------------------------------------


def test_page_script_parses(fixture_inp: dict, fixture_out: dict, tmp_path) -> None:
    """Syntax-check the embedded presenter script with node, when available.

    The Python tests never execute the page, so a JavaScript syntax error
    (e.g. a redeclared const) would otherwise ship as a blank page.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    html = build_household_html(fixture_inp, fixture_out)
    scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
    assert scripts, "page must contain its presenter script"
    js = tmp_path / "page.js"
    js.write_text(scripts[-1], encoding="utf-8")
    result = subprocess.run([node, "--check", str(js)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
