"""pytest configuration and fixtures for the mimirheim test suite.

This module wires up the golden file scenario testing infrastructure and the
shared in-process MQTT broker fixture used by integration tests.

- ``--update-golden`` CLI flag: when set, each scenario test writes its
  ``SolveResult`` to ``golden.json`` instead of comparing against a stored one.
- ``scenario_dir`` fixture: parameterised over all directories found under
  ``tests/scenarios/``.
- ``mqtt_broker`` fixture: starts an in-process amqtt broker on port 11884 and
  yields the broker URL. Used by integration tests to avoid depending on an
  external broker.

Usage:

    uv run pytest tests/scenarios/                  # assert against golden files
    uv run pytest tests/scenarios/ --update-golden  # regenerate golden files

Input format:
    Each scenario directory must contain ``input.json`` (a serialised
    ``SolveBundle``) and ``config.yaml`` (a serialised ``MimirheimConfig``).
"""

from pathlib import Path

import asyncio
import pytest
from amqtt.broker import Broker

_SCENARIOS_DIR = Path(__file__).parent / "scenarios"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the --update-golden flag."""
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden.json files from the current solver output.",
    )


def _discover_scenarios() -> list[Path]:
    """Return a sorted list of all scenario directories that contain input.json."""
    if not _SCENARIOS_DIR.exists():
        return []
    return sorted(
        d for d in _SCENARIOS_DIR.iterdir()
        if d.is_dir() and (d / "input.json").exists()
    )


@pytest.fixture(params=_discover_scenarios(), ids=lambda p: p.name)
def scenario_dir(request: pytest.FixtureRequest) -> Path:
    """Yield the path to one scenario directory."""
    return request.param  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Integration test fixtures
# ---------------------------------------------------------------------------

_BROKER_PORT = 11884


@pytest.fixture
async def mqtt_broker() -> object:
    """Start an in-process amqtt broker on port 11884, yield its URL, stop on teardown.

    Uses port 11884 to avoid conflicting with any locally running broker on 1883.
    The broker is configured with anonymous authentication and no persistence.
    All integration tests that require MQTT connectivity should receive this
    fixture.

    Yields:
        The broker URL string, e.g. ``"mqtt://127.0.0.1:11884"``.
    """
    config = {
        "listeners": {
            "default": {"type": "tcp", "bind": f"127.0.0.1:{_BROKER_PORT}"},
        },
    }
    broker = Broker(config)
    await broker.start()
    yield f"mqtt://127.0.0.1:{_BROKER_PORT}"
    # Best-effort shutdown. When a test fails before disconnecting its paho
    # clients, wait_closed() may not complete within the timeout window. We
    # suppress the TimeoutError because the broker is torn down along with the
    # test process regardless; letting the error propagate would mask the
    # original test failure.
    try:
        await asyncio.wait_for(broker.shutdown(), timeout=5.0)
    except (asyncio.TimeoutError, TimeoutError):
        pass

