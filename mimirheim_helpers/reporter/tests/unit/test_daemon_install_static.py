"""Unit tests for ReporterDaemon._install_index_html.

Verifies that the method copies all three expected static files (index.html,
index.css, plotly.min.js) into output_dir when they are absent, does not
overwrite files that already exist, and is called after every report render
so that files deleted between renders are restored.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reporter.daemon import ReporterDaemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_daemon(output_dir: Path) -> ReporterDaemon:
    """Return a ReporterDaemon instance configured to use output_dir.

    Bypasses the MqttDaemon __init__ so no MQTT connection is attempted.
    """
    cfg = MagicMock()
    cfg.reporting.output_dir = output_dir
    daemon = object.__new__(ReporterDaemon)
    daemon._reporter_config = cfg.reporting
    return daemon


# ---------------------------------------------------------------------------
# Tests: _install_index_html copies missing files
# ---------------------------------------------------------------------------


def test_installs_index_html_when_absent(tmp_path: Path) -> None:
    """index.html is written into output_dir when it does not already exist."""
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert (tmp_path / "index.html").exists()


def test_installs_index_css_when_absent(tmp_path: Path) -> None:
    """index.css is written into output_dir when it does not already exist."""
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert (tmp_path / "index.css").exists()


def test_installs_plotly_js_when_absent(tmp_path: Path) -> None:
    """plotly.min.js is written into output_dir when it does not already exist."""
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert (tmp_path / "plotly.min.js").exists()


def test_does_not_overwrite_existing_index_html(tmp_path: Path) -> None:
    """index.html is not overwritten if it already exists."""
    existing = tmp_path / "index.html"
    existing.write_text("custom content")
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert existing.read_text() == "custom content"


def test_does_not_overwrite_existing_index_css(tmp_path: Path) -> None:
    """index.css is not overwritten if it already exists."""
    existing = tmp_path / "index.css"
    existing.write_text("body { color: red; }")
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert existing.read_text() == "body { color: red; }"


def test_does_not_overwrite_existing_plotly_js(tmp_path: Path) -> None:
    """plotly.min.js is not overwritten if it already exists."""
    existing = tmp_path / "plotly.min.js"
    existing.write_text("// custom plotly")
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    assert existing.read_text() == "// custom plotly"


def test_plotly_js_is_real_library(tmp_path: Path) -> None:
    """plotly.min.js installed into output_dir is the real Plotly library (> 100 kB)."""
    daemon = _make_daemon(tmp_path)
    daemon._install_index_html()
    plotly_dest = tmp_path / "plotly.min.js"
    assert plotly_dest.stat().st_size > 100_000, (
        "plotly.min.js should be the real minified library (> 100 kB)"
    )


# ---------------------------------------------------------------------------
# Tests: static files are restored after deletion (per-render behaviour)
# ---------------------------------------------------------------------------


def test_render_and_save_restores_deleted_static_files(
    tmp_path: Path, fixture_dump_pair: tuple[Path, Path]
) -> None:
    """Static assets deleted between renders are reinstalled by _render_and_save."""
    import json

    from reporter.daemon import ReporterDaemon

    input_path, output_path = fixture_dump_pair

    cfg = MagicMock()
    cfg.output_dir = tmp_path
    cfg.max_reports = 0
    daemon = object.__new__(ReporterDaemon)
    daemon._reporter_config = cfg

    # Perform a first render so the static files are created.
    ts = "2026-04-03T15:30:00Z"
    safe_ts = "2026-04-03T15-30-00Z"
    daemon._render_and_save(ts, safe_ts, f"{safe_ts}_report.html", input_path, output_path)

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "index.css").exists()
    assert (tmp_path / "plotly.min.js").exists()

    # Simulate the static files being deleted (e.g. the output volume was remounted).
    (tmp_path / "index.html").unlink()
    (tmp_path / "index.css").unlink()
    (tmp_path / "plotly.min.js").unlink()

    # A second render should restore all three files.
    ts2 = "2026-04-03T15:45:00Z"
    safe_ts2 = "2026-04-03T15-45-00Z"
    daemon._render_and_save(ts2, safe_ts2, f"{safe_ts2}_report.html", input_path, output_path)

    assert (tmp_path / "index.html").exists(), "index.html was not restored after deletion"
    assert (tmp_path / "index.css").exists(), "index.css was not restored after deletion"
    assert (tmp_path / "plotly.min.js").exists(), "plotly.min.js was not restored after deletion"
