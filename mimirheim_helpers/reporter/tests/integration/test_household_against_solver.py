"""Cross-check the household view's per-day money against the real solver.

The household view recomputes two of the solver's figures per day from the
dump: the naive baseline (``_naive_steps``, mirroring ``_compute_naive_cost``,
the clipped PV series ``build_and_solve`` hands it, and
``PvDevice.max_deliverable_kw``) and the value of energy left in storage
(``_stored_eur``, mirroring ``_compute_soc_credit``). A committed fixture cannot
tell when those rules change in the core. This test solves a purpose-built case
with the real ``build_and_solve``, writes it with the real ``debug_dump``,
renders the page from that dump pair, and holds every day against the solver.

The case exercises every clipping path: a staged array whose forecast exceeds
its highest register and dips below zero at night, a hybrid inverter whose
panels exceed ``max_pv_kw``, and storage valued three ways (a battery, the
hybrid, a plugged-in EV). The hybrid's forecast stays at or above zero: the
core does not clip a negative one, and the solve comes out infeasible.
"""
from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from mimirheim.config.schema import MimirheimConfig
from mimirheim.core.bundle import SolveBundle, SolveResult
from mimirheim.core.model_builder import build_and_solve, debug_dump
from reporter.household import build_household_html

_AMS = ZoneInfo("Europe/Amsterdam")
# 22:00 Amsterdam on 1 June: two hours of today, all of tomorrow, and the
# first six hours of the day after.
_T0 = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)
_STEPS = 32 * 4

_CONFIG = {
    "mqtt": {"host": "localhost", "client_id": "household-cross-check"},
    "grid": {"import_limit_kw": 17.0, "export_limit_kw": 17.0},
    "pv_arrays": {
        # Peaks at 3 kW but the highest register is 2 kW.
        "roof": {"max_power_kw": 3.0, "production_stages": [0.0, 1.0, 2.0]},
    },
    "hybrid_inverters": {
        "hybrid": {
            "capacity_kwh": 8.0, "min_soc_kwh": 0.8,
            "max_charge_kw": 2.4, "max_discharge_kw": 2.4, "max_pv_kw": 1.9,
            "battery_charge_efficiency": 0.96, "battery_discharge_efficiency": 0.95,
            "inverter_efficiency": 0.97,
        },
    },
    "batteries": {
        "battery": {
            "capacity_kwh": 5.0, "min_soc_kwh": 0.5,
            "charge_segments": [{"power_max_kw": 2.0, "efficiency": 0.95}],
            # Unequal powers, so a power-weighted average differs from a plain one.
            "discharge_segments": [
                {"power_max_kw": 1.5, "efficiency": 0.96},
                {"power_max_kw": 0.5, "efficiency": 0.90},
            ],
        },
    },
    "ev_chargers": {
        "car": {
            "capacity_kwh": 50.0, "min_soc_kwh": 5.0,
            "charge_segments": [{"power_max_kw": 7.0, "efficiency": 0.92}],
            "discharge_segments": [{"power_max_kw": 7.0, "efficiency": 0.91}],
            "capabilities": {"v2h": True},
        },
    },
    "static_loads": {"base": {}},
}


def _local_hour(i: int) -> float:
    t = (_T0 + timedelta(minutes=15 * i)).astimezone(_AMS)
    return t.hour + t.minute / 60


def _sun(peak_kw: float) -> list[float]:
    """A daylight hump peaking at 13:00 local, slightly negative at night as
    real forecasts are."""
    return [
        round(max(-0.05, peak_kw * math.cos((_local_hour(i) - 13.0) / 12.0 * math.pi)), 3)
        for i in range(_STEPS)
    ]


def _bundle() -> SolveBundle:
    # Cheap at night and around midday, dear in the evening.
    prices = [
        round(0.22 + 0.12 * math.sin((_local_hour(i) - 11.0) / 24.0 * 2 * math.pi), 4)
        for i in range(_STEPS)
    ]
    roof, panels = _sun(2.8), [max(0.0, kw) for kw in _sun(2.4)]
    return SolveBundle.model_validate({
        "solve_time_utc": _T0.isoformat(),
        "triggered_at_utc": _T0.isoformat(),
        "horizon_prices": prices,
        "horizon_export_prices": [round(p - 0.08, 4) for p in prices],
        "horizon_confidence": [1.0] * _STEPS,
        "pv_forecast": roof,
        "pv_forecasts": {"roof": roof},
        "base_load_forecast": [round(0.6 + 0.4 * (17 <= _local_hour(i) < 22), 3) for i in range(_STEPS)],
        "battery_inputs": {"battery": {"soc_kwh": 2.0}},
        "hybrid_inverter_inputs": {"hybrid": {"soc_kwh": 3.0, "pv_forecast_kw": panels}},
        "ev_inputs": {"car": {"soc_kwh": 30.0, "available": True}},
    })


