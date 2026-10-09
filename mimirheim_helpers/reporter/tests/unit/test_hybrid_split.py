"""Unit tests: splitting a hybrid inverter into its solar and battery parts.

A hybrid's AC ``kw`` mixes its own panels with its battery. ``split_hybrid_step``
separates them from the planned SOC and the configured efficiencies.

The main test runs the inverter's equations forward (panels, cell and inverter
balancing on the DC bus) for many scenarios, then checks that the split
recovers exactly what was put in. The expected values come from that forward
model, never from the function under test. Every efficiency set but one is
below 1.0: at 1.0 a cell-side and an AC-side number coincide, which is how an
earlier version of this split went wrong unnoticed.
"""
from __future__ import annotations

import json
import random
import re

import pytest

from reporter.household import build_household_html
from reporter.metrics import (
    compute_schedule_metrics,
    hybrid_splits,
    split_hybrid_step,
)

STEP_H = 0.25

# (inverter, charge, discharge): ideal, the schema defaults, a real install,
# and a deliberately lossy one.
EFFICIENCIES = [
    (1.0, 1.0, 1.0),
    (0.97, 0.95, 0.95),
    (0.97, 0.96, 0.96),
    (0.93, 0.90, 0.85),
]


def _forward(pv_dc: float, charge_dc: float, discharge_dc: float, eff: tuple):
    """Run the hybrid's equations forward. Returns (kw, soc_delta_kwh, expected).

    The DC bus balances panels, cell and inverter:
    ``pv_dc + discharge_dc = charge_dc + inverter_dc``. A positive
    ``inverter_dc`` is exported through the inverter (AC out = inv * dc); a
    negative one is drawn from the AC side (AC in = -dc / inv).
    """
    inv, eff_in, eff_out = eff
    inverter_dc = pv_dc + discharge_dc - charge_dc
    kw = inverter_dc * inv if inverter_dc >= 0 else inverter_dc / inv
    cell_kw = charge_dc * eff_in - discharge_dc / eff_out
    # Solar fills the charge first; any shortfall is drawn from the AC side.
    pv_to_cell = min(pv_dc, charge_dc)
    expected = {
        "pv_dc_kw": pv_dc,
        "pv_ac_kw": max(0.0, pv_dc - charge_dc) * inv,
        "battery_ac_kw": discharge_dc * inv,
        "ac_charge_kw": max(0.0, -kw),
        "pv_to_cell_kw": pv_to_cell,
    }
    return kw, cell_kw * STEP_H, expected


def _scenarios():
    rng = random.Random(20261005)
    for eff in EFFICIENCIES:
        for _ in range(250):
            pv = rng.choice([0.0, rng.uniform(0.0, 3.0)])
            mode = rng.choice(["charge", "discharge", "idle"])
            charge = rng.uniform(0.0, 2.4) if mode == "charge" else 0.0
            discharge = rng.uniform(0.0, 2.4) if mode == "discharge" else 0.0
            yield eff, pv, charge, discharge


@pytest.mark.parametrize(("eff", "pv", "charge", "discharge"), list(_scenarios()))
def test_split_recovers_the_forward_model(
    eff: tuple, pv: float, charge: float, discharge: float
) -> None:
    kw, soc_delta, expected = _forward(pv, charge, discharge, eff)
    inv, eff_in, eff_out = eff
    split = split_hybrid_step(
        kw, 5.0, 5.0 + soc_delta, STEP_H,
        inverter_efficiency=inv, charge_efficiency=eff_in, discharge_efficiency=eff_out,
    )
    for field, value in expected.items():
        assert getattr(split, field) == pytest.approx(value, abs=1e-9), field


# ---------------------------------------------------------------------------
# Worked examples at the schema defaults (inverter 0.97, charge/discharge 0.95)
# ---------------------------------------------------------------------------

_DEFAULTS = {"inverter_efficiency": 0.97, "charge_efficiency": 0.95, "discharge_efficiency": 0.95}


