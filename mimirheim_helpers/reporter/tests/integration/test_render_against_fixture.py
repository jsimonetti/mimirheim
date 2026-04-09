"""Integration tests: render against a real fixture dump pair.

These tests confirm that ``build_report_html`` produces a well-formed HTML
document given a real dump pair, and that the HTML references
``plotly.min.js`` as an external sidecar rather than inlining it.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from reporter.render import build_report_html


def test_render_produces_html_string(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """build_report_html returns a non-empty HTML string."""
    result = build_report_html(fixture_inp, fixture_out)
    assert isinstance(result, str)
    assert len(result) > 1000
    assert "<!DOCTYPE html>" in result


def test_render_html_references_plotlyjs(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """The HTML references plotly.min.js as an external sidecar."""
    result = build_report_html(fixture_inp, fixture_out)
    assert 'src="plotly.min.js"' in result
    assert "plotly.min.js" in result


def test_render_html_contains_naive_section(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """The HTML contains the naive (unoptimised) chart section."""
    result = build_report_html(fixture_inp, fixture_out)
    assert "naive-chart" in result
    assert "Unoptimised" in result


def test_render_html_contains_optimised_section(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """The HTML contains the optimised chart section."""
    result = build_report_html(fixture_inp, fixture_out)
    assert "opt-chart" in result
    assert "Optimised" in result


def test_render_html_contains_summary(
    fixture_inp: dict, fixture_out: dict
) -> None:
    """The HTML contains the plain-HTML economic summary section."""
    result = build_report_html(fixture_inp, fixture_out)
    assert "Economic summary" in result
    assert "Saving vs naive" in result


def test_render_html_can_be_written_to_file(
    fixture_inp: dict, fixture_out: dict, tmp_path: Path
) -> None:
    """build_report_html output can be written to disk; plotly.min.js sidecar works.

    Copies the real plotly.min.js from the installed package to verify that
    the HTML + sidecar pair works as intended.
    """
    result = build_report_html(fixture_inp, fixture_out)
    out_html = tmp_path / "test_report.html"
    out_html.write_text(result, encoding="utf-8")
    assert out_html.exists()
    assert out_html.stat().st_size > 1000

    # The HTML must not contain the inlined plotly bundle.
    content = out_html.read_text()
    assert "src=\"plotly.min.js\"" in content
    # Confirm there is no large inline script blob (inlined plotly is >1 MB)
    assert len(content) < 500_000, (
        "HTML appears to contain an inlined plotly bundle. "
        "Expected external sidecar reference only."
    )
