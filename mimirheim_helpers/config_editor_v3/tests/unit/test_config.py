"""Unit tests for config_editor_v3.config.ConfigEditorV3Config."""

from __future__ import annotations

import pytest
from helper_common.config import MqttConfig
from pydantic import ValidationError

from config_editor_v3.config import ConfigEditorV3Config


def test_valid_config_applies_defaults() -> None:
    cfg = ConfigEditorV3Config(mqtt=MqttConfig(host="localhost"))

    assert cfg.port == 8100
    assert cfg.log_level == "INFO"


def test_missing_mqtt_section_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorV3Config()


def test_port_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorV3Config(mqtt=MqttConfig(host="localhost"), port=80)


def test_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ConfigEditorV3Config(mqtt=MqttConfig(host="localhost"), unexpected_field=True)
