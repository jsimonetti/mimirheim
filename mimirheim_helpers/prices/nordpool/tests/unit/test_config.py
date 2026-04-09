"""Unit tests for nordpool.config.

Covers schema validation for all Pydantic models used by the nordpool tool.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from nordpool.config import MqttConfig, NordpoolApiConfig, NordpoolConfig


_VALID_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "client_id": "nordpool-test",
    },
    "trigger_topic": "mimir/input/tools/prices/trigger",
    "output_topic": "mimir/input/prices",
    "nordpool": {
        "area": "NO2",
    },
    "signal_mimir": False,
}


class TestMqttConfig:
    def test_valid_minimal(self) -> None:
        cfg = MqttConfig(host="broker", port=1883, client_id="id")
        assert cfg.host == "broker"
        assert cfg.port == 1883

    def test_port_defaults_to_1883(self) -> None:
        cfg = MqttConfig(host="broker", client_id="id")
        assert cfg.port == 1883

    def test_optional_auth_defaults_none(self) -> None:
        cfg = MqttConfig(host="broker", client_id="id")
        assert cfg.username is None
        assert cfg.password is None

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            MqttConfig(host="broker", client_id="id", unknown_field="x")


class TestNordpoolApiConfig:
    def test_valid_minimal(self) -> None:
        cfg = NordpoolApiConfig(area="NO2")
        assert cfg.area == "NO2"
        assert cfg.import_formula == "price"
        assert cfg.export_formula == "price"

    def test_custom_import_formula_accepted(self) -> None:
        cfg = NordpoolApiConfig(area="NL", import_formula="((price + 0.09161) * 1.21) + 0.0248")
        assert "0.09161" in cfg.import_formula

    def test_custom_export_formula_accepted(self) -> None:
        cfg = NordpoolApiConfig(area="NL", export_formula="price * 0.9")
        assert cfg.export_formula == "price * 0.9"

    def test_ts_variable_usable_in_formula(self) -> None:
        # Formulas may reference ts for time-varying tariffs.
        cfg = NordpoolApiConfig(area="NL", import_formula="price + (0.05 if ts.hour < 7 else 0.1)")
        assert "ts.hour" in cfg.import_formula

    def test_invalid_import_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            NordpoolApiConfig(area="NO2", import_formula="price +* 0.1")

    def test_invalid_export_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            NordpoolApiConfig(area="NO2", export_formula="def bad():")

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            NordpoolApiConfig(area="NO2", currency="EUR")


class TestNordpoolConfig:
    def test_valid_full_config(self) -> None:
        cfg = NordpoolConfig.model_validate(_VALID_CONFIG)
        assert cfg.nordpool.area == "NO2"
        assert cfg.signal_mimir is False

    def test_signal_mimir_without_explicit_trigger_uses_derived_topic(self) -> None:
        """When signal_mimir is True and no explicit trigger topic is given, the derived topic is used."""
        cfg = NordpoolConfig.model_validate({**_VALID_CONFIG, "signal_mimir": True})
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_signal_mimir_true_with_trigger_topic_accepted(self) -> None:
        good = {
            **_VALID_CONFIG,
            "signal_mimir": True,
            "mimir_trigger_topic": "mimir/input/trigger",
        }
        cfg = NordpoolConfig.model_validate(good)
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_rejects_unknown_top_level_fields(self) -> None:
        bad = {**_VALID_CONFIG, "extra": "not_allowed"}
        with pytest.raises(ValidationError):
            NordpoolConfig.model_validate(bad)

    def test_missing_mqtt_host_rejected(self) -> None:
        bad = {**_VALID_CONFIG, "mqtt": {"client_id": "id"}}
        with pytest.raises(ValidationError):
            NordpoolConfig.model_validate(bad)
