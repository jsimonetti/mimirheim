"""Unit tests for zonneplan_prices.config.

Covers schema validation for all Pydantic models used by the Zonneplan tool.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from zonneplan_prices.config import ZonneplanApiConfig, ZonneplanPricesConfig


_VALID_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "client_id": "zonneplan-test",
    },
    "trigger_topic": "mimir/input/tools/prices/trigger",
    "zonneplan": {
        "email": "user@example.com",
    },
}


class TestZonneplanApiConfig:
    def test_valid_minimal(self) -> None:
        cfg = ZonneplanApiConfig()
        assert cfg.import_formula == "price"
        assert cfg.export_formula == "price_excl_tax"
        assert cfg.token_file == "zonneplan_token.json"
        assert cfg.email is None

    def test_email_accepted(self) -> None:
        cfg = ZonneplanApiConfig(email="user@example.com")
        assert cfg.email == "user@example.com"

    def test_token_file_override(self) -> None:
        cfg = ZonneplanApiConfig(token_file="/data/my_token.json")
        assert cfg.token_file == "/data/my_token.json"

    def test_custom_import_formula_accepted(self) -> None:
        cfg = ZonneplanApiConfig(import_formula="price * 1.1 + 0.05")
        assert "1.1" in cfg.import_formula

    def test_excl_tax_variable_in_formula(self) -> None:
        cfg = ZonneplanApiConfig(import_formula="price_excl_tax * 1.21 + 0.05")
        assert cfg.import_formula == "price_excl_tax * 1.21 + 0.05"

    def test_ts_variable_in_formula(self) -> None:
        cfg = ZonneplanApiConfig(import_formula="price + (0.05 if ts.hour < 7 else 0.1)")
        assert "ts.hour" in cfg.import_formula

    def test_invalid_import_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            ZonneplanApiConfig(import_formula="price +* 0.1")

    def test_invalid_export_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            ZonneplanApiConfig(export_formula="def bad():")

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ZonneplanApiConfig(unknown_field="x")


class TestZonneplanPricesConfig:
    def test_valid_minimal(self) -> None:
        cfg = ZonneplanPricesConfig(**_VALID_CONFIG)
        assert cfg.mqtt.host == "localhost"
        assert cfg.trigger_topic == "mimir/input/tools/prices/trigger"
        assert cfg.mimir_topic_prefix == "mimir"
        # output_topic and mimir_trigger_topic are derived from mimir_topic_prefix.
        assert cfg.output_topic == "mimir/input/prices"
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"
        assert cfg.mqtt.client_id == "zonneplan-test"  # explicit value preserved
        assert cfg.ha_discovery is None
        assert cfg.stats_topic is None
        assert cfg.signal_mimir is False

    def test_signal_mimir_auto_derives_trigger_topic(self) -> None:
        # mimir_trigger_topic is derived from mimir_topic_prefix, so
        # signal_mimir=True without an explicit mimir_trigger_topic is valid.
        cfg = ZonneplanPricesConfig(**{**_VALID_CONFIG, "signal_mimir": True})
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_signal_mimir_with_explicit_trigger_topic(self) -> None:
        cfg = ZonneplanPricesConfig(
            **{**_VALID_CONFIG, "signal_mimir": True, "mimir_trigger_topic": "custom/trigger"}
        )
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "custom/trigger"

    def test_ha_discovery_accepted(self) -> None:
        cfg = ZonneplanPricesConfig(
            **{**_VALID_CONFIG, "ha_discovery": {"enabled": True}}
        )
        assert cfg.ha_discovery is not None
        assert cfg.ha_discovery.enabled is True

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ZonneplanPricesConfig(**{**_VALID_CONFIG, "unknown_field": "x"})

    def test_output_topic_override(self) -> None:
        cfg = ZonneplanPricesConfig(**{**_VALID_CONFIG, "output_topic": "custom/prices"})
        assert cfg.output_topic == "custom/prices"

    def test_stats_topic_accepted(self) -> None:
        cfg = ZonneplanPricesConfig(**{**_VALID_CONFIG, "stats_topic": "mimir/stats/prices"})
        assert cfg.stats_topic == "mimir/stats/prices"
