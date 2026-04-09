"""Unit tests for baseload_ha.config.

Covers schema validation for all Pydantic models used by the homeassistant_db baseload tool.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from baseload_ha_db.config import EntityConfig, HaConfig, MqttConfig, BaseloadConfig


_VALID_CONFIG: dict = {
    "mqtt": {
        "host": "localhost",
        "port": 1883,
        "client_id": "ha-baseload-test",
    },
    "trigger_topic": "mimir/input/tools/baseload/trigger",
    "output_topic": "mimir/input/base",
    "homeassistant": {
        "db_url": "sqlite:////config/home-assistant_v2.db",
        "sum_entities": [
            {"entity_id": "sensor.power_l1_w", "unit": "W"},
            {"entity_id": "sensor.power_l2_w", "unit": "kW"},
        ],
        "subtract_entities": [{"entity_id": "sensor.battery_w", "unit": "W"}],
        "lookback_days": 7,
        "horizon_hours": 24,
    },
    "signal_mimir": False,
}


class TestMqttConfig:
    def test_valid_minimal(self) -> None:
        cfg = MqttConfig(host="broker", client_id="id")
        assert cfg.port == 1883

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            MqttConfig(host="broker", client_id="id", unknown="x")


class TestEntityConfig:
    def test_valid(self) -> None:
        e = EntityConfig(entity_id="sensor.power_w", unit="W")
        assert e.entity_id == "sensor.power_w"
        assert e.unit == "W"

    def test_unit_can_be_omitted(self) -> None:
        e = EntityConfig(entity_id="sensor.power_w")
        assert e.unit is None

    def test_mw_and_gw_units_accepted(self) -> None:
        assert EntityConfig(entity_id="sensor.p", unit="MW").unit == "MW"
        assert EntityConfig(entity_id="sensor.p", unit="GW").unit == "GW"

    def test_mw_unit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityConfig(entity_id="sensor.p", unit="mW")

    def test_invalid_unit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityConfig(entity_id="sensor.power", unit="invalid_unit")

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            EntityConfig(entity_id="sensor.power", unit="W", unknown="x")

    def test_outlier_factor_defaults_to_ten(self) -> None:
        e = EntityConfig(entity_id="sensor.p")
        assert e.outlier_factor == pytest.approx(10.0)

    def test_outlier_factor_above_zero_accepted(self) -> None:
        e = EntityConfig(entity_id="sensor.p", outlier_factor=5.0)
        assert e.outlier_factor == pytest.approx(5.0)

    def test_outlier_factor_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityConfig(entity_id="sensor.p", outlier_factor=0.0)

    def test_outlier_factor_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EntityConfig(entity_id="sensor.p", outlier_factor=-1.0)

    def test_energy_unit_kwh_accepted(self) -> None:
        e = EntityConfig(entity_id="sensor.e", unit="kWh")
        assert e.unit == "kWh"

    def test_energy_unit_wh_accepted(self) -> None:
        e = EntityConfig(entity_id="sensor.e", unit="Wh")
        assert e.unit == "Wh"

    def test_energy_unit_mwh_accepted(self) -> None:
        e = EntityConfig(entity_id="sensor.e", unit="MWh")
        assert e.unit == "MWh"


class TestHaConfig:
    def _entity(self, entity_id: str = "sensor.p1", unit: str = "W") -> dict:
        return {"entity_id": entity_id, "unit": unit}

    def test_valid_full(self) -> None:
        cfg = HaConfig(
            db_url="sqlite:////config/ha.db",
            sum_entities=[self._entity()],
            lookback_days=7,
            horizon_hours=24,
        )
        assert cfg.subtract_entities == []
        assert cfg.horizon_hours == 24

    def test_subtract_entities_defaults_to_empty_list(self) -> None:
        cfg = HaConfig(db_url="sqlite:////config/ha.db", sum_entities=[self._entity()])
        assert cfg.subtract_entities == []

    def test_horizon_hours_defaults_to_48(self) -> None:
        cfg = HaConfig(db_url="sqlite:////config/ha.db", sum_entities=[self._entity()])
        assert cfg.horizon_hours == 48

    def test_sum_entities_must_not_be_empty(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(db_url="sqlite:////config/ha.db", sum_entities=[])

    def test_entities_with_mixed_units_accepted(self) -> None:
        cfg = HaConfig(
            db_url="sqlite:////config/ha.db",
            sum_entities=[
                {"entity_id": "sensor.p_w", "unit": "W"},
                {"entity_id": "sensor.pv_kw", "unit": "kW"},
            ],
        )
        assert cfg.sum_entities[0].unit == "W"
        assert cfg.sum_entities[1].unit == "kW"

    def test_entities_without_unit_accepted(self) -> None:
        cfg = HaConfig(
            db_url="sqlite:////config/ha.db",
            sum_entities=[{"entity_id": "sensor.p1"}],
        )
        assert cfg.sum_entities[0].unit is None

    def test_entity_invalid_unit_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[{"entity_id": "sensor.p1", "unit": "invalid_unit"}],
            )

    def test_lookback_days_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                lookback_days=0,
            )

    def test_lookback_days_above_112_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                lookback_days=113,
            )

    def test_lookback_decay_defaults_to_one(self) -> None:
        cfg = HaConfig(
            db_url="sqlite:////config/ha.db",
            sum_entities=[self._entity()],
        )
        assert cfg.lookback_decay == 1.0

    def test_lookback_decay_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                lookback_decay=0.5,
            )

    def test_horizon_hours_below_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                horizon_hours=0,
            )

    def test_horizon_hours_above_168_rejected(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                horizon_hours=169,
            )

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            HaConfig(
                db_url="sqlite:////config/ha.db",
                sum_entities=[self._entity()],
                extra_field="bad",
            )


class TestBaseloadConfig:
    def test_valid_full_config(self) -> None:
        cfg = BaseloadConfig.model_validate(_VALID_CONFIG)
        assert cfg.homeassistant.sum_entities[0].entity_id == "sensor.power_l1_w"
        assert cfg.homeassistant.sum_entities[1].unit == "kW"
        assert cfg.homeassistant.subtract_entities[0].unit == "W"

    def test_signal_mimir_without_explicit_trigger_uses_derived_topic(self) -> None:
        """When signal_mimir is True and no explicit trigger topic is given, the derived topic is used."""
        cfg = BaseloadConfig.model_validate({**_VALID_CONFIG, "signal_mimir": True})
        assert cfg.signal_mimir is True
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_signal_mimir_with_trigger_topic_accepted(self) -> None:
        good = {
            **_VALID_CONFIG,
            "signal_mimir": True,
            "mimir_trigger_topic": "mimir/input/trigger",
        }
        cfg = BaseloadConfig.model_validate(good)
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_rejects_unknown_top_level_fields(self) -> None:
        bad = {**_VALID_CONFIG, "unexpected": True}
        with pytest.raises(ValidationError):
            BaseloadConfig.model_validate(bad)
