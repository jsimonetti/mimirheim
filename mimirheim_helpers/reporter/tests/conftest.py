"""conftest.py — test fixtures for mimirheim-reporter tests.

The dump pair used by the render tests is committed under ``tests/fixtures/``
so the tests are hermetic: they run on a clean checkout, in CI, and on a
machine that has never run mimirheim.

Previously these fixtures were copied at session start from the repo-root
``mimirheim_dumps/`` directory, and skipped when it was absent. That
directory is gitignored, so on CI it never exists and every test that
depended on it skipped on every run, silently. Refreshing the fixtures is now
an explicit developer action rather than a precondition for running the
suite; see ``scripts/refresh_reporter_fixtures.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The specific fixture dump pair used by tests.
_FIXTURE_TS = "2026-04-03T15-30-00Z"
_FIXTURE_INPUT = _FIXTURES_DIR / f"{_FIXTURE_TS}_input.json"
_FIXTURE_OUTPUT = _FIXTURES_DIR / f"{_FIXTURE_TS}_output.json"


@pytest.fixture(scope="session")
def fixture_dump_pair() -> tuple[Path, Path]:
    """Return the (input_path, output_path) of the committed fixture dump pair.

    Fails rather than skips when a fixture is missing. A missing committed
    fixture is a broken checkout, not a reason to quietly stop testing the
    renderer.
    """
    for path in (_FIXTURE_INPUT, _FIXTURE_OUTPUT):
        if not path.exists():
            pytest.fail(
                f"Committed fixture {path.name} is missing from {_FIXTURES_DIR}. "
                "It should be tracked in git; restore it or regenerate the pair "
                "with scripts/refresh_reporter_fixtures.py."
            )
    return (_FIXTURE_INPUT, _FIXTURE_OUTPUT)


@pytest.fixture(scope="session")
def fixture_inp(fixture_dump_pair: tuple[Path, Path]) -> dict:
    """Return the parsed input JSON for the fixture dump."""
    return json.loads(fixture_dump_pair[0].read_text())


@pytest.fixture(scope="session")
def fixture_out(fixture_dump_pair: tuple[Path, Path]) -> dict:
    """Return the parsed output JSON for the fixture dump."""
    return json.loads(fixture_dump_pair[1].read_text())
