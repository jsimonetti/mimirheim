"""Unit tests for SOC extraction in ``_render_helpers._read_soc_from_schedule``.

Every device in ``device_meta`` is a storage device, so every one of them
should carry ``soc_kwh`` on each schedule step. When the field is missing the
function still has to return a full-length list, but it must say so: a silent
fallback to zero renders as a flat SOC line that looks like a real measurement
of an idle battery rather than like absent data.
"""
from __future__ import annotations

import logging
import re

from reporter._render_helpers import _read_soc_from_schedule

_META = {"bat": {"dtype": "battery", "capacity_kwh": 10.0}}


def _step(devices: dict) -> dict:
    return {"grid_import_kw": 0.0, "grid_export_kw": 0.0, "devices": devices}


def test_reads_soc_from_each_step() -> None:
    """The happy path returns the solver's own end-of-step SOC values."""
    schedule = [
        _step({"bat": {"kw": -2.0, "type": "battery", "soc_kwh": 4.0}}),
        _step({"bat": {"kw": -2.0, "type": "battery", "soc_kwh": 4.5}}),
    ]

    assert _read_soc_from_schedule(schedule, _META) == {"bat": [4.0, 4.5]}


def test_zero_soc_is_kept_not_treated_as_missing(caplog) -> None:
    """A genuine 0.0 SOC is data, not an absent field, and must not warn.

    An empty battery reports 0.0. Conflating that with a missing key would
    make the warning fire on correct dumps and train the reader to ignore it.
    """
    schedule = [_step({"bat": {"kw": 0.0, "type": "battery", "soc_kwh": 0.0}})]

    with caplog.at_level(logging.WARNING):
        result = _read_soc_from_schedule(schedule, _META)

    assert result == {"bat": [0.0]}
    assert caplog.text == ""


def test_missing_soc_warns_and_names_the_device(caplog) -> None:
    """A storage device with no soc_kwh must produce a warning, not silence.

    This is the failure that produced flat SOC charts for months: the writer
    omitted the field, the reader defaulted it to zero, and nothing anywhere
    said so.
    """
    schedule = [
        _step({"bat": {"kw": -2.0, "type": "battery"}}),
        _step({"bat": {"kw": -2.0, "type": "battery"}}),
    ]

    with caplog.at_level(logging.WARNING):
        result = _read_soc_from_schedule(schedule, _META)

    assert result == {"bat": [0.0, 0.0]}
    assert "bat" in caplog.text
    assert "soc_kwh" in caplog.text


def test_missing_soc_warns_once_per_device_not_once_per_step(caplog) -> None:
    """A 288-step dump must not emit 288 identical warnings."""
    schedule = [_step({"bat": {"kw": 0.0, "type": "battery"}}) for _ in range(288)]

    with caplog.at_level(logging.WARNING):
        _read_soc_from_schedule(schedule, _META)

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_partially_missing_soc_warns(caplog) -> None:
    """A field present on some steps and not others is still a defect."""
    schedule = [
        _step({"bat": {"kw": -2.0, "type": "battery", "soc_kwh": 4.0}}),
        _step({"bat": {"kw": -2.0, "type": "battery"}}),
    ]

    with caplog.at_level(logging.WARNING):
        result = _read_soc_from_schedule(schedule, _META)

    assert result == {"bat": [4.0, 0.0]}
    assert "bat" in caplog.text


def test_device_absent_from_schedule_warns(caplog) -> None:
    """A device in the config but not in the schedule is also worth flagging."""
    schedule = [_step({"other": {"kw": 1.0, "type": "pv"}})]

    with caplog.at_level(logging.WARNING):
        result = _read_soc_from_schedule(schedule, _META)

    assert result == {"bat": [0.0]}
    assert "bat" in caplog.text


# ---------------------------------------------------------------------------
# Time axis: read from the dump, never recomputed
# ---------------------------------------------------------------------------


