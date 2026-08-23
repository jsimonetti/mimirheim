"""Unit tests for helper_common.config.load_helper_config.

Five helper entry points carried a byte-identical ``_load_config`` before this
function existed. These tests pin the behaviour all five shared: parse the
YAML, apply the MQTT environment overrides, validate against the given model,
and on any failure log the full traceback and exit 1.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from helper_common.config import MqttConfig, load_helper_config


class _ExampleConfig(BaseModel):
    """Stand-in for a real helper config model."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="Broker settings.")
    output_topic: str = Field(description="Where the helper publishes.")


_MINIMAL = """
mqtt:
  host: broker.local
output_topic: mimir/input/prices
"""


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_helper")


class TestSuccess:
    def test_returns_validated_model(self, tmp_path: Path, logger: logging.Logger) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL)

        cfg = load_helper_config(str(path), _ExampleConfig, logger)

        assert isinstance(cfg, _ExampleConfig)
        assert cfg.mqtt.host == "broker.local"
        assert cfg.output_topic == "mimir/input/prices"

    def test_applies_mqtt_env_overrides(
        self, tmp_path: Path, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL)
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        monkeypatch.setenv("MQTT_PORT", "8883")

        cfg = load_helper_config(str(path), _ExampleConfig, logger)

        # The environment wins over the file, as it does for every helper.
        assert cfg.mqtt.host == "supervisor.broker"
        assert cfg.mqtt.port == 8883


class TestFailure:
    """Every failure path must exit 1 and leave a traceback in the log."""

    def test_missing_file_exits_1(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        missing = tmp_path / "nope.yaml"

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(missing), _ExampleConfig, logger)

        assert exc.value.code == 1
        assert "Failed to load configuration" in caplog.text
        assert str(missing) in caplog.text
        assert "FileNotFoundError" in caplog.text

    def test_malformed_yaml_exits_1(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mqtt: [unclosed\n")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger)

        assert exc.value.code == 1
        assert "Failed to load configuration" in caplog.text

    def test_validation_failure_exits_1(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.yaml"
        # output_topic is required and absent.
        path.write_text("mqtt:\n  host: broker.local\n")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger)

        assert exc.value.code == 1
        assert "output_topic" in caplog.text

    def test_unknown_key_is_rejected(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL + "typo_key: 1\n")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger)

        assert exc.value.code == 1


class TestLoggerChoice:
    def test_logs_to_the_caller_supplied_logger(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The record must carry the helper's own logger name, not this module's.

        Each helper passes its own logger so operators can filter by helper.
        """
        named = logging.getLogger("nordpool")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
            load_helper_config(str(tmp_path / "absent.yaml"), _ExampleConfig, named)

        assert [r.name for r in caplog.records] == ["nordpool"]
