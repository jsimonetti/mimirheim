"""Unit tests for mimirheim core's startup resilience (ADR-0009/0010/0011/0012).

Covers the pure helper functions ``__main__.py`` uses to split Broker
Settings validation (always fatal) from full configuration validation (never
fatal, falls back to Awaiting Configuration): ``_read_raw_config``,
``_apply_mqtt_env_overrides``, ``_load_broker_settings``,
``_try_load_full_config``. ``_run_awaiting_configuration`` is covered at the
wiring level with the MQTT client mocked out; the Config Service protocol
handling it delegates to is covered in
tests/unit/test_awaiting_configuration.py.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from mimirheim.__main__ import (
    _apply_mqtt_env_overrides,
    _load_broker_settings,
    _read_raw_config,
    _run_awaiting_configuration,
    _try_load_full_config,
)
from mimirheim.config.schema import MimirheimConfig, MqttConfig

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "sample_mimirheim_config.yaml"


class TestReadRawConfig:
    def test_missing_file_returns_empty_dict_and_a_detail(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.yaml"

        raw, detail = _read_raw_config(str(missing))

        assert raw == {}
        assert detail is not None
        assert str(missing) in detail

    def test_valid_file_returns_parsed_dict_and_no_detail(self) -> None:
        raw, detail = _read_raw_config(str(FIXTURE_PATH))

        assert detail is None
        assert raw["mqtt"]["host"] == "localhost"

    def test_malformed_yaml_returns_empty_dict_and_a_detail(self, tmp_path: Path) -> None:
        path = tmp_path / "mimirheim.yaml"
        path.write_text("mqtt: [unclosed\n")

        raw, detail = _read_raw_config(str(path))

        assert raw == {}
        assert detail is not None

    def test_empty_file_returns_empty_dict_and_no_detail(self, tmp_path: Path) -> None:
        path = tmp_path / "mimirheim.yaml"
        path.write_text("")

        raw, detail = _read_raw_config(str(path))

        assert raw == {}
        assert detail is None


class TestApplyMqttEnvOverrides:
    def test_mqtt_prefix_overrides_topic_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MQTT_PREFIX", "custom")
        raw: dict = {}

        _apply_mqtt_env_overrides(raw)

        assert raw["mqtt"]["topic_prefix"] == "custom"

    def test_absent_mqtt_prefix_leaves_topic_prefix_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MQTT_PREFIX", raising=False)
        raw = {"mqtt": {"host": "localhost", "topic_prefix": "mine"}}

        _apply_mqtt_env_overrides(raw)

        assert raw["mqtt"]["topic_prefix"] == "mine"


class TestLoadBrokerSettings:
    def test_valid_mqtt_section_returns_mqtt_config(self) -> None:
        raw = {"mqtt": {"host": "localhost", "port": 1883}}

        broker_settings = _load_broker_settings(raw, "mimirheim.yaml")

        assert isinstance(broker_settings, MqttConfig)
        assert broker_settings.host == "localhost"

    def test_missing_mqtt_section_and_no_env_exits_1(self) -> None:
        with pytest.raises(SystemExit) as exc:
            _load_broker_settings({}, "mimirheim.yaml")

        assert exc.value.code == 1

    def test_invalid_mqtt_section_exits_1(self) -> None:
        # port must be an int; "not-a-port" fails MqttConfig validation.
        raw = {"mqtt": {"host": "localhost", "port": "not-a-port"}}

        with pytest.raises(SystemExit) as exc:
            _load_broker_settings(raw, "mimirheim.yaml")

        assert exc.value.code == 1


class TestTryLoadFullConfig:
    def test_valid_config_returns_config_and_no_detail(self) -> None:
        raw = {
            "mqtt": {"host": "localhost"},
            "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        }

        config, detail = _try_load_full_config(raw, None)

        assert isinstance(config, MimirheimConfig)
        assert detail is None

    def test_load_error_short_circuits_validation(self) -> None:
        config, detail = _try_load_full_config({}, "Cannot read config file")

        assert config is None
        assert detail == "Cannot read config file"

    def test_invalid_config_returns_none_and_a_detail(self) -> None:
        # grid: is required and absent.
        raw = {"mqtt": {"host": "localhost"}}

        config, detail = _try_load_full_config(raw, None)

        assert config is None
        assert detail is not None
        assert "grid" in detail


class TestRunAwaitingConfiguration:
    """Covers the wiring _run_awaiting_configuration performs, with the MQTT
    client mocked out: connecting via Broker Settings, running until a
    Restart Request or termination signal, then stopping.
    """

    def test_client_is_started_and_stopped_around_the_shutdown_wait(
        self, tmp_path: Path
    ) -> None:
        broker_settings = MqttConfig(host="localhost", client_id="mimir")
        config_path = tmp_path / "mimirheim.yaml"

        with patch(
            "mimirheim.__main__.AwaitingConfigurationClient"
        ) as mock_client_cls:
            mock_client = mock_client_cls.return_value

            # Simulate a Restart Request (or signal) firing the moment the
            # main thread reaches shutdown_event.wait(), by having the
            # on_restart_requested callback captured at construction time
            # invoked immediately when client.start() runs.
            def _start_and_signal_restart() -> None:
                _, kwargs = mock_client_cls.call_args
                kwargs["on_restart_requested"]()

            mock_client.start.side_effect = _start_and_signal_restart

            _run_awaiting_configuration(broker_settings, config_path, "grid: Field required")

        mock_client_cls.assert_called_once_with(
            paho_client=mock_client_cls.call_args.kwargs["paho_client"],
            mqtt=broker_settings,
            config_path=config_path,
            detail="grid: Field required",
            on_restart_requested=mock_client_cls.call_args.kwargs["on_restart_requested"],
        )
        mock_client.start.assert_called_once_with()
        mock_client.stop.assert_called_once_with()

    def test_logs_a_warning_naming_the_validation_detail(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        broker_settings = MqttConfig(host="localhost", client_id="mimir")
        config_path = tmp_path / "mimirheim.yaml"

        with patch("mimirheim.__main__.AwaitingConfigurationClient") as mock_client_cls:
            mock_client = mock_client_cls.return_value

            def _start_and_signal_restart() -> None:
                _, kwargs = mock_client_cls.call_args
                kwargs["on_restart_requested"]()

            mock_client.start.side_effect = _start_and_signal_restart

            with caplog.at_level(logging.WARNING, logger="mimirheim"):
                _run_awaiting_configuration(broker_settings, config_path, "grid: Field required")

        assert "grid: Field required" in caplog.text
