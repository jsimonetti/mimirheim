"""Unit tests for config_editor.config.

Tests verify:
- Default field values are applied when config-editor.yaml is empty.
- Custom values for port, config_dir, and log_level are accepted.
- port values outside the valid range (1024–65535) are rejected.
- Unknown top-level fields are rejected (extra="forbid").
- load_config() returns defaults when the config file does not exist.
- load_config() applies the CONFIG_EDITOR_ALLOWED_IP environment variable
  override after Pydantic validation.
- load_config() clears allowed_ip when CONFIG_EDITOR_ALLOWED_IP is unset,
  even if a previous call had set a value (env var takes precedence over
  any residual state).
- load_config() exits with code 0 when disabled is true or null (bare key).
- load_config() continues normally when disabled is false or absent.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from config_editor.config import ConfigEditorConfig, load_config


# ---------------------------------------------------------------------------
# ConfigEditorConfig: defaults
# ---------------------------------------------------------------------------

def test_defaults_are_applied() -> None:
    """An empty dict produces a config with all documented defaults."""
    cfg = ConfigEditorConfig.model_validate({})
    assert cfg.port == 8099
    assert cfg.config_dir == Path("/config")
    assert cfg.log_level == "INFO"
    assert cfg.allowed_ip is None


# ---------------------------------------------------------------------------
# ConfigEditorConfig: valid custom values
# ---------------------------------------------------------------------------

def test_custom_port_accepted() -> None:
    """A port within the valid range is accepted."""
    cfg = ConfigEditorConfig.model_validate({"port": 9000})
    assert cfg.port == 9000


def test_custom_config_dir_accepted() -> None:
    """A custom config_dir string is coerced to Path."""
    cfg = ConfigEditorConfig.model_validate({"config_dir": "/data/mimirheim"})
    assert cfg.config_dir == Path("/data/mimirheim")


def test_custom_log_level_accepted() -> None:
    """DEBUG and WARNING are accepted as log_level values."""
    for level in ("DEBUG", "WARNING"):
        cfg = ConfigEditorConfig.model_validate({"log_level": level})
        assert cfg.log_level == level


# ---------------------------------------------------------------------------
# ConfigEditorConfig: invalid values
# ---------------------------------------------------------------------------

def test_port_below_minimum_rejected() -> None:
    """A port below 1024 raises ValidationError."""
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"port": 80})


def test_port_above_maximum_rejected() -> None:
    """A port above 65535 raises ValidationError."""
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"port": 70000})


def test_port_at_minimum_boundary_accepted() -> None:
    """Port 1024 is the minimum valid value and must be accepted."""
    cfg = ConfigEditorConfig.model_validate({"port": 1024})
    assert cfg.port == 1024


def test_port_at_maximum_boundary_accepted() -> None:
    """Port 65535 is the maximum valid value and must be accepted."""
    cfg = ConfigEditorConfig.model_validate({"port": 65535})
    assert cfg.port == 65535


def test_unknown_top_level_field_rejected() -> None:
    """An unrecognised top-level field raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        ConfigEditorConfig.model_validate({"unexpected": "value"})


# ---------------------------------------------------------------------------
# load_config(): empty and comment-only YAML files
# ---------------------------------------------------------------------------

def test_load_config_empty_file_returns_defaults(tmp_path: Path) -> None:
    """An empty config file returns a ConfigEditorConfig with all defaults."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("")
    cfg = load_config(str(cfg_file))
    assert cfg.port == 8099
    assert cfg.config_dir == Path("/config")
    assert cfg.log_level == "INFO"


def test_load_config_comment_only_file_returns_defaults(tmp_path: Path) -> None:
    """A file containing only YAML comments (safe_load returns None) returns defaults."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("# this file intentionally left blank\n")
    cfg = load_config(str(cfg_file))
    assert cfg.port == 8099


def test_load_config_custom_port(tmp_path: Path) -> None:
    """A file with port: 9000 is loaded with that port."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"port": 9000}))
    cfg = load_config(str(cfg_file))
    assert cfg.port == 9000


def test_load_config_missing_file_returns_defaults(tmp_path: Path) -> None:
    """load_config() returns defaults when the config file does not exist."""
    cfg = load_config(str(tmp_path / "nonexistent.yaml"))
    assert cfg.port == 8099
    assert cfg.config_dir == Path("/config")
    assert cfg.log_level == "INFO"


def test_load_config_invalid_config_exits(tmp_path: Path) -> None:
    """load_config() calls sys.exit(1) when Pydantic validation fails."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"port": 80}))  # port below minimum
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# load_config(): CONFIG_EDITOR_ALLOWED_IP env var override
# ---------------------------------------------------------------------------

def test_load_config_sets_allowed_ip_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CONFIG_EDITOR_ALLOWED_IP env var sets allowed_ip after validation."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "172.30.33.1")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("")
    cfg = load_config(str(cfg_file))
    assert cfg.allowed_ip == "172.30.33.1"


def test_load_config_allowed_ip_none_when_env_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """allowed_ip is None when CONFIG_EDITOR_ALLOWED_IP is not set."""
    monkeypatch.delenv("CONFIG_EDITOR_ALLOWED_IP", raising=False)
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("")
    cfg = load_config(str(cfg_file))
    assert cfg.allowed_ip is None


def test_load_config_empty_env_var_treated_as_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty CONFIG_EDITOR_ALLOWED_IP string is treated the same as absent."""
    monkeypatch.setenv("CONFIG_EDITOR_ALLOWED_IP", "")
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("")
    cfg = load_config(str(cfg_file))
    assert cfg.allowed_ip is None


# ---------------------------------------------------------------------------
# ConfigEditorConfig: disabled field
# ---------------------------------------------------------------------------

def test_disabled_false_is_default() -> None:
    """disabled defaults to False when the key is absent."""
    cfg = ConfigEditorConfig.model_validate({})
    assert cfg.disabled is False


def test_disabled_true_accepted() -> None:
    """disabled: true is accepted by the model."""
    cfg = ConfigEditorConfig.model_validate({"disabled": True})
    assert cfg.disabled is True


def test_disabled_false_accepted() -> None:
    """disabled: false is accepted and does not cause an exit."""
    cfg = ConfigEditorConfig.model_validate({"disabled": False})
    assert cfg.disabled is False


def test_disabled_null_accepted() -> None:
    """A bare disabled key (YAML null) is accepted as None."""
    cfg = ConfigEditorConfig.model_validate({"disabled": None})
    assert cfg.disabled is None


# ---------------------------------------------------------------------------
# load_config(): disabled exits cleanly
# ---------------------------------------------------------------------------

def test_load_config_disabled_true_exits_with_code_zero(tmp_path: Path) -> None:
    """load_config() exits with code 0 when disabled: true."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"disabled": True}))
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.code == 0


def test_load_config_disabled_null_exits_with_code_zero(tmp_path: Path) -> None:
    """load_config() exits with code 0 when disabled is a bare key (null value).

    In YAML, 'disabled:' (key with no value) deserialises to {"disabled": None}.
    This is the form a user would write if they want to disable the editor without
    assigning an explicit true/false value.
    """
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text("disabled:\n")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(cfg_file))
    assert exc_info.value.code == 0


def test_load_config_disabled_false_does_not_exit(tmp_path: Path) -> None:
    """load_config() does not exit when disabled: false."""
    cfg_file = tmp_path / "config-editor.yaml"
    cfg_file.write_text(yaml.dump({"disabled": False}))
    cfg = load_config(str(cfg_file))
    assert cfg.disabled is False
