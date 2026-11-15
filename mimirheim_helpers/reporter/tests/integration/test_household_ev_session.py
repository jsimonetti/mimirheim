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
_CONFIG = MimirheimConfig.model_validate({
    "mqtt": {"host": "localhost", "client_id": "ev-session"},
    "grid": {"import_limit_kw": 17.0, "export_limit_kw": 17.0},
    "static_loads": {"base": {}},
    "ev_chargers": {
        "car": {
            "capacity_kwh": 60.0, "min_soc_kwh": 5.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 1.0}],
        },
    },
})


def _bundle(k: int, soc_kwh: float) -> SolveBundle:
    """The solve made ``k`` quarter hours after plug-in."""
    return SolveBundle.model_validate({
        "solve_time_utc": (_T0 + timedelta(minutes=15 * k)).isoformat(),
        "triggered_at_utc": (_T0 + timedelta(minutes=15 * k)).isoformat(),
        "horizon_prices": _PRICES[k:k + _HORIZON],
        "horizon_export_prices": [0.05] * _HORIZON,
        "horizon_confidence": [1.0] * _HORIZON,
        "pv_forecast": [0.0] * _HORIZON,
        "base_load_forecast": [0.5] * _HORIZON,
        "ev_inputs": {
            "car": {
                "soc_kwh": soc_kwh, "available": True, "target_soc_kwh": 30.0,
                "window_latest": (_T0 + timedelta(hours=4)).isoformat(),
            },
        },
    })


def _replay(tmp: Path, quarters: int) -> list[tuple[dict, dict, SolveResult]]:
    """Solve every quarter hour from plug-in, taking each plan's first step as
    what happened, and return the dump pairs with their results."""
    pairs = []
    soc = 20.0
    for k in range(quarters + 1):
        bundle = _bundle(k, soc)
        result = build_and_solve(bundle, _CONFIG)
        assert result.solve_status == "optimal"
        paths = debug_dump(bundle, result, _CONFIG, tmp / f"{k:02d}", max_dumps=0)
        assert paths is not None
        inp, out = (json.loads(Path(p).read_text()) for p in paths)
        pairs.append((inp, out, result))
        soc = result.schedule[0].devices["car"].soc_kwh
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
