"""The household view's naive car across a plug-in session.

The page's "today" stitches the quarter hours already past from the first step
of each earlier plan. Every plan's own naive baseline starts the car at its
first step, so stitching those would make the naive car follow the real one
and the saving of smart charging would vanish as its quarters became history.

This test replays a morning: the car is plugged in at 08:00 needing 10 kWh by
12:00, the first two hours cost 0.30 EUR/kWh and the last two 0.10. The real
solver is run every quarter hour, each plan's first step is taken as what
happened, and the page is rendered at 10:00 from the current plan and the
eight earlier ones. The naive car charges at 08:00 at full power, so today's
saving must be the 10 kWh x 0.20 EUR/kWh that waiting for the cheap hours
saves, however many quarters are already past.
"""
from __future__ import annotations

import json
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
_T0 = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # 08:00 Amsterdam
_PRICES = [0.30] * 8 + [0.10] * 24
_HORIZON = 16
_CONFIG = {
    "mqtt": {"host": "localhost", "client_id": "ev-session"},
    "grid": {"import_limit_kw": 17.0, "export_limit_kw": 17.0},
    "static_loads": {"base": {}},
}
_CAR = {
    "capacity_kwh": 60.0, "min_soc_kwh": 5.0,
    "charge_segments": [{"power_max_kw": 11.0, "efficiency": 1.0}],
}


def _config(ev: bool, dishwasher: bool) -> MimirheimConfig:
    cfg = dict(_CONFIG)
    if ev:
        cfg["ev_chargers"] = {"car": _CAR}
    if dishwasher:
        cfg["deferrable_loads"] = {"dishwasher": {"power_profile": [2.0]}}
    return MimirheimConfig.model_validate(cfg)


def _bundle(
    t0: datetime, k: int, soc_kwh: float, *, ev: bool, dishwasher: str
) -> SolveBundle:
    """The solve made ``k`` quarter hours after ``t0``. ``dishwasher`` is
    "window" while it waits, "ran" with its committed start, or ""."""
    now = t0 + timedelta(minutes=15 * k)
    data: dict = {
        "solve_time_utc": now.isoformat(),
        "triggered_at_utc": now.isoformat(),
        "horizon_prices": _PRICES[k:k + _HORIZON],
        "horizon_export_prices": [0.05] * _HORIZON,
        "horizon_confidence": [1.0] * _HORIZON,
        "pv_forecast": [0.0] * _HORIZON,
        "base_load_forecast": [0.5] * _HORIZON,
    }
    if ev:
        data["ev_inputs"] = {
            "car": {
                "soc_kwh": soc_kwh, "available": True, "target_soc_kwh": 30.0,
                "window_latest": (t0 + timedelta(hours=4)).isoformat(),
            },
        }
    if dishwasher == "window":
        data["deferrable_windows"] = {
            # Must finish by 10:15, so 10:00 is its only cheap start.
            "dishwasher": {"earliest": t0.isoformat(), "latest": (t0 + timedelta(minutes=135)).isoformat()},
        }
    elif dishwasher == "ran":
        data["deferrable_start_times"] = {"dishwasher": (t0 + timedelta(hours=2)).isoformat()}
    return SolveBundle.model_validate(data)


def _replay(
    tmp: Path, quarters: int, *, t0: datetime = _T0, ev: bool = True, dishwasher: bool = False,
) -> list[tuple[dict, dict, SolveResult]]:
    """Solve every quarter hour from ``t0``, taking each plan's first step as
    what happened, and return the dump pairs with their results. The
    dishwasher's automation commits the start once the plan runs it."""
    config = _config(ev, dishwasher)
    pairs = []
    soc = 20.0
    state = "window" if dishwasher else ""
    for k in range(quarters + 1):
        bundle = _bundle(t0, k, soc, ev=ev, dishwasher=state)
        result = build_and_solve(bundle, config)
        assert result.solve_status == "optimal"
        paths = debug_dump(bundle, result, config, tmp / f"{k:02d}", max_dumps=0)
        assert paths is not None
        inp, out = (json.loads(Path(p).read_text()) for p in paths)
        pairs.append((inp, out, result))
        if ev:
            soc = result.schedule[0].devices["car"].soc_kwh
        if state == "window" and result.schedule[0].devices["dishwasher"].kw < 0.0:
            state = "ran"
    return pairs


def _today(pairs: list[tuple[dict, dict, SolveResult]]) -> dict:
    inp, out, _ = pairs[-1]
    earlier = [(i, o) for i, o, _ in pairs[:-1]]
    html = build_household_html(inp, out, tz=_AMS, earlier=earlier)
    m = re.search(r'<script id="household-data" type="application/json">(.*?)</script>', html, re.S)
    assert m
    return json.loads(m.group(1).replace("<\\/", "</"))["days"][0]["summary"]


@pytest.mark.parametrize("quarters", [0, 4, 8, 12])
def test_the_saving_of_waiting_for_cheap_hours_survives_into_history(
    tmp_path: Path, quarters: int
) -> None:
    pairs = _replay(tmp_path, quarters)
    # Guard the replay: the car waits for the cheap hours and gets its 10 kWh.
    first_plan = pairs[0][2]
    assert all(s.devices["car"].kw == 0.0 for s in first_plan.schedule[:8])
    assert first_plan.schedule[-1].devices["car"].soc_kwh == pytest.approx(30.0, abs=1e-4)
    summary = _today(pairs)
    assert summary["saving"] == pytest.approx(2.00, abs=1e-3)


def test_a_session_carries_across_midnight(tmp_path: Path) -> None:
    """Plugged in at 23:00 the evening before: the naive car charges then, at
    0.30 EUR/kWh, and has its 10 kWh before midnight. The real car waits for
    the cheap hours after 01:00. Today's baseline must not charge the naive
    car a second time just because the date changed."""
    t0 = datetime(2026, 11, 2, 22, 0, tzinfo=UTC)  # 23:00 Amsterdam
    pairs = _replay(tmp_path, 10, t0=t0)  # rendered at 01:30
    inp, out, result = pairs[-1]
    summary = _today(pairs)
    # Today's quarters are 00:00 to 01:15 from history and the current plan
    # from 01:30. The naive car owes nothing in any of them, so today's
    # baseline is the base load alone: 0.5 kW at each step's price.
    today = [step for step in out["schedule"] if step["t"] < "2026-11-03T23:00"]
    base = 0.5 * 0.25 * (
        sum(_PRICES[4:10]) + sum(s["import_price_eur_per_kwh"] for s in today)
    )
    assert summary["naive"] == pytest.approx(base, abs=1e-3)


@pytest.mark.parametrize(("quarters", "saving"), [(0, 0.10), (4, 0.10), (10, 0.0)])
def test_a_delayed_dishwasher_shows_its_saving_until_it_runs(
    tmp_path: Path, quarters: int, saving: float
) -> None:
    """A one-quarter 2 kW dishwasher with a window from 08:00, run at 10:00
    at 0.10 EUR/kWh instead of 08:00 at 0.30. Deferrable loads are not
    followed across plans: while a plan still has a recommended start, today
    shows what the delay saves, 0.5 kWh x 0.20 EUR/kWh; once it has run, its
    quarter is drawn as planned and the saving is gone. A documented
    limitation, pinned here so a change to it is deliberate."""
    pairs = _replay(tmp_path, quarters, ev=False, dishwasher=True)
    assert pairs[0][2].deferrable_recommended_starts["dishwasher"] == _T0 + timedelta(hours=2)
    assert _today(pairs)["saving"] == pytest.approx(saving, abs=1e-3)
