"""Unit tests for reporter/baseline.py: the naive flexible load across plans."""
from __future__ import annotations

import pytest

from reporter.baseline import naive_overrides


def _plan(t: str, soc: float, end_soc: float, power_kw: float) -> tuple[dict, dict]:
    """A dump pair for one plan: a charge-only car at ``soc`` that the plan
    takes to ``end_soc``, on a charger of ``power_kw`` at 100%."""
    inp = {
        "solve_time_utc": t,
        "config": {
            "ev_chargers": {
                "car": {"charge_segments": [{"power_max_kw": power_kw, "efficiency": 1.0}]},
            },
        },
        "ev_inputs": {"car": {"soc_kwh": soc, "available": True}},
    }
    out = {
        "schedule": [
            {"t": t, "devices": {"car": {"kw": 0.0, "type": "ev_charger", "soc_kwh": soc}}},
            {"t": t, "devices": {"car": {"kw": 0.0, "type": "ev_charger", "soc_kwh": end_soc}}},
        ],
    }
    return inp, out


def test_each_quarter_charges_at_its_own_plans_rate() -> None:
    """The car needs 2 kWh and waits through the first quarter, when the
    charger was configured at 4 kW. That quarter's naive car takes 1 kWh, as
    much as 4 kW gives in 15 minutes, even though the charger is at 8 kW by
    the current plan. The current plan's naive car then still owes 1 kWh."""
    earlier_inp, earlier_out = _plan("2026-11-02T07:00:00Z", 20.0, 22.0, 4.0)
    inp, out = _plan("2026-11-02T07:15:00Z", 20.0, 22.0, 8.0)

    per_quarter, current = naive_overrides(
        [(earlier_out["schedule"][0], earlier_inp, earlier_out)], inp, out, 0.25
    )

    assert per_quarter[0].ev_needed["car"] == pytest.approx(2.0)
    assert current.ev_needed["car"] == pytest.approx(1.0)


def test_unplugging_ends_the_session() -> None:
    """After an unplugged quarter the naive car starts over: the energy the
    previous session's naive car took no longer counts against the new one."""
    first_inp, first_out = _plan("2026-11-02T07:00:00Z", 20.0, 22.0, 8.0)
    gone_inp, gone_out = _plan("2026-11-02T07:15:00Z", 20.0, 22.0, 8.0)
    gone_inp["ev_inputs"]["car"]["available"] = False
    inp, out = _plan("2026-11-02T07:30:00Z", 20.0, 22.0, 8.0)
    history = [
        (first_out["schedule"][0], first_inp, first_out),
        (gone_out["schedule"][0], gone_inp, gone_out),
    ]

    _, current = naive_overrides(history, inp, out, 0.25)

    assert current.ev_needed["car"] == pytest.approx(2.0)
