"""Unit tests for config_editor.config.

Tests verify:
- Default field values are applied when only the required mqtt section is given.
- Custom values for port, log_level, and allowed_ip are accepted.
- port values outside the valid range (1024-65535) are rejected.
- Unknown top-level fields are rejected (extra="forbid").
- load_config() delegates to helper_common.config.load_helper_config
  (config-owner-startup-resilience ticket 03): a missing file or a
  full-validation failure enters Awaiting Configuration instead of exiting,
  same as every other helper; only a missing/invalid mqtt: section remains
  immediately fatal.
- load_config() applies the CONFIG_EDITOR_ALLOWED_IP environment variable
  override after Pydantic validation.
- load_config() clears allowed_ip when CONFIG_EDITOR_ALLOWED_IP is unset,
  even if a previous call had set a value (env var takes precedence over
  any residual state).
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pydantic import ValidationError

from config_editor.config import ConfigEditorConfig, load_config

_logger = logging.getLogger("test-config-editor")


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


# ---------------------------------------------------------------------------
# ConfigEditorConfig: defaults
# ---------------------------------------------------------------------------

def test_defaults_are_applied() -> None:
    """A dict with only the required mqtt section produces documented defaults."""
    cfg = ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}})
    assert cfg.port == 8099
    assert cfg.log_level == "INFO"
    assert cfg.allowed_ip is None


def test_missing_mqtt_section_is_rejected() -> None:
    """mqtt has no default: the Config Service protocol requires a broker."""
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({})


# ---------------------------------------------------------------------------
# ConfigEditorConfig: valid custom values
# ---------------------------------------------------------------------------

def test_custom_port_accepted() -> None:
    cfg = ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "port": 9000})
    assert cfg.port == 9000


def test_custom_log_level_accepted() -> None:
    for level in ("DEBUG", "WARNING"):
        cfg = ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "log_level": level})
        assert cfg.log_level == level


# ---------------------------------------------------------------------------
# ConfigEditorConfig: invalid values
# ---------------------------------------------------------------------------

def test_port_below_minimum_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "port": 80})


def test_port_above_maximum_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "port": 70000})


def test_port_at_minimum_boundary_accepted() -> None:
    cfg = ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "port": 1024})
    assert cfg.port == 1024


def test_port_at_maximum_boundary_accepted() -> None:
    cfg = ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "port": 65535})
    assert cfg.port == 65535


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"mqtt": {"host": "localhost"}, "unexpected": "value"})


# ---------------------------------------------------------------------------
# load_config(): empty and comment-only YAML files
# ---------------------------------------------------------------------------

def test_load_config_empty_file_uses_mqtt_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty config file still validates when MQTT_HOST is supplied by the environment."""
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("")
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.port == 8099
    assert cfg.mqtt.host == "broker.local"


def test_load_config_comment_only_file_uses_mqtt_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A file containing only YAML comments (safe_load returns None) still validates via env."""
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("# this file intentionally left blank\n")
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.port == 8099


def test_load_config_custom_port(tmp_path: Path) -> None:
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}, "port": 9000}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.port == 9000


def test_load_config_missing_file_with_mqtt_env_enters_awaiting_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mock_awaiting_configuration: MagicMock
) -> None:
    """A missing file is no longer silently filled in with defaults: the Config
    Editor connects to MQTT (Broker Settings validate fine from the
    environment) and shows up as Awaiting Configuration, exactly like every
    other helper with a missing file (config-owner-startup-resilience ticket
    03) -- including that it, too, is the tool a first-time user relies on to
    create every other config file, so it must still be reachable before its
    own file exists."""
    monkeypatch.setenv("MQTT_HOST", "broker.local")

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(tmp_path / "nonexistent.yaml"), _logger)

    assert exc_info.value.code == 0
    kwargs = mock_awaiting_configuration.call_args.kwargs
    assert "Cannot read config file" in kwargs["detail"]


def test_load_config_missing_file_and_no_mqtt_source_exits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a file and without env-supplied MQTT settings, there is nothing to
    validate against: MQTT is a hard requirement, so this exits rather than
    silently starting with no broker configured."""
    monkeypatch.delenv("MQTT_HOST", raising=False)
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(tmp_path / "nonexistent.yaml"), _logger)
    assert exc_info.value.code == 1


def test_load_config_invalid_config_enters_awaiting_configuration(
    tmp_path: Path, mock_awaiting_configuration: MagicMock
) -> None:
    """A full-validation failure (here, port below the valid range) enters
    Awaiting Configuration instead of exiting 1, same as every other helper.
    Only a missing/invalid mqtt: section remains immediately fatal."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}, "port": 80}))  # port below minimum

    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file), _logger)

    assert exc_info.value.code == 0
    kwargs = mock_awaiting_configuration.call_args.kwargs
    assert "port" in kwargs["detail"]


# ---------------------------------------------------------------------------
# load_config(): CONFIG_EDITOR_ALLOWED_IP env var override
# ---------------------------------------------------------------------------

def test_load_config_sets_allowed_ip_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG_EDITOR_ALLOWED_IP env var sets allowed_ip after validation."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "172.30.33.1")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip == "172.30.33.1"


def test_load_config_allowed_ip_none_when_env_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """allowed_ip is None when CONFIG_EDITOR_ALLOWED_IP is not set."""
    monkeypatch.delenv("CONFIG_EDITOR_ALLOWED_IP", raising=False)
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip is None


def test_load_config_empty_env_var_treated_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty CONFIG_EDITOR_ALLOWED_IP string is treated the same as absent."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip is None


def test_load_config_keeps_allowed_ip_from_yaml_when_env_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured allowed_ip must survive when the env var is not set.

    Outside the HA add-on -- plain Docker, bare metal -- there is no
    CONFIG_EDITOR_ALLOWED_IP. The value in the file is the only restriction the
    operator has, and discarding it means the server accepts every source IP.
    """
    monkeypatch.delenv("CONFIG_EDITOR_ALLOWED_IP", raising=False)
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}, "allowed_ip": "192.168.1.5"}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip == "192.168.1.5"


def test_load_config_env_overrides_allowed_ip_from_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the Supervisor supplies the ingress gateway, it wins over the file."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "172.30.32.1")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}, "allowed_ip": "192.168.1.5"}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip == "172.30.32.1"


def test_load_config_keeps_allowed_ip_from_yaml_when_env_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty env var means "not an add-on", so the file still wins."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"mqtt": {"host": "localhost"}, "allowed_ip": "192.168.1.5"}))
    cfg = load_config(str(cfg_file), _logger)
    assert cfg.allowed_ip == "192.168.1.5"