def _slice(bundle: SolveBundle, i0: int, i1: int) -> SolveBundle:
    """The bundle cut down to steps ``[i0, i1)``, starting at step ``i0``."""
    d = bundle.model_dump()
    for key in ("horizon_prices", "horizon_export_prices", "horizon_confidence",
                "pv_forecast", "base_load_forecast"):
        d[key] = d[key][i0:i1]
    d["pv_forecasts"] = {k: v[i0:i1] for k, v in d["pv_forecasts"].items()}
    for inputs in d["hybrid_inverter_inputs"].values():
        inputs["pv_forecast_kw"] = inputs["pv_forecast_kw"][i0:i1]
    d["solve_time_utc"] = bundle.solve_time_utc + timedelta(minutes=15 * i0)
    return SolveBundle.model_validate(d)


@pytest.fixture(scope="module")
def solved(tmp_path_factory: pytest.TempPathFactory) -> tuple[SolveBundle, MimirheimConfig, SolveResult, dict]:
    """Solve the case, dump it as the solver loop does, render the page from
    the dump pair and return the page's per-day data."""
    bundle, config = _bundle(), MimirheimConfig.model_validate(_CONFIG)
    result = build_and_solve(bundle, config)
    assert result.solve_status == "optimal"
    paths = debug_dump(bundle, result, config, tmp_path_factory.mktemp("dumps"), max_dumps=0)
    assert paths is not None
    inp, out = (json.loads(Path(p).read_text()) for p in paths)
    html = build_household_html(inp, out, tz=_AMS)
    m = re.search(r'<script id="household-data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    return bundle, config, result, json.loads(m.group(1).replace("<\\/", "</"))


def test_the_case_exercises_every_clipping_path() -> None:
    """Guard the case itself: without these it would prove nothing."""
    bundle = _bundle()
    assert max(bundle.pv_forecasts["roof"]) > 2.0  # above the highest register
    assert max(bundle.hybrid_inverter_inputs["hybrid"].pv_forecast_kw) > 1.9  # above max_pv_kw
    assert min(bundle.pv_forecasts["roof"]) < 0.0  # negative at night


def test_the_plan_splits_into_three_days(solved) -> None:
    _, _, _, payload = solved
    assert [(d["i1"] - d["i0"], d["complete"]) for d in payload["days"]] == [
        (8, True), (96, True), (24, False),
    ]


def test_each_days_baseline_is_the_solvers(solved) -> None:
    """Each day's baseline equals the solver's naive_cost_eur for that day's
    steps alone: the baseline depends on the inputs only, so solving the day's
    slice of the bundle gives the solver's own figure for it."""
    bundle, config, result, payload = solved
    for day in payload["days"]:
        expected = build_and_solve(_slice(bundle, day["i0"], day["i1"]), config).naive_cost_eur
        assert day["summary"]["naive"] == pytest.approx(expected, abs=1e-3), day["date"]
    total = sum(d["summary"]["naive"] for d in payload["days"])
    assert total == pytest.approx(result.naive_cost_eur, abs=1e-3)


def test_the_days_stored_energy_adds_up_to_the_solvers_credit(solved) -> None:
    """What each day leaves in storage, summed, is the solver's soc_credit_eur:
    same devices, same cell-side SOC, same discharge efficiencies, same price."""
    _, _, result, payload = solved
    assert result.soc_credit_eur != 0.0
    total = sum(d["summary"]["stored"] for d in payload["days"])
    assert total == pytest.approx(result.soc_credit_eur, abs=1e-3)


def test_the_days_add_up_to_the_solvers_cost_and_saving(solved) -> None:
    """Grid cash per day sums to optimised_cost_eur, and the signed per-day
    savings to the plan's own saving."""
    _, _, result, payload = solved
    days = [d["summary"] for d in payload["days"]]
    assert sum(d["cash"] for d in days) == pytest.approx(result.optimised_cost_eur, abs=1e-3)
    saving = result.naive_cost_eur - (result.optimised_cost_eur - result.soc_credit_eur)
    assert sum(d["saving"] for d in days) == pytest.approx(saving, abs=1e-3)
