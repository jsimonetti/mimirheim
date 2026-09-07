"""Unit tests for config_editor.cli (plan 70 decision 1: --validate-schemas).

Exercises validate_schemas() directly against a temp config directory, the
same way the real CLI would call it -- registry.build_registry is not
mocked, so these tests also prove the CLI and the server can never disagree
about what is valid: they call the exact same discovery function.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_editor import cli


def _write_schema(directory: Path, filename: str, content: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(json.dumps(content))


def test_valid_dropin_reports_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    schemas_dir = tmp_path / "schemas"
    _write_schema(
        schemas_dir,
        "widget.schema.json",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}},
            "x-mimirheim": {"file": "widget.yaml", "category": "other", "python_package": "widget"},
        },
    )

    exit_code = cli.validate_schemas(tmp_path)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK widget" in out


def test_invalid_dropin_reports_exact_rejection_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schemas_dir = tmp_path / "schemas"
    _write_schema(
        schemas_dir,
        "broken.schema.json",
        {"type": "object", "additionalProperties": False, "properties": {}},
    )

    exit_code = cli.validate_schemas(tmp_path)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert (
        "REJECTED broken.schema.json: x-mimirheim is missing; every mimirheim helper "
        "schema must declare an x-mimirheim envelope (SPEC.md §3)." in out
    )


def test_valid_and_invalid_dropins_both_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    schemas_dir = tmp_path / "schemas"
    _write_schema(
        schemas_dir,
        "widget.schema.json",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"enabled": {"type": "boolean"}},
            "x-mimirheim": {"file": "widget.yaml", "category": "other", "python_package": "widget"},
        },
    )
    _write_schema(
        schemas_dir,
        "broken.schema.json",
        {"type": "object", "additionalProperties": False, "properties": {}},
    )

    exit_code = cli.validate_schemas(tmp_path)

    out = capsys.readouterr().out
    assert exit_code == 1
    assert "OK widget" in out
    assert "REJECTED broken.schema.json:" in out


def test_no_dropins_still_reports_every_bundled_schema_ok(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Step 0's safety check: run against the real bundled schemas with no drop-ins."""
    exit_code = cli.validate_schemas(tmp_path)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "OK mimirheim" in out
    assert "REJECTED" not in out
