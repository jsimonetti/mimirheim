"""Unit tests for helper_common.config.load_helper_config.

Every helper daemon calls this to turn its YAML file into a validated
config model. Since the config-owner-startup-resilience feature, validation
happens in two stages (mirroring mimirheim core's own startup):

- Broker Settings (the ``mqtt:`` section, validated against ``MqttConfig``
  alone) failing to validate remains immediately fatal: a daemon that
  cannot determine how to reach MQTT cannot serve the Config Service
  protocol either, so there is nothing useful left to do.
- Everything else failing to validate — including the file not existing,
  not being readable, or not being valid YAML — is no longer fatal. Instead
  ``load_helper_config`` hands off to
  ``helper_common.awaiting_configuration.run_awaiting_configuration``, which
  is mocked out in these tests (it blocks on real MQTT network I/O and is
  covered by its own test file), and the process exits 0 once that returns.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import BaseModel, ConfigDict, Field

from helper_common.config import (
    MqttConfig,
    apply_mqtt_env_overrides,
    load_helper_config,
)
from mimirheim_shared.formspec import FieldSpec, FormSpec


class _ExampleConfig(BaseModel):
    """Stand-in for a real helper config model."""

    model_config = ConfigDict(extra="forbid")

    mqtt: MqttConfig = Field(description="Broker settings.")
    output_topic: str = Field(description="Where the helper publishes.")


_EXAMPLE_FORM_SPEC = FormSpec(
    fields={"output_topic": FieldSpec(label="Output topic", description="Where the helper publishes.")}
)

_MINIMAL = """
mqtt:
  host: broker.local
output_topic: mimir/input/prices
"""

_OWNER_KWARGS = {
    "owner_id": "example-helper",
    "display_name": "Example Helper",
    "form_spec": _EXAMPLE_FORM_SPEC,
}


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_helper")


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


class TestSuccess:
    def test_returns_validated_model(self, tmp_path: Path, logger: logging.Logger) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL)

        cfg = load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

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

        cfg = load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        # The environment wins over the file, as it does for every helper.
        assert cfg.mqtt.host == "supervisor.broker"
        assert cfg.mqtt.port == 8883


class TestBrokerSettingsFailureIsFatal:
    """A daemon with no usable ``mqtt:`` section cannot connect at all, so
    this remains an immediate ``sys.exit(1)``, unchanged from before this
    feature."""

    def test_missing_file_and_no_env_broker_settings_exits_1(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("MQTT_HOST", raising=False)
        missing = tmp_path / "nope.yaml"

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(missing), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 1
        assert "Invalid Broker Settings" in caplog.text

    def test_invalid_mqtt_section_in_file_exits_1(
        self, tmp_path: Path, logger: logging.Logger, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\n  port: not-a-port\noutput_topic: mimir/input/prices\n")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 1
        assert "Invalid Broker Settings" in caplog.text

    def test_bad_mqtt_port_env_var_exits_1(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL)
        monkeypatch.setenv("MQTT_PORT", "not-a-number")

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 1
        assert "Failed to load configuration" in caplog.text


class TestFullValidationFailureEntersAwaitingConfiguration:
    """Every other failure enters Awaiting Configuration instead of exiting 1.

    ``run_awaiting_configuration`` is mocked out, so these tests assert on
    how it was called and that ``load_helper_config`` exits 0 once it
    returns, not on real MQTT behaviour.
    """

    def test_missing_file_with_env_broker_settings(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        missing = tmp_path / "nope.yaml"

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(missing), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        mock_awaiting_configuration.assert_called_once()
        kwargs = mock_awaiting_configuration.call_args.kwargs
        assert kwargs["mqtt_config"] == MqttConfig(host="supervisor.broker")
        assert kwargs["config_path"] == missing
        assert "Cannot read config file" in kwargs["detail"]
        assert kwargs["owner_id"] == "example-helper"
        assert kwargs["display_name"] == "Example Helper"
        assert kwargs["model_cls"] is _ExampleConfig
        assert kwargs["form_spec"] is _EXAMPLE_FORM_SPEC
        assert kwargs["logger"] is logger

    def test_malformed_yaml_with_env_broker_settings(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        path = tmp_path / "config.yaml"
        path.write_text("mqtt: [unclosed\n")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        kwargs = mock_awaiting_configuration.call_args.kwargs
        assert "Cannot parse config file" in kwargs["detail"]

    def test_schema_validation_failure(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        path = tmp_path / "config.yaml"
        # output_topic is required and absent.
        path.write_text("mqtt:\n  host: broker.local\n")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        kwargs = mock_awaiting_configuration.call_args.kwargs
        assert "output_topic" in kwargs["detail"]
        assert kwargs["mqtt_config"] == MqttConfig(host="broker.local")

    def test_unknown_key_is_rejected(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        path = tmp_path / "config.yaml"
        path.write_text(_MINIMAL + "typo_key: 1\n")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        mock_awaiting_configuration.assert_called_once()

    def test_empty_file_with_env_broker_settings(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        monkeypatch: pytest.MonkeyPatch,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        """An empty file is treated the same as one that fails schema
        validation: nothing has been configured yet, which is the ordinary
        first-run case, not an unrecoverable error."""
        monkeypatch.setenv("MQTT_HOST", "supervisor.broker")
        path = tmp_path / "config.yaml"
        path.write_text("")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 0
        mock_awaiting_configuration.assert_called_once()


class TestLoggerChoice:
    def test_logs_to_the_caller_supplied_logger(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The fatal Broker Settings record must carry the helper's own
        logger name, not this module's. Each helper passes its own logger so
        operators can filter by helper."""
        named = logging.getLogger("nordpool")
        monkeypatch.delenv("MQTT_HOST", raising=False)

        with caplog.at_level(logging.ERROR), pytest.raises(SystemExit):
            load_helper_config(str(tmp_path / "absent.yaml"), _ExampleConfig, named, **_OWNER_KWARGS)

        assert [r.name for r in caplog.records] == ["nordpool"]


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

        cfg = load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert cfg.mqtt.host == "supervisor.broker"
        assert cfg.mqtt.port == 8883

    def test_null_section_without_environment_is_fatal(
        self,
        tmp_path: Path,
        logger: logging.Logger,
        mock_awaiting_configuration: MagicMock,
    ) -> None:
        """With nothing to fill it in, Broker Settings validation also fails,
        so this is fatal, not Awaiting Configuration — matching the "missing
        file and no env" case above."""
        path = tmp_path / "config.yaml"
        path.write_text("mqtt:\noutput_topic: mimir/input/prices\n")

        with pytest.raises(SystemExit) as exc:
            load_helper_config(str(path), _ExampleConfig, logger, **_OWNER_KWARGS)

        assert exc.value.code == 1
        mock_awaiting_configuration.assert_not_called()


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
