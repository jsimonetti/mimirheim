"""conftest.py — test fixtures for mimirheim-reporter tests.

Copies a real dump pair from the repo's ``mimirheim_dumps/`` directory into
``tests/fixtures/`` so tests are self-contained and do not depend on the
location of the workspace root.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

# Path to the mimirheim_dumps directory relative to the workspace root.
# Walk up from this file: tests/ -> reporter/ -> mimirheim_helpers/ -> mimirheim/
_REPO_ROOT = Path(__file__).parent.parent.parent.parent
_DUMPS_DIR = _REPO_ROOT / "mimirheim_dumps"
_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The specific fixture dump pair used by tests.
_FIXTURE_TS = "2026-04-03T15-30-00Z"
_FIXTURE_INPUT = _DUMPS_DIR / f"{_FIXTURE_TS}_input.json"
_FIXTURE_OUTPUT = _DUMPS_DIR / f"{_FIXTURE_TS}_output.json"


@pytest.fixture(scope="session")
def copy_fixture_dumps() -> None:
    """Copy one real dump pair into tests/fixtures/ for offline test use."""
    if not _FIXTURE_INPUT.exists() or not _FIXTURE_OUTPUT.exists():
        pytest.skip(
            f"Fixture dump pair not found in {_DUMPS_DIR!s}. "
            "Run mimirheim to generate dump files first."
        )
    _FIXTURES_DIR.mkdir(exist_ok=True)
    shutil.copy2(_FIXTURE_INPUT, _FIXTURES_DIR / _FIXTURE_INPUT.name)
    shutil.copy2(_FIXTURE_OUTPUT, _FIXTURES_DIR / _FIXTURE_OUTPUT.name)


@pytest.fixture(scope="session")
def fixture_dump_pair(copy_fixture_dumps: None) -> tuple[Path, Path]:
    """Return the (input_path, output_path) of the fixture dump pair."""
    return (
        _FIXTURES_DIR / _FIXTURE_INPUT.name,
        _FIXTURES_DIR / _FIXTURE_OUTPUT.name,
    )


@pytest.fixture(scope="session")
def fixture_inp(fixture_dump_pair: tuple[Path, Path]) -> dict:
    """Return the parsed input JSON for the fixture dump."""
    return json.loads(fixture_dump_pair[0].read_text())


@pytest.fixture(scope="session")
def fixture_out(fixture_dump_pair: tuple[Path, Path]) -> dict:
    """Return the parsed output JSON for the fixture dump."""
    return json.loads(fixture_dump_pair[1].read_text())
