"""Unit tests for SOC extraction in ``_render_helpers._read_soc_from_schedule``.

Every device in ``device_meta`` is a storage device, so every one of them
should carry ``soc_kwh`` on each schedule step. When the field is missing the
function still has to return a full-length list, but it must say so: a silent
fallback to zero renders as a flat SOC line that looks like a real measurement
of an idle battery rather than like absent data.
"""
from __future__ import annotations

import logging

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
