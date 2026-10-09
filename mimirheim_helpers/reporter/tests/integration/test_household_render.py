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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from reporter.household import build_household_html
from reporter.metrics import compute_economic_metrics, compute_schedule_metrics

_FLOW_KEYS = (
    "t", "price", "imp", "expo", "load", "sun",
    "sun_home", "batt_home", "other_home", "grid_home",
    "chg_sun", "chg_grid", "mode",
)


def _payload(inp: dict, out: dict, **kwargs) -> dict:
    """Render and extract the JSON data payload the page embeds."""
    html = build_household_html(inp, out, **kwargs)
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
        "hybrid_inverter_inputs": {"hyb": {"soc_kwh": 5.0, "pv_forecast_kw": [1.0, 0.0]}},
        "config": {"hybrid_inverters": {"hyb": {
            "capacity_kwh": 10.0,
            "max_pv_kw": 2.0,
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
# Days: today, tomorrow and the day after
# ---------------------------------------------------------------------------

_AMS = ZoneInfo("Europe/Amsterdam")


def _steps(start: str, n: int, devices: dict, imp: float = 0.0) -> list[dict]:
    """``n`` quarter-hour steps from ``start`` (UTC), all alike."""
    t0 = datetime.fromisoformat(start.replace("Z", "+00:00"))
    return [
        _step((t0 + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), devices, imp=imp)
        for i in range(n)
    ]


def test_days_split_at_local_midnight(fixture_inp: dict, fixture_out: dict) -> None:
    """The fixture runs 17:30 to 17:15 the next day, Amsterdam time (UTC+2).

    Local midnight is 22:00 UTC, step 26 from 15:30 UTC. Today reaches its
    midnight; tomorrow stops at 17:30, so it is incomplete.
    """
    days = _payload(fixture_inp, fixture_out, tz=_AMS)["days"]
    assert [(d["i0"], d["i1"], d["date"], d["complete"]) for d in days] == [
        (0, 26, "2026-04-03", True),
        (26, 96, "2026-04-04", False),
    ]
    # The same plan split at UTC midnight instead: step 34.
    utc = _payload(fixture_inp, fixture_out, tz=timezone.utc)["days"]
    assert [(d["i0"], d["i1"]) for d in utc] == [(0, 34), (34, 96)]


def test_at_most_three_days() -> None:
    """A plan reaching into a fourth day still offers three tabs."""
    load = {"base": {"type": "static_load", "kw": -1.0}}
    out = {"schedule": _steps("2026-10-01T00:00:00Z", 4 * 96, load, imp=1.0)}
    days = _payload({}, out, tz=timezone.utc)["days"]
    assert [(d["i0"], d["i1"]) for d in days] == [(0, 96), (96, 192), (192, 288)]
    assert all(d["complete"] for d in days)


def test_days_add_up_to_the_solvers_money(fixture_inp: dict, fixture_out: dict) -> None:
    """Per day: the baseline adds up to the solver's naive_cost_eur, and grid
    cash (effective cost plus stored value) to its optimised_cost_eur."""
    days = _payload(fixture_inp, fixture_out, tz=_AMS)["days"]
    naive = sum(d["summary"]["naive"] for d in days)
    assert naive == pytest.approx(fixture_out["naive_cost_eur"], abs=1e-3)
    cash = sum(d["summary"]["opt"] + d["summary"]["stored"] for d in days)
    assert cash == pytest.approx(fixture_out["optimised_cost_eur"], abs=1e-3)


def test_day_baseline_follows_the_solver_step_by_step(fixture_inp: dict, fixture_out: dict) -> None:
    """Each day's baseline is the solver's own per step: base load minus PV,
    at the import or export price.

    The fixture predates per-array forecasts: it has two arrays and only the
    summed series, which the solver then used as it stood.
    """
    sched = fixture_out["schedule"]
    assert len(fixture_inp["config"]["pv_arrays"]) == 2 and "pv_forecasts" not in fixture_inp
    per_step = []
    for t, s in enumerate(sched):
        pv = fixture_inp["pv_forecast"][t]
        net = fixture_inp["base_load_forecast"][t] - pv
        price = s["import_price_eur_per_kwh"] if net >= 0 else s["export_price_eur_per_kwh"]
        per_step.append(net * price * 0.25)
    days = _payload(fixture_inp, fixture_out, tz=_AMS)["days"]
    for d in days:
        assert d["summary"]["naive"] == pytest.approx(sum(per_step[d["i0"]:d["i1"]]), abs=1e-3)


def test_day_baseline_clips_each_array_like_the_solver() -> None:
    """With per-array forecasts each array is clipped into [0, its ceiling].

    "staged" peaks at 3 kW but its highest register is 2 kW, so day one's
    3 kW forecast delivers 2 kW against the 4 kW load. Day two's -1 kW is
    sensor noise and delivers nothing. Clipping to max_power_kw, or not at
    all, would move the baseline between the days.
    """
    n = 4
    load = {"base": {"type": "static_load", "kw": -4.0}}
    out = {"schedule": _steps("2026-10-01T23:00:00Z", 2 * n, load, imp=4.0)}
    inp = {
        "base_load_forecast": [4.0] * 2 * n,
        "pv_forecasts": {"staged": [3.0] * n + [-1.0] * n},
        "config": {"pv_arrays": {"staged": {"max_power_kw": 3.0, "production_stages": [0.0, 2.0]}}},
    }
    day_one = (4.0 - 2.0) * 0.30 * 0.25 * n
    day_two = 4.0 * 0.30 * 0.25 * n
    days = _payload(inp, out, tz=timezone.utc)["days"]
    assert [d["summary"]["naive"] for d in days] == pytest.approx([day_one, day_two])


def test_day_baseline_counts_a_hybrid_inverters_own_panels() -> None:
    """A hybrid's panels feed the baseline like the solver's: clipped to
    max_pv_kw and taken through the inverter, here with no PV array at all.

    Day one's 6 kW forecast on a 4 kW MPPT delivers 4 x 0.96 = 3.84 kW of the
    4 kW load; day two has no sun.
    """
    n = 4
    load = {"base": {"type": "static_load", "kw": -4.0}}
    out = {"schedule": _steps("2026-10-01T23:00:00Z", 2 * n, load, imp=4.0)}
    inp = {
        "base_load_forecast": [4.0] * 2 * n,
        "hybrid_inverter_inputs": {
            "hyb": {"soc_kwh": 0.0, "pv_forecast_kw": [6.0] * n + [0.0] * n},
        },
        "config": {"hybrid_inverters": {"hyb": {
            "capacity_kwh": 10.0,
            "max_pv_kw": 4.0,
            "inverter_efficiency": 0.96,
            "battery_discharge_efficiency": 0.95,
        }}},
    }
    day_one = (4.0 - 3.84) * 0.30 * 0.25 * n
    day_two = 4.0 * 0.30 * 0.25 * n
    days = _payload(inp, out, tz=timezone.utc)["days"]
    assert [d["summary"]["naive"] for d in days] == pytest.approx([day_one, day_two])


@pytest.mark.parametrize(
    ("group", "device", "path"),
    [
        # Power-weighted: (0.9 x 2 + 0.8 x 1) / 3.
        ("batteries", {"discharge_segments": [
            {"power_max_kw": 2.0, "efficiency": 0.9},
            {"power_max_kw": 1.0, "efficiency": 0.8},
        ]}, 2.6 / 3),
        ("batteries", {"discharge_segments": [
            {"power_max_kw": 0.0, "efficiency": 0.9},
            {"power_max_kw": 0.0, "efficiency": 0.8},
        ]}, 0.85),
        ("batteries", {"discharge_efficiency_curve": [
            {"power_kw": 0.0, "efficiency": 0.95},
            {"power_kw": 2.0, "efficiency": 0.85},
        ]}, 0.9),
        ("batteries", {}, 1.0),
        ("hybrid_inverters", {
            "max_pv_kw": 2.0,
            "battery_discharge_efficiency": 0.9,
            "inverter_efficiency": 0.96,
        }, 0.864),
        ("ev_chargers", {"discharge_segments": [{"power_max_kw": 7.0, "efficiency": 0.9}]}, 0.9),
    ],
)
def test_energy_left_for_tomorrow_is_not_a_cost_today(
    group: str, device: dict, path: float
) -> None:
    """A day that charges storage for the next gets the stored energy's value
    back: the cell's 1 kWh, at the plan's average 0.30 EUR/kWh, through the
    device's discharge path, the way the solver's soc_credit_eur values it.

    The battery level the page shows is the house's own storage, so a
    plugged-in EV is valued but has no level of its own there.
    """
    kind, inputs = {
        "batteries": ("battery", "battery_inputs"),
        "hybrid_inverters": ("hybrid_inverter", "hybrid_inverter_inputs"),
        "ev_chargers": ("ev_charger", "ev_inputs"),
    }[group]
    inp = {
        inputs: {"bat": {"soc_kwh": 2.0, "available": True, "pv_forecast_kw": [0.0] * 4}},
        "config": {group: {"bat": {"capacity_kwh": 10.0, **device}}},
    }
    day1 = _steps("2026-10-01T23:30:00Z", 2, {"bat": {"type": kind, "kw": -2.0}}, imp=2.0)
    day1[0]["devices"]["bat"] = {"type": kind, "kw": -2.0, "soc_kwh": 2.5}
    day1[1]["devices"]["bat"] = {"type": kind, "kw": -2.0, "soc_kwh": 3.0}
    day2 = _steps("2026-10-02T00:00:00Z", 2, {"bat": {"type": kind, "kw": 0.0, "soc_kwh": 3.0}})
    days = _payload(inp, {"schedule": day1 + day2}, tz=timezone.utc)["days"]

    stored = 0.30 * 1.0 * path
    assert days[0]["summary"]["stored"] == pytest.approx(stored, abs=1e-4)
    # Cash: 2 kW for 30 min at 0.30 EUR/kWh = 0.30 EUR, less what is stored.
    assert days[0]["summary"]["opt"] == pytest.approx(0.30 - stored, abs=1e-4)
    assert days[1]["summary"]["stored"] == pytest.approx(0.0)
    levels = [None, None] if group == "ev_chargers" else [20, 30]
    assert [d["soc_start_pct"] for d in days] == levels


def test_storage_the_solver_does_not_credit_is_not_valued() -> None:
    """An unplugged EV, whose planned SOC is not the vehicle's, and a battery
    without config are left out, as in the solver's soc_credit_eur."""
    inp = {
        "ev_inputs": {"car": {"soc_kwh": 20.0, "available": False}},
        "battery_inputs": {"bat": {"soc_kwh": 2.0}},
        "config": {"ev_chargers": {"car": {}}},
    }
    devices = {
        "car": {"type": "ev_charger", "kw": 0.0, "soc_kwh": 5.0},
        "bat": {"type": "battery", "kw": -2.0, "soc_kwh": 3.0},
    }
    out = {"schedule": _steps("2026-10-01T23:30:00Z", 2, devices, imp=2.0)}
    today = _payload(inp, out, tz=timezone.utc)["days"][0]
    assert today["summary"]["stored"] == 0.0


def _two_days(battery_kw: float, soc_end: float, load_kw: float, imp: float) -> dict:
    """A day at 0.20 EUR/kWh where the battery goes from 2.0 kWh to
    ``soc_end``, then an idle day at 0.40 EUR/kWh: average price 0.30."""
    day1 = _steps("2026-10-01T00:00:00Z", 4, {
        "base": {"type": "static_load", "kw": -load_kw},
        "bat": {"type": "battery", "kw": battery_kw},
    }, imp=imp)
    for i, step in enumerate(day1):
        step["import_price_eur_per_kwh"] = 0.20
        step["devices"]["bat"]["soc_kwh"] = 2.0 + (soc_end - 2.0) * (i + 1) / 4
    day2 = _steps("2026-10-02T00:00:00Z", 4, {"bat": {"type": "battery", "kw": 0.0, "soc_kwh": soc_end}})
    for step in day2:
        step["import_price_eur_per_kwh"] = 0.40
    return {"schedule": day1 + day2}


_BATTERY = {
    "battery_inputs": {"bat": {"soc_kwh": 2.0}},
    "config": {"batteries": {"bat": {
        "capacity_kwh": 10.0,
        "discharge_segments": [{"power_max_kw": 2.0, "efficiency": 0.9}],
    }}},
}


def test_a_day_spending_earlier_energy_can_come_out_negative() -> None:
    """The battery covers 0.9 of the 1 kW load from 1 kWh stored before.

    Grid cash 0.1 kW x 1 h x 0.20 = 0.02 EUR against a 0.20 EUR baseline, but
    the 1 kWh spent is worth 0.9 x 0.30 = 0.27 EUR: -0.09 EUR, and not clipped
    to zero, so the days still add up. The page calls it a carry day.
    """
    inp = {**_BATTERY, "base_load_forecast": [1.0] * 4 + [0.0] * 4}
    days = _payload(inp, _two_days(0.9, 1.0, 1.0, 0.1), tz=timezone.utc)["days"]
    today = days[0]["summary"]
    assert today["cash"] == pytest.approx(0.02)
    assert today["stored"] == pytest.approx(-0.27)
    assert today["saving"] == pytest.approx(-0.09)
    assert today["case"] == "carry"


def test_a_day_that_simply_costs_more_is_extra() -> None:
    """Charging 1 kWh at 0.40 EUR/kWh on a day when the plan averages 0.30:
    the stored energy is worth less than it cost, with nothing spent from
    before, so the day costs more and the page says so plainly."""
    out = _two_days(-1.0, 3.0, 0.0, 1.0)
    for step in out["schedule"][:4]:
        step["import_price_eur_per_kwh"] = 0.40
    for step in out["schedule"][4:]:
        step["import_price_eur_per_kwh"] = 0.20
    today = _payload(_BATTERY, out, tz=timezone.utc)["days"][0]["summary"]
    # Cash 1 kW x 1 h x 0.40 = 0.40; stored 1 kWh x 0.9 x 0.30 = 0.27.
    assert today["saving"] == pytest.approx(-0.13)
    assert today["case"] == "extra"


def test_a_later_day_says_how_much_of_it_is_predicted_prices() -> None:
    """Steps below confidence 1.0 are priced from a prediction."""
    load = {"base": {"type": "static_load", "kw": -1.0}}
    out = {"schedule": _steps("2026-10-01T00:00:00Z", 192, load, imp=1.0)}
    inp = {"horizon_confidence": [1.0] * 96 + [1.0] * 48 + [0.55] * 48}
    days = _payload(inp, out, tz=timezone.utc)["days"]
    assert [d["predicted"] for d in days] == [0.0, 0.5]


def test_today_counts_the_quarter_hours_before_this_plan() -> None:
    """Earlier plans from today add their first step to today's numbers.

    This plan starts at 10:00. Plans made at 09:00 and 09:15 (09:15 twice,
    the later one counts) add two quarter hours; one from yesterday, one
    overlapping this plan and an infeasible one with no schedule are ignored.
    """
    load = {"base": {"type": "static_load", "kw": -1.0}}
    out = {"schedule": _steps("2026-10-01T10:00:00Z", 4, load, imp=1.0)}

    def made(at: str, first: str, imp: float) -> tuple[dict, dict]:
        return {"triggered_at_utc": at}, {"schedule": _steps(first, 2, load, imp=imp)}

    earlier = [
        made("2026-10-01T09:15:00Z", "2026-10-01T09:15:00Z", 1.0),
        made("2026-10-01T09:00:00Z", "2026-10-01T09:00:00Z", 1.0),
        made("2026-10-01T09:14:00Z", "2026-10-01T09:15:00Z", 0.0),
        made("2026-09-30T23:30:00Z", "2026-09-30T23:30:00Z", 1.0),
        made("2026-10-01T10:00:00Z", "2026-10-01T10:00:00Z", 1.0),
        ({"triggered_at_utc": "2026-10-01T09:30:00Z"}, {"schedule": []}),
    ]
    today = _payload({}, out, tz=timezone.utc, earlier=earlier)["days"][0]
    plan_only = _payload({}, out, tz=timezone.utc)["days"][0]

    assert today["past_from"] == "09:00"
    assert plan_only["past_from"] is None
    # 4 plan steps + 2 earlier ones, 1 kW each, a quarter hour each.
    assert today["summary"]["load"] == pytest.approx(1.5)
    assert today["summary"]["import"] == pytest.approx(1.5)
    assert plan_only["summary"]["load"] == pytest.approx(1.0)
    # "Still to come" is the plan's own part.
    assert today["summary"]["rest_saving"] == pytest.approx(plan_only["summary"]["saving"])
    assert plan_only["summary"]["rest_saving"] is None


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
