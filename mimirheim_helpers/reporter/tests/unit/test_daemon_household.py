"""Unit tests for the daemon's stable household page.

The household view is written to a single stable filename (``household.html``)
reflecting the newest solve, alongside the per-solve technical reports. These
tests verify the write, the newest-wins refresh, and that a household render
failure is isolated from the rest of the daemon.

They mirror ``test_daemon_install_static.py``: the daemon is constructed via
``object.__new__`` with a stub ``_reporter_config`` so no MQTT connection is
attempted.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

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
    cfg.dump_dir = dump_dir
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
