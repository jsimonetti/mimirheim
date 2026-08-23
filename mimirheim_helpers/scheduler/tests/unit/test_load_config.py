"""Unit tests for scheduler.config.load_config.

load_config is the only path a running daemon takes to its configuration, and
both of its failure branches exit the process. Tests verify:

- A valid YAML file loads and validates.
- A missing or unreadable file exits with code 1 and names the path.
- A file that fails schema validation exits with code 1 and reports why.
- Environment overrides from the Home Assistant Supervisor are applied.
"""

import os
from pathlib import Path

import pytest

from scheduler.config import load_config

_VALID = """
mqtt:
  host: broker.example
  port: 1883
schedules:
  - "*/15 * * * *": mimir/input/trigger
"""


def _write(tmp_path: Path, text: str, name: str = "scheduler.yaml") -> str:
    """Write a config file and return its path as a string."""
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def test_valid_file_loads(tmp_path: Path) -> None:
    """A well-formed config file produces a validated SchedulerConfig."""
    config = load_config(_write(tmp_path, _VALID))

    assert config.mqtt.host == "broker.example"
    assert config.parsed_schedules() == [("*/15 * * * *", "mimir/input/trigger")]


def test_client_id_defaults_when_absent(tmp_path: Path) -> None:
    """An omitted client_id becomes the tool's own default, not None."""
    config = load_config(_write(tmp_path, _VALID))

    assert config.mqtt.client_id == "mimir-scheduler"


def test_missing_file_exits_with_code_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A path that does not exist is reported and stops the process."""
    missing = str(tmp_path / "absent.yaml")

    with pytest.raises(SystemExit) as exc:
        load_config(missing)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    # Asserting the specific message, not just the path: the path also appears
    # in the schema-validation error, so a missing file would look handled even
    # if the OSError branch were gone.
    assert "Cannot read config file" in err
    assert missing in err


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores file permissions, so the file stays readable",
)
def test_unreadable_file_exits_with_code_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that cannot be opened is reported rather than raising OSError."""
    path = tmp_path / "unreadable.yaml"
    path.write_text(_VALID)
    path.chmod(0o000)

    try:
        with pytest.raises(SystemExit) as exc:
            load_config(str(path))
    finally:
        path.chmod(0o600)

    assert exc.value.code == 1
    assert "Cannot read config file" in capsys.readouterr().err


def test_invalid_config_exits_with_code_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A file that parses as YAML but fails the schema is reported."""
    path = _write(tmp_path, "mqtt:\n  host: broker.example\nschedules: []\n")

    with pytest.raises(SystemExit) as exc:
        load_config(path)

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "Invalid configuration" in err
    assert "must not be empty" in err


def test_invalid_cron_in_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A bad cron expression surfaces through load_config, not at first fire."""
    path = _write(
        tmp_path,
        'mqtt:\n  host: h\nschedules:\n  - "0 14 * * 9": some/topic\n',
    )

    with pytest.raises(SystemExit) as exc:
        load_config(path)

    assert exc.value.code == 1
    assert "out of range" in capsys.readouterr().err


def test_mqtt_env_overrides_take_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Supervisor-injected broker settings override the file, as for every helper."""
    monkeypatch.setenv("MQTT_HOST", "supervisor.local")
    monkeypatch.setenv("MQTT_PORT", "8883")

    config = load_config(_write(tmp_path, _VALID))

    assert config.mqtt.host == "supervisor.local"
    assert config.mqtt.port == 8883
