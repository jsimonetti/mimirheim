"""Unit tests for the daemon's stable household page.

The household view is written to a single stable filename (``household.html``)
reflecting the newest solve, alongside the per-solve technical reports. These
tests verify the write, the newest-wins refresh, that a household render
failure is isolated from the rest of the daemon, and which of today's earlier
dumps the page is given.

They mirror ``test_daemon_install_static.py``: the daemon is constructed via
``object.__new__`` with a stub ``_reporter_config`` so no MQTT connection is
attempted.
"""
from __future__ import annotations

import copy
import json
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reporter.daemon import ReporterDaemon


def _embedded_payload(html: str) -> dict:
    m = re.search(
        r'<script id="household-data" type="application/json">(.*?)</script>',
        html,
        re.DOTALL,
    )
    assert m, "household page must embed a household-data JSON block"
    return json.loads(m.group(1).replace("<\\/", "</"))


def _daemon(output_dir: Path, dump_dir: Path | None = None) -> ReporterDaemon:
    cfg = MagicMock()
    cfg.output_dir = output_dir
    # A real, possibly empty, directory: writing the page also reads today's
    # earlier dumps from it.
    cfg.dump_dir = dump_dir if dump_dir is not None else output_dir
    daemon = object.__new__(ReporterDaemon)
    daemon._reporter_config = cfg
    return daemon


def test_write_household_creates_stable_page(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict
) -> None:
    """_write_household writes household.html with the household view."""
    daemon = _daemon(tmp_path)
    daemon._write_household(fixture_inp, fixture_out)
    page = tmp_path / "household.html"
    assert page.exists()
    content = page.read_text()
    assert "household-data" in content
    assert "plotly" not in content.lower()


def test_write_household_overwrites_latest_wins(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict
) -> None:
    """Writing again overwrites the stable page with the newer solve."""
    daemon = _daemon(tmp_path)
    daemon._write_household(fixture_inp, fixture_out)

    newer_inp = dict(fixture_inp, triggered_at_utc="2026-04-03T18:00:00Z")
    daemon._write_household(newer_inp, fixture_out)

    d = _embedded_payload((tmp_path / "household.html").read_text())
    assert d["summary"]["solve"] == "2026-04-03T18:00:00Z"


def test_write_household_is_fault_isolated(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict, monkeypatch
) -> None:
    """A render failure is logged and swallowed, never raised."""
    import reporter.daemon as daemon_mod

    def boom(_inp, _out):
        raise RuntimeError("render exploded")

    monkeypatch.setattr(daemon_mod, "build_household_html", boom)
    daemon = _daemon(tmp_path)
    # Must not raise.
    daemon._write_household(fixture_inp, fixture_out)
    assert not (tmp_path / "household.html").exists()


def test_household_disabled_writes_nothing(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict
) -> None:
    """With household_enabled false, no household page is written."""
    daemon = _daemon(tmp_path)
    daemon._reporter_config.household_enabled = False
    daemon._write_household(fixture_inp, fixture_out)
    assert not (tmp_path / "household.html").exists()


def test_refresh_household_latest_picks_newest(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict
) -> None:
    """Startup refresh writes the household page from the newest dump pair."""
    dump_dir = tmp_path / "dumps"
    out_dir = tmp_path / "out"
    dump_dir.mkdir()
    out_dir.mkdir()

    older = "2026-04-03T15-30-00Z"
    newer = "2026-04-03T16-45-00Z"
    (dump_dir / f"{older}_input.json").write_text(
        json.dumps(dict(fixture_inp, triggered_at_utc="2026-04-03T15:30:00Z"))
    )
    (dump_dir / f"{older}_output.json").write_text(json.dumps(fixture_out))
    (dump_dir / f"{newer}_input.json").write_text(
        json.dumps(dict(fixture_inp, triggered_at_utc="2026-04-03T16:45:00Z"))
    )
    (dump_dir / f"{newer}_output.json").write_text(json.dumps(fixture_out))

    daemon = _daemon(out_dir, dump_dir)
    daemon._refresh_household_latest()

    d = _embedded_payload((out_dir / "household.html").read_text())
    assert d["summary"]["solve"] == "2026-04-03T16:45:00Z"


@pytest.fixture
def amsterdam(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run the reporter in Europe/Amsterdam, whatever the machine's zone."""
    monkeypatch.setenv("TZ", "Europe/Amsterdam")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _write_pair(dump_dir: Path, ts_file: str, out: str | None = "{}") -> None:
    """A dump pair whose input names its own file; ``out=None`` leaves the
    output missing."""
    (dump_dir / f"{ts_file}_input.json").write_text(json.dumps({"file": ts_file}))
    if out is not None:
        (dump_dir / f"{ts_file}_output.json").write_text(out)


@pytest.mark.usefixtures("amsterdam")
def test_earlier_pairs_are_todays_before_this_plan(tmp_path: Path) -> None:
    """This plan starts at 12:00 Amsterdam (10:00 UTC): today began at 22:00 UTC.

    The window opens an hour earlier for a clock change since midnight; the
    view itself drops what is not on today's date. This plan and later ones
    are not "earlier", and a pair that cannot be read is skipped.
    """
    for ts_file in (
        "2026-09-30T20-45-00Z",  # before the window
        "2026-09-30T21-30-00Z",  # in the hour of margin
        "2026-10-01T05-00-00Z",
        "2026-10-01T09-45-00Z",
        "2026-10-01T10-00-00Z",  # this plan
        "2026-10-01T10-15-00Z",  # later
    ):
        _write_pair(tmp_path, ts_file)
    _write_pair(tmp_path, "2026-10-01T06-00-00Z", out="not json")
    _write_pair(tmp_path, "2026-10-01T07-00-00Z", out=None)
    daemon = _daemon(tmp_path, tmp_path)

    pairs = daemon._earlier_pairs({"schedule": [{"t": "2026-10-01T10:00:00Z"}]})

    assert [inp["file"] for inp, _ in pairs] == [
        "2026-09-30T21-30-00Z",
        "2026-10-01T05-00-00Z",
        "2026-10-01T09-45-00Z",
    ]
    assert daemon._earlier_pairs({"schedule": []}) == []


@pytest.mark.usefixtures("amsterdam")
def test_household_page_covers_today_from_earlier_dumps(
    tmp_path: Path, fixture_inp: dict, fixture_out: dict
) -> None:
    """A dump from earlier today puts its quarter hour on the page's today.

    The fixture's plan starts at 17:30 Amsterdam; a plan made at 12:00 the
    same day makes today start there.
    """
    earlier = copy.deepcopy(fixture_out)
    t0 = datetime.fromisoformat("2026-04-03T10:00:00+00:00")
    for i, step in enumerate(earlier["schedule"]):
        step["t"] = (t0 + timedelta(minutes=15 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp_path / "2026-04-03T10-00-00Z_input.json").write_text(json.dumps(fixture_inp))
    (tmp_path / "2026-04-03T10-00-00Z_output.json").write_text(json.dumps(earlier))
    daemon = _daemon(tmp_path, tmp_path)

    daemon._write_household(fixture_inp, fixture_out)

    today = _embedded_payload((tmp_path / "household.html").read_text())["days"][0]
    assert today["past_from"] == "12:00"