def _minimal_dumps(n_steps: int) -> tuple[dict, dict]:
    """An input/output dump pair small enough to render, with n_steps steps."""
    xs = [f"2026-08-23T{12 + i // 4:02d}:{(i % 4) * 15:02d}:00Z" for i in range(n_steps)]
    inp = {
        "solve_time_utc": "2026-08-23T12:00:00Z",
        "strategy": "minimize_cost",
        "horizon_prices": [0.20] * n_steps,
        "horizon_export_prices": [0.05] * n_steps,
        "horizon_confidence": [1.0] * n_steps,
        "pv_forecast": [0.0] * n_steps,
        "base_load_forecast": [0.5] * n_steps,
        "battery_inputs": {"bat": {"soc_kwh": 5.0}},
        "config": {
            "batteries": {
                "bat": {
                    "capacity_kwh": 10.0,
                    "min_soc_kwh": 0.0,
                    "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                    "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                }
            }
        },
    }
    out = {
        "strategy": "minimize_cost",
        "solve_status": "optimal",
        "objective_value": 1.0,
        "dispatch_suppressed": False,
        "naive_cost_eur": 1.0,
        "optimised_cost_eur": 0.5,
        "soc_credit_eur": 0.0,
        "schedule": [
            {
                "t": xs[i],
                "import_price_eur_per_kwh": 0.20,
                "export_price_eur_per_kwh": 0.05,
                "grid_import_kw": 0.5,
                "grid_export_kw": 0.0,
                "devices": {"bat": {"kw": 0.0, "type": "battery", "soc_kwh": 5.0}},
            }
            for i in range(n_steps)
        ],
    }
    return inp, out


def test_x_axis_comes_from_the_schedule_timestamps() -> None:
    """Traces must be plotted against the step timestamps written in the dump.

    The dump carries an absolute ISO timestamp per step, so the reporter has
    no reason to rebuild a time axis from solve_time_utc and an assumed step
    spacing.
    """
    from reporter.render import build_report_html

    inp, out = _minimal_dumps(4)
    html = build_report_html(inp, out)

    for step in out["schedule"]:
        assert step["t"] in html, f"step timestamp {step['t']} missing from the report"


def _x_arrays(html: str) -> list[str]:
    """Return the raw text of every Plotly trace "x" array found in the page.

    The traces are emitted as JSON inside the Plotly.newPlot call, so the x
    arrays can be pulled out textually. Crude, but it lets a test distinguish
    "this timestamp is on a chart axis" from "this timestamp is in a caption",
    which is the whole point here.
    """
    return re.findall(r'"x":\s*\[(.*?)\]', html, flags=re.S)


def test_x_axis_ignores_a_stale_solve_time_in_the_input() -> None:
    """The schedule's timestamps win over solve_time_utc if the two disagree.

    Guards the ordering: reading the per-step field must not become a fallback
    behind a recomputation from the input.

    solve_time_utc has legitimate uses elsewhere on the page. It labels the
    title, the heading, and the "Solve time (UTC)" summary row, so this asserts
    on the chart axes specifically rather than on the document as a whole.
    """
    from reporter.render import build_report_html

    inp, out = _minimal_dumps(4)
    inp["solve_time_utc"] = "1999-01-01T00:00:00Z"

    html = build_report_html(inp, out)
    axes = _x_arrays(html)

    assert axes, "no chart axes found in the report"
    for axis in axes:
        assert "1999" not in axis, "solve_time_utc reached a chart axis"
    assert any(out["schedule"][0]["t"] in axis for axis in axes), (
        "no axis was built from the schedule's own timestamps"
    )


def test_empty_schedule_still_renders() -> None:
    """An infeasible solve has no steps; rendering must not raise."""
    from reporter.render import build_report_html

    inp, out = _minimal_dumps(4)
    out["schedule"] = []
    out["solve_status"] = "infeasible"

    build_report_html(inp, out)


def test_report_html_renders_without_recomputed_times() -> None:
    """The HTML path carries the same duplicated block; cover it too."""
    from reporter.render import build_report_html

    inp, out = _minimal_dumps(4)
    html = build_report_html(inp, out)

    assert "2026-08-23T12:00:00Z" in html
