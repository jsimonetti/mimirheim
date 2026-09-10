"""Unit tests for epexpredictor_prices.config.

Covers schema validation for all Pydantic models used by the
epexpredictor_prices tool.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from epexpredictor_prices.config import (
    ConfidenceDecayConfig,
    EpexPredictorApiConfig,
    EpexPredictorPricesConfig,
)


_VALID_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "client_id": "epexpredictor-test",
    },
    "trigger_topic": "mimir/input/tools/prices/trigger",
    "output_topic": "mimir/input/prices",
    "epexpredictor": {
        "area": "NL",
    },
    "signal_mimir": False,
}


class TestEpexPredictorApiConfig:
    def test_valid_minimal(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL")
        assert cfg.area == "NL"
        assert cfg.import_formula == "price"
        assert cfg.export_formula == "price"

    def test_base_url_defaults_to_public_instance(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL")
        assert cfg.base_url == "https://epexpredictor.batzill.com"

    def test_base_url_override_is_preserved(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL", base_url="https://self-hosted.example.com")
        assert cfg.base_url == "https://self-hosted.example.com"

    def test_horizon_hours_defaults_to_72(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL")
        assert cfg.horizon_hours == 72

    def test_horizon_hours_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EpexPredictorApiConfig(area="NL", horizon_hours=0)

    def test_horizon_hours_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EpexPredictorApiConfig(area="NL", horizon_hours=-1)

    def test_custom_import_formula_accepted(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL", import_formula="price * 1.1")
        assert cfg.import_formula == "price * 1.1"

    def test_custom_export_formula_accepted(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL", export_formula="price * 0.9")
        assert cfg.export_formula == "price * 0.9"

    def test_invalid_import_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            EpexPredictorApiConfig(area="NL", import_formula="price +* 0.1")

    def test_invalid_export_formula_rejected(self) -> None:
        with pytest.raises(ValidationError, match="syntax"):
            EpexPredictorApiConfig(area="NL", export_formula="def bad():")

    def test_price_interval_defaults_to_quarter_hourly(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL")
        assert cfg.price_interval == "quarter_hourly"

    def test_price_interval_accepts_hourly(self) -> None:
        cfg = EpexPredictorApiConfig(area="NL", price_interval="hourly")
        assert cfg.price_interval == "hourly"

    def test_price_interval_rejects_unknown_value(self) -> None:
        with pytest.raises(ValidationError):
            EpexPredictorApiConfig(area="NL", price_interval="half_hourly")

    def test_unknown_area_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EpexPredictorApiConfig(area="XX")

    @pytest.mark.parametrize(
        "area",
        ["DE", "AT", "BE", "NL", "SE1", "SE2", "SE3", "SE4", "DK1", "DK2", "ES", "PT"],
    )
    def test_every_supported_area_accepted(self, area: str) -> None:
        cfg = EpexPredictorApiConfig(area=area)
        assert cfg.area == area

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            EpexPredictorApiConfig(area="NL", currency="EUR")


class TestConfidenceDecayConfig:
    def test_defaults(self) -> None:
        cfg = ConfidenceDecayConfig()
        assert cfg.hours_0_to_6 == 0.90
        assert cfg.hours_6_to_24 == 0.75
        assert cfg.hours_24_to_48 == 0.55
        assert cfg.hours_48_plus == 0.35
        assert cfg.known_until_confidence is None

    def test_band_overrides_respected(self) -> None:
        cfg = ConfidenceDecayConfig(
            hours_0_to_6=0.99,
            hours_6_to_24=0.80,
            hours_24_to_48=0.60,
            hours_48_plus=0.10,
        )
        assert cfg.hours_0_to_6 == 0.99
        assert cfg.hours_6_to_24 == 0.80
        assert cfg.hours_24_to_48 == 0.60
        assert cfg.hours_48_plus == 0.10

    def test_known_until_confidence_accepts_one(self) -> None:
        cfg = ConfidenceDecayConfig(known_until_confidence=1.0)
        assert cfg.known_until_confidence == 1.0

    def test_known_until_confidence_accepts_zero(self) -> None:
        cfg = ConfidenceDecayConfig(known_until_confidence=0.0)
        assert cfg.known_until_confidence == 0.0

    def test_known_until_confidence_above_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConfidenceDecayConfig(known_until_confidence=1.1)

    def test_known_until_confidence_below_range_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ConfidenceDecayConfig(known_until_confidence=-0.1)

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            ConfidenceDecayConfig(unknown_field=1.0)


class TestEpexPredictorPricesConfig:
    def test_valid_full_config(self) -> None:
        cfg = EpexPredictorPricesConfig.model_validate(_VALID_CONFIG)
        assert cfg.epexpredictor.area == "NL"
        assert cfg.signal_mimir is False

    def test_confidence_decay_omitted_uses_defaults(self) -> None:
        cfg = EpexPredictorPricesConfig.model_validate(_VALID_CONFIG)
        assert cfg.confidence_decay.hours_0_to_6 == 0.90
        assert cfg.confidence_decay.hours_6_to_24 == 0.75
        assert cfg.confidence_decay.hours_24_to_48 == 0.55
        assert cfg.confidence_decay.hours_48_plus == 0.35
        assert cfg.confidence_decay.known_until_confidence is None

    def test_output_topic_defaults_to_canonical_prices_topic(self) -> None:
        raw = {k: v for k, v in _VALID_CONFIG.items() if k != "output_topic"}
        cfg = EpexPredictorPricesConfig.model_validate(raw)
        assert cfg.output_topic == "mimir/input/prices"

    def test_mimir_trigger_topic_defaults_to_canonical_trigger_topic(self) -> None:
        cfg = EpexPredictorPricesConfig.model_validate(
            {**_VALID_CONFIG, "signal_mimir": True}
        )
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_client_id_defaults_to_mimir_epexpredictor(self) -> None:
        raw = {
            **_VALID_CONFIG,
            "mqtt": {"host": "localhost", "port": 1883},
        }
        cfg = EpexPredictorPricesConfig.model_validate(raw)
        assert cfg.mqtt.client_id == "mimir-epexpredictor"

    def test_explicit_client_id_preserved(self) -> None:
        cfg = EpexPredictorPricesConfig.model_validate(_VALID_CONFIG)
        assert cfg.mqtt.client_id == "epexpredictor-test"

    def test_rejects_unknown_top_level_fields(self) -> None:
        bad = {**_VALID_CONFIG, "extra": "not_allowed"}
        with pytest.raises(ValidationError):
            EpexPredictorPricesConfig.model_validate(bad)

    def test_rejects_unknown_nested_field(self) -> None:
        bad = {**_VALID_CONFIG, "epexpredictor": {"area": "NL", "bad_field": "x"}}
        with pytest.raises(ValidationError):
            EpexPredictorPricesConfig.model_validate(bad)

    def test_missing_mqtt_host_rejected(self) -> None:
        bad = {**_VALID_CONFIG, "mqtt": {"client_id": "id"}}
        with pytest.raises(ValidationError):
            EpexPredictorPricesConfig.model_validate(bad)


class TestHomeAssistantConfig:
    def test_forecast_sensor_in_full_config(self) -> None:
        from helper_common.config import HomeAssistantConfig

        raw = {**_VALID_CONFIG, "ha_discovery": {"enabled": True, "forecast_sensor": True}}
        cfg = EpexPredictorPricesConfig.model_validate(raw)
        assert cfg.ha_discovery is not None
        assert cfg.ha_discovery.forecast_sensor is True
        assert HomeAssistantConfig(forecast_sensor=True).forecast_sensor is True

    def test_ha_discovery_rejects_unknown_fields(self) -> None:
        raw = {**_VALID_CONFIG, "ha_discovery": {"enabled": True, "bad_field": "x"}}
        with pytest.raises(ValidationError):
            EpexPredictorPricesConfig.model_validate(raw)
