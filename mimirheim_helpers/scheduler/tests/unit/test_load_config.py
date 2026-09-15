"""Unit tests for the scheduler's use of helper_common.config.load_helper_config.

The scheduler has no bespoke ``load_config`` of its own any more (config-owner-
startup-resilience ticket 03): ``__main__.main`` calls the shared
``helper_common.config.load_helper_config`` directly, same as every other
helper. ``load_helper_config`` itself is unit-tested in
mimirheim_helpers/common/tests/unit/test_load_helper_config.py; these tests
only cover the scheduler-specific parts:

- A valid YAML file loads and validates into a SchedulerConfig.
- The client_id default is applied.
- Environment overrides from the Home Assistant Supervisor are applied.
- A missing/unreadable Broker Settings section is still immediately fatal.
- Every other failure — including a bad cron expression or an empty
  schedules list, which used to also exit 1 — now enters Awaiting
  Configuration instead, mirroring every other helper.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from helper_common.config import load_helper_config
from scheduler.config import SchedulerConfig
from scheduler.formspec import SCHEDULER_CONFIG_FORM_SPEC

_VALID = """
mqtt:
  host: broker.example
  port: 1883
schedules:
  - "*/15 * * * *": mimir/input/trigger
"""

_OWNER_KWARGS = {
    "owner_id": "scheduler",
    "display_name": "Scheduler",
    "form_spec": SCHEDULER_CONFIG_FORM_SPEC,
}


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("scheduler")


@pytest.fixture
def mock_awaiting_configuration() -> MagicMock:
    """Patch out the blocking Awaiting Configuration loop.

    ``run_awaiting_configuration`` opens a real MQTT connection and blocks
    until a Restart Request or termination signal; it must never actually
    run inside a unit test. Patched where ``load_helper_config`` imports it
    from (locally, at call time, to avoid a circular import).
    """
    with patch("helper_common.awaiting_configuration.run_awaiting_configuration") as mock:
        yield mock


def _write(tmp_path: Path, text: str, name: str = "scheduler.yaml") -> str:
    """Write a config file and return its path as a string."""
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_valid_file_loads(tmp_path: Path, logger: logging.Logger) -> None:
    """A well-formed config file produces a validated SchedulerConfig."""
    config = load_helper_config(
        _write(tmp_path, _VALID), SchedulerConfig, logger, **_OWNER_KWARGS
    )

    assert config.mqtt.host == "broker.example"
    assert config.parsed_schedules() == [("*/15 * * * *", "mimir/input/trigger")]


def test_client_id_defaults_when_absent(tmp_path: Path, logger: logging.Logger) -> None:
    """An omitted client_id becomes the tool's own default, not None."""
    config = load_helper_config(
        _write(tmp_path, _VALID), SchedulerConfig, logger, **_OWNER_KWARGS
    )

    assert config.mqtt.client_id == "mimir-scheduler"


def test_mqtt_env_overrides_take_precedence(
    tmp_path: Path, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supervisor-injected broker settings override the file, as for every helper."""
    monkeypatch.setenv("MQTT_HOST", "supervisor.local")
    monkeypatch.setenv("MQTT_PORT", "8883")

    config = load_helper_config(
        _write(tmp_path, _VALID), SchedulerConfig, logger, **_OWNER_KWARGS
    )

    assert config.mqtt.host == "supervisor.local"
    assert config.mqtt.port == 8883


class TestBrokerSettingsFailureIsFatal:
    """A daemon with no usable ``mqtt:`` section cannot connect at all, so
    this remains an immediate ``sys.exit(1)``, unchanged from before this
    feature."""

    def test_missing_file_and_no_env_broker_settings_exits_1(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MQTT_HOST", raising=False)
        missing = str(tmp_path / "absent.yaml")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(missing, SchedulerConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 1


class TestFullValidationFailureEntersAwaitingConfiguration:
    """Every other failure enters Awaiting Configuration instead of exiting 1.

    ``run_awaiting_configuration`` is mocked out, so these tests assert on
    how it was called and that ``load_helper_config`` exits 0 once it
    returns, not on real MQTT behaviour.
    """

    def test_empty_schedules_list(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        """An empty schedules list used to exit 1; now it is the ordinary
        first-run case, same as any other missing required field."""
        path = _write(tmp_path, "mqtt:\n  host: broker.example\nschedules: []\n")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(path, SchedulerConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        kwargs = mock_awaiting_configuration.call_args.kwargs
        assert "at least 1 item" in kwargs["detail"]

    def test_invalid_cron_in_file(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        """A bad cron expression surfaces through load_helper_config, not at
        first fire, but no longer stops the process outright."""
        path = _write(
            tmp_path,
            'mqtt:\n  host: h\nschedules:\n  - "0 14 * * 9": some/topic\n',
        )

        with pytest.raises(SystemExit) as exc:
            load_helper_config(path, SchedulerConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        kwargs = mock_awaiting_configuration.call_args.kwargs
        assert "out of range" in kwargs["detail"]

    def test_missing_file_with_env_broker_settings(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        missing = str(tmp_path / "absent.yaml")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(missing, SchedulerConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        mock_awaiting_configuration.assert_called_once()