def test_solar_charging_the_cell_while_exporting() -> None:
    """2 kW of sun: 1 kW into the battery, 1 kW through the inverter.

    The cell gains 1 kW * 0.95 = 0.95 kW (0.2375 kWh in 15 min) and the AC side
    delivers 1 kW * 0.97 = 0.97 kW. Solar at the MPPT input is 2 kW. Adding the
    cell-side 0.95 to the AC-side 0.97 would give 1.92 kW, 4% low.
    """
    split = split_hybrid_step(0.97, 5.0, 5.2375, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == pytest.approx(2.0)
    assert split.pv_to_cell_kw == pytest.approx(1.0)
    assert split.pv_ac_kw == pytest.approx(0.97)
    assert split.battery_ac_kw == 0.0


def test_battery_and_solar_both_feeding_the_house() -> None:
    """1 kW of sun plus a cell losing 0.5 kW.

    The cell's 0.5 kW reaches the bus as 0.475 kW. The AC side delivers
    (1 + 0.475) * 0.97 = 1.43075 kW, of which 0.475 * 0.97 = 0.46075 kW is
    battery and 0.97 kW is solar.
    """
    split = split_hybrid_step(1.43075, 5.0, 4.875, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == pytest.approx(1.0)
    assert split.battery_ac_kw == pytest.approx(0.46075)
    assert split.pv_ac_kw == pytest.approx(0.97)
    assert split.pv_to_cell_kw == 0.0


def test_grid_tops_up_solar_charging() -> None:
    """0.5 kW of sun and 1.5 kW from the grid into the cell.

    The AC side draws 1.0 kW, reaching the bus as 0.97 kW; with 0.5 kW of sun,
    1.47 kW goes into the cell, which gains 1.47 * 0.95 = 1.3965 kW.
    """
    split = split_hybrid_step(-1.0, 5.0, 5.0 + 1.3965 * STEP_H, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == pytest.approx(0.5)
    assert split.pv_to_cell_kw == pytest.approx(0.5)
    assert split.ac_charge_kw == pytest.approx(1.0)
    assert split.pv_ac_kw == 0.0


def test_grid_charging_alone_is_not_solar() -> None:
    """The AC side draws 2 kW and the cell gains exactly that, after losses."""
    gain = 2.0 * 0.97 * 0.95
    split = split_hybrid_step(-2.0, 5.0, 5.0 + gain * STEP_H, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == pytest.approx(0.0, abs=1e-12)
    assert split.pv_to_cell_kw == pytest.approx(0.0, abs=1e-12)


def test_dc_total_equals_ac_share_plus_cell_share() -> None:
    """The documented identity: pv_dc = pv_ac / inverter + pv_to_cell."""
    split = split_hybrid_step(0.97, 5.0, 5.2375, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == pytest.approx(split.pv_ac_kw / 0.97 + split.pv_to_cell_kw)


# ---------------------------------------------------------------------------
# When the SOC change is unknown, nothing is counted as solar
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("prev", "now"), [(None, 5.0), (5.0, None), (None, None)])
def test_unknown_soc_counts_no_solar(prev, now) -> None:
    split = split_hybrid_step(1.0, prev, now, STEP_H, **_DEFAULTS)
    assert split.pv_dc_kw == 0.0
    assert split.pv_ac_kw == 0.0
    assert split.battery_ac_kw == 1.0


def _hybrid_dump(eff: float = 0.95, inv: float = 0.97, with_start: bool = True):
    inp = {"config": {"hybrid_inverters": {"hyb": {
        "capacity_kwh": 10.0,
        "max_pv_kw": 2.0,
        "inverter_efficiency": inv,
        "battery_charge_efficiency": eff,
        "battery_discharge_efficiency": eff,
    }}}}
    if with_start:
        inp["hybrid_inverter_inputs"] = {"hyb": {"soc_kwh": 5.0, "pv_forecast_kw": [2.0, 1.0]}}
    load = {"type": "static_load", "kw": -0.97}
    schedule = [
        # The solar-charging example above, then the mixed-discharge one.
        {"t": "2026-10-01T10:00:00Z", "grid_import_kw": 0.0, "grid_export_kw": 0.0,
         "import_price_eur_per_kwh": 0.3, "export_price_eur_per_kwh": 0.05,
         "devices": {"hyb": {"type": "hybrid_inverter", "kw": 0.97, "soc_kwh": 5.2375},
                     "base": load}},
        {"t": "2026-10-01T10:15:00Z", "grid_import_kw": 0.0, "grid_export_kw": 0.46075,
         "import_price_eur_per_kwh": 0.3, "export_price_eur_per_kwh": 0.05,
         "devices": {"hyb": {"type": "hybrid_inverter", "kw": 1.43075, "soc_kwh": 5.1125},
                     "base": load}},
    ]
    return inp, {"schedule": schedule}


def test_hybrid_splits_walks_the_schedule_with_inputs() -> None:
    inp, out = _hybrid_dump()
    first, second = (step["hyb"] for step in hybrid_splits(out["schedule"], inp))
    assert first.pv_dc_kw == pytest.approx(2.0)
    assert second.pv_dc_kw == pytest.approx(1.0)
    assert second.battery_ac_kw == pytest.approx(0.46075)


def test_without_a_starting_soc_the_first_step_counts_no_solar() -> None:
    inp, out = _hybrid_dump(with_start=False)
    first, second = (step["hyb"] for step in hybrid_splits(out["schedule"], inp))
    assert first.pv_dc_kw == 0.0
    assert second.pv_dc_kw == pytest.approx(1.0)


def test_metrics_count_hybrid_solar_at_the_mppt_input() -> None:
    """2 kW then 1 kW of hybrid solar for 15 minutes each: 0.75 kWh."""
    inp, out = _hybrid_dump()
    assert compute_schedule_metrics(out["schedule"], inp).pv_total_kwh == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# The household view uses the same split, so the two views agree
# ---------------------------------------------------------------------------


def _household_payload(inp, out) -> dict:
    html = build_household_html(inp, out)
    m = re.search(r'<script id="household-data" type="application/json">(.*?)</script>',
                  html, re.DOTALL)
    return json.loads(m.group(1).replace("<\\/", "</"))


def test_household_sun_matches_the_metrics_total() -> None:
    inp, out = _hybrid_dump()
    d = _household_payload(inp, out)
    assert sum(d["sun"]) * STEP_H == pytest.approx(d["summary"]["pv"])
    assert d["summary"]["pv"] == pytest.approx(0.75)


def test_household_usage_still_balances_with_real_efficiencies() -> None:
    inp, out = _hybrid_dump()
    d = _household_payload(inp, out)
    for i in range(len(d["t"])):
        served = d["sun_home"][i] + d["batt_home"][i] + d["other_home"][i] + d["grid_home"][i]
        assert served == pytest.approx(d["load"][i], abs=1e-6)
    # First step: the hybrid's 0.97 kW AC is all sun; 1 kW of its sun charges the cell.
    assert d["sun_home"][0] == pytest.approx(0.97)
    assert d["chg_sun"][0] == pytest.approx(1.0)
    assert d["mode"][0] == "solar_charge"
