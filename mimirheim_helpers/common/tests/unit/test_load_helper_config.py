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

from helper_common.config import (
    MqttConfig,
    apply_mqtt_env_overrides,
    load_helper_config,
)


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


class TestEmptyConfigFile:
    """An empty or comment-only file used to surface as an AttributeError.

    ``yaml.safe_load("")`` returns ``None``. That ``None`` was passed straight
    to ``apply_mqtt_env_overrides``, which called ``.setdefault`` on it. With
    MQTT env vars set the result was ``AttributeError: 'NoneType' object has no
    attribute 'setdefault'``; without them it reached Pydantic, which reported
    only that the input was not a dictionary.

    Assertions here target the exception message rather than ``caplog.text``:
    pytest's ``tmp_path`` embeds the test name in the directory it creates, so
    a substring check against the whole log record can match the path instead
    of the message and pass for the wrong reason.
    """

    @pytest.mark.parametrize(
        "content", ["", "\n\n", "# nothing but a comment\n"], ids=["empty", "blank", "comment"]
    )
    def test_names_the_file_as_empty(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        content: str,
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(content)

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger)

        assert exc.value.code == 1
        assert "AttributeError" not in caplog.text
        assert "no configuration" in caplog.text

    @pytest.mark.parametrize(
        "content", ["", "# nothing but a comment\n"], ids=["empty", "comment"]
    )
    def test_names_the_file_as_empty_with_env_vars_set(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
        content: str,
    ) -> None:
        """The env-vars-present case is the one that raised AttributeError."""
        path = tmp_path / "config.yaml"
        path.write_text(content)
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
            load_helper_config(str(path), _ExampleConfig, logger)

        assert "AttributeError" not in caplog.text
        assert "no configuration" in caplog.text


class TestNullMqttSection:
    """A bare ``mqtt:`` key parses to ``None``, which used to crash.

    Rather than turning it into a different error, the override step now treats
    a null section as an absent one. That makes the Supervisor case work: a
    config that writes ``mqtt:`` and lets the environment supply every value is
    now valid, where before it raised.
    """

    def test_null_section_is_filled_from_the_environment(
        self, tmp_path: Path, logger: logging.Logger, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\noutput_topic: mimir/input/prices\n")
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        monkeypatch.setenv("MQTT_PORT", "8883")

        cfg = load_helper_config(str(path), _ExampleConfig, logger)

        assert cfg.mqtt.host == "supervisor.broker"
        assert cfg.mqtt.port == 8883

    def test_null_section_without_environment_reports_the_field(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With nothing to fill it, Pydantic's own message is already clear."""
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\noutput_topic: mimir/input/prices\n")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger)

        assert exc.value.code == 1
        assert "AttributeError" not in caplog.text
        assert "ValidationError" in caplog.text


class TestApplyMqttEnvOverrides:
    """Direct tests of the override step, with no log or path indirection."""

    def test_null_mqtt_section_becomes_a_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_HOST", "broker.local")

        assert apply_mqtt_env_overrides({"mqtt": None}) == {
            "mqtt": {"host": "broker.local"}
        }

    def test_null_mqtt_section_with_no_env_is_left_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for var in ("MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_SSL"):
            monkeypatch.delenv(var, raising=False)

        assert apply_mqtt_env_overrides({"mqtt": None}) == {"mqtt": None}

    def test_non_dict_input_names_the_problem(self) -> None:
        with pytest.raises(ValueError, match="no configuration"):
            apply_mqtt_env_overrides(None)  # type: ignore[arg-type]

    def test_non_numeric_port_names_the_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_PORT", "not-a-number")

        with pytest.raises(ValueError, match="MQTT_PORT"):
            apply_mqtt_env_overrides({})

    def test_non_numeric_port_message_quotes_the_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MQTT_PORT", "8O83")

        with pytest.raises(ValueError, match="8O83"):
            apply_mqtt_env_overrides({})

    def test_out_of_range_port_is_reported_here_not_by_pydantic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A numeric but impossible port is still a bad environment variable."""
        monkeypatch.setenv("MQTT_PORT", "99999")

        with pytest.raises(ValueError, match="MQTT_PORT"):
            apply_mqtt_env_overrides({})
