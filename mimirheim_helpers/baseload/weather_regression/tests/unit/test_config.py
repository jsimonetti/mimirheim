"""Unit tests for baseload_weather.config.

Covers schema validation for every Pydantic model of the weather-dependent
base load helper: the happy path, every documented rejection, derived topic
defaults and the default MQTT client identifier.
"""

from __future__ import annotations

import copy
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from baseload_weather.config import (
    EnergyEntityConfig,
    ModelConfig,
    RecorderConfig,
    TemperatureEntityConfig,
    WeatherBaseloadConfig,
    WeatherForecastConfig,
)

_VALID: dict[str, Any] = {
    "mqtt": {"host": "localhost"},
    "homeassistant": {
        "db_url": "sqlite:////config/home-assistant_v2.db",
        "sum_entities": [{"entity_id": "sensor.grid_import_kwh"}],
        "subtract_entities": [{"entity_id": "sensor.ev_kwh", "unit": "Wh"}],
        "temperature_entity": {"entity_id": "sensor.outdoor_temperature"},
    },
    "weather_forecast": {"latitude": 52.0, "longitude": 5.1},
    "model": {"timezone": "Europe/Amsterdam"},
}


def _cfg(**overrides: Any) -> dict[str, Any]:
    """Return a deep copy of the valid config with top-level overrides applied."""
    data = copy.deepcopy(_VALID)
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    "model,data",
    [
        (TemperatureEntityConfig, {"entity_id": "sensor.t"}),
        (
            RecorderConfig,
            {
                "db_url": "sqlite://",
                "sum_entities": [{"entity_id": "sensor.e"}],
                "temperature_entity": {"entity_id": "sensor.t"},
            },
        ),
        (WeatherForecastConfig, {"latitude": 0.0, "longitude": 0.0}),
        (ModelConfig, {"timezone": "UTC"}),
    ],
)
def test_nested_models_reject_unknown_fields(model: type, data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model(**data, bogus=1)


class TestEnergyEntityConfig:
    def test_defaults(self) -> None:
        e = EnergyEntityConfig(entity_id="sensor.e")
        assert e.unit is None
        assert e.outlier_factor == 10.0

    @pytest.mark.parametrize("unit", ["Wh", "kWh", "MWh"])
    def test_energy_units_accepted(self, unit: str) -> None:
        assert EnergyEntityConfig(entity_id="sensor.e", unit=unit).unit == unit

    @pytest.mark.parametrize("unit", ["W", "kW", "°C", "J"])
    def test_non_energy_units_rejected(self, unit: str) -> None:
        with pytest.raises(ValidationError):
            EnergyEntityConfig(entity_id="sensor.e", unit=unit)

    def test_outlier_factor_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            EnergyEntityConfig(entity_id="sensor.e", outlier_factor=0.0)

    def test_rejects_unknown_fields(self) -> None:
        with pytest.raises(ValidationError):
            EnergyEntityConfig(entity_id="sensor.e", bogus=1)


class TestTemperatureEntityConfig:
    @pytest.mark.parametrize("unit", [None, "°C", "°F"])
    def test_units_accepted(self, unit: str | None) -> None:
        assert TemperatureEntityConfig(entity_id="sensor.t", unit=unit).unit == unit

    @pytest.mark.parametrize("unit", ["K", "C", "kWh"])
    def test_other_units_rejected(self, unit: str) -> None:
        with pytest.raises(ValidationError):
            TemperatureEntityConfig(entity_id="sensor.t", unit=unit)


class TestRecorderConfig:
    def test_requires_at_least_one_sum_entity(self) -> None:
        with pytest.raises(ValidationError):
            RecorderConfig(
                db_url="sqlite://",
                sum_entities=[],
                temperature_entity={"entity_id": "sensor.t"},
            )

    def test_subtract_entities_default_empty(self) -> None:
        r = RecorderConfig(
            db_url="sqlite://",
            sum_entities=[{"entity_id": "sensor.e"}],
            temperature_entity={"entity_id": "sensor.t"},
        )
        assert r.subtract_entities == []

    def test_temperature_entity_required(self) -> None:
        with pytest.raises(ValidationError):
            RecorderConfig(db_url="sqlite://", sum_entities=[{"entity_id": "sensor.e"}])


class TestWeatherForecastConfig:
    def test_defaults(self) -> None:
        w = WeatherForecastConfig(latitude=0.0, longitude=0.0)
        assert w.base_url == "https://api.open-meteo.com"
        assert w.timeout_s == 30.0

    @pytest.mark.parametrize(
        "field,value",
        [("latitude", 90.1), ("latitude", -90.1), ("longitude", 180.1), ("longitude", -180.1)],
    )
    def test_coordinates_bounded(self, field: str, value: float) -> None:
        data = {"latitude": 0.0, "longitude": 0.0, field: value}
        with pytest.raises(ValidationError):
            WeatherForecastConfig(**data)

    @pytest.mark.parametrize("timeout", [0.5, 121.0])
    def test_timeout_bounded(self, timeout: float) -> None:
        with pytest.raises(ValidationError):
            WeatherForecastConfig(latitude=0.0, longitude=0.0, timeout_s=timeout)


class TestModelConfig:
    def test_defaults(self) -> None:
        m = ModelConfig(timezone="UTC")
        assert m.lookback_days == 56
        assert m.lookback_decay == 1.0
        assert m.smoothing_hours == 12.0
        assert m.t_base_min_c == 10.0
        assert m.t_base_max_c == 20.0
        assert m.min_samples == 10
        assert m.min_hdd_variance == 1.0
        assert m.exclude_dates == []

    def test_timezone_required(self) -> None:
        with pytest.raises(ValidationError):
            ModelConfig()

    @pytest.mark.parametrize("tz", ["Mars/Olympus_Mons", "", "../etc/passwd", "Europe/", "/UTC"])
    def test_unknown_or_malformed_timezone_rejected(self, tz: str) -> None:
        with pytest.raises(ValidationError):
            ModelConfig(timezone=tz)

    def test_invalid_exclude_date_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ModelConfig(timezone="UTC", exclude_dates=["2026-02-30"])

    def test_exclude_dates_parsed(self) -> None:
        m = ModelConfig(timezone="UTC", exclude_dates=["2026-07-22", "2026-11-12"])
        assert m.exclude_dates == [date(2026, 7, 22), date(2026, 11, 12)]

    @pytest.mark.parametrize(
        "field,value",
        [
            ("lookback_days", 6),
            ("lookback_days", 401),
            ("lookback_decay", 0.99),
            ("smoothing_hours", 0.5),
            ("smoothing_hours", 73.0),
            ("min_samples", 2),
            ("min_hdd_variance", 0.0),
        ],
    )
    def test_bounds(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            ModelConfig(timezone="UTC", **{field: value})

    @pytest.mark.parametrize("lo,hi", [(15.0, 15.0), (18.0, 12.0)])
    def test_t_base_range_must_be_increasing(self, lo: float, hi: float) -> None:
        with pytest.raises(ValidationError):
            ModelConfig(timezone="UTC", t_base_min_c=lo, t_base_max_c=hi)

    @pytest.mark.parametrize("field", ["t_base_min_c", "t_base_max_c"])
    @pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
    def test_t_base_bounds_must_be_finite(self, field: str, value: float) -> None:
        with pytest.raises(ValidationError):
            ModelConfig(timezone="UTC", **{field: value})


class TestWeatherBaseloadConfig:
    def test_valid_minimal(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg())
        assert cfg.horizon_hours == 96
        assert cfg.signal_mimir is False
        assert cfg.stats_topic is None
        assert cfg.model_stats_topic is None
        assert cfg.ha_discovery is None

    def test_derived_topics(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg())
        assert cfg.trigger_topic == "mimir/input/tools/baseload/trigger"
        assert cfg.output_topic == "mimir/input/baseload/base_load/forecast"
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_derived_topics_follow_prefix_and_load_name(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(
            _cfg(mimir_topic_prefix="home", mimir_static_load_name="house")
        )
        assert cfg.trigger_topic == "home/input/tools/baseload/trigger"
        assert cfg.output_topic == "home/input/baseload/house/forecast"
        assert cfg.mimir_trigger_topic == "home/input/trigger"

    def test_explicit_topics_kept(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(
            _cfg(trigger_topic="t/x", output_topic="o/x", mimir_trigger_topic="m/x")
        )
        assert (cfg.trigger_topic, cfg.output_topic, cfg.mimir_trigger_topic) == ("t/x", "o/x", "m/x")

    def test_default_client_id(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg())
        assert cfg.mqtt.client_id == "mimir-baseload-weather"

    def test_explicit_client_id_kept(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg(mqtt={"host": "h", "client_id": "mine"}))
        assert cfg.mqtt.client_id == "mine"

    def test_client_id_differs_from_homeassistant_db_helper_default(self) -> None:
        # Helpers may not import other helper packages, so the homeassistant_db
        # helper's default ("mimir-baseload", baseload_ha_db/config.py) is
        # restated here rather than imported.
        cfg = WeatherBaseloadConfig.model_validate(_cfg())
        assert cfg.mqtt.client_id != "mimir-baseload"

    def test_signal_mimir_derives_trigger_topic(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg(signal_mimir=True))
        assert cfg.mimir_trigger_topic == "mimir/input/trigger"

    def test_signal_mimir_rejects_empty_trigger_topic(self) -> None:
        with pytest.raises(ValidationError):
            WeatherBaseloadConfig.model_validate(_cfg(signal_mimir=True, mimir_trigger_topic=""))

    def test_ha_discovery_accepted(self) -> None:
        cfg = WeatherBaseloadConfig.model_validate(_cfg(ha_discovery={}))
        assert cfg.ha_discovery is not None

    @pytest.mark.parametrize("hours", [0, 169])
    def test_horizon_bounded(self, hours: int) -> None:
        with pytest.raises(ValidationError):
            WeatherBaseloadConfig.model_validate(_cfg(horizon_hours=hours))

    @pytest.mark.parametrize("section", ["homeassistant", "weather_forecast", "model"])
    def test_sections_required(self, section: str) -> None:
        data = _cfg()
        del data[section]
        with pytest.raises(ValidationError):
            WeatherBaseloadConfig.model_validate(data)

    def test_rejects_unknown_top_level_field(self) -> None:
        with pytest.raises(ValidationError):
            WeatherBaseloadConfig.model_validate(_cfg(bogus=True))
