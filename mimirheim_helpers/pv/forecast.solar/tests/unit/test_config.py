"""Unit tests for pv_fetcher.config.

Tests verify:
- A valid configuration loads correctly.
- The array name is purely a label (no constraint imposed on it).
- When output_topic is omitted, it is derived from mimir_topic_prefix and the array key.
- declination must be 0–90.
- azimuth must be -180–180.
- peak_power_kwp must be > 0.
- confidence_decay defaults apply when the section is omitted.
- Unknown fields in mqtt, arrays, or top-level are rejected.
- signal_mimir defaults to False.
- When signal_mimir is True without an explicit trigger topic, the derived trigger is used.
- forecast_solar.api_key defaults to None.
"""

import pytest
from pydantic import ValidationError

from pv_fetcher.config import PvFetcherConfig


def _base_raw(**overrides: object) -> dict:
    raw: dict = {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "test-pv",
        },
        "trigger_topic": "mimir/input/tools/pv/trigger",
        "forecast_solar": {},
        "arrays": {
            "my_array": {
                "output_topic": "mimir/input/pv",
                "latitude": 52.37,
                "longitude": 4.89,
                "declination": 35,
                "azimuth": 0,
                "peak_power_kwp": 5.0,
            }
        },
        "signal_mimir": False,
    }
    raw.update(overrides)
    return raw


def test_valid_config_loads() -> None:
    config = PvFetcherConfig.model_validate(_base_raw())
    assert config.mqtt.host == "localhost"
    assert len(config.arrays) == 1
    assert "my_array" in config.arrays
    assert config.arrays["my_array"].output_topic == "mimir/input/pv"


def test_array_name_is_arbitrary() -> None:
    """The array key name has no constraint — it is a label for logging only."""
    for name in ["roof_pv", "anything_at_all", "a", "123"]:
        config = PvFetcherConfig.model_validate(_base_raw(arrays={
            name: {
                "output_topic": "mimir/input/pv",
                "latitude": 52.0,
                "longitude": 4.0,
                "declination": 30,
                "azimuth": 0,
                "peak_power_kwp": 4.0,
            }
        }))
        assert name in config.arrays


def test_declination_must_be_0_to_90() -> None:
    for bad in [-1, 91]:
        raw = _base_raw()
        raw["arrays"]["my_array"]["declination"] = bad
        with pytest.raises(ValidationError):
            PvFetcherConfig.model_validate(raw)


def test_declination_boundary_values_accepted() -> None:
    for val in [0, 90]:
        raw = _base_raw()
        raw["arrays"]["my_array"]["declination"] = val
        PvFetcherConfig.model_validate(raw)


def test_azimuth_must_be_minus_180_to_180() -> None:
    for bad in [-181, 181]:
        raw = _base_raw()
        raw["arrays"]["my_array"]["azimuth"] = bad
        with pytest.raises(ValidationError):
            PvFetcherConfig.model_validate(raw)


def test_azimuth_boundary_values_accepted() -> None:
    for val in [-180, 180]:
        raw = _base_raw()
        raw["arrays"]["my_array"]["azimuth"] = val
        PvFetcherConfig.model_validate(raw)


def test_peak_power_kwp_must_be_positive() -> None:
    raw = _base_raw()
    raw["arrays"]["my_array"]["peak_power_kwp"] = 0.0
    with pytest.raises(ValidationError):
        PvFetcherConfig.model_validate(raw)


def test_confidence_decay_defaults_applied() -> None:
    config = PvFetcherConfig.model_validate(_base_raw())
    d = config.confidence_decay
    assert d.hours_0_to_6 == 0.90
    assert d.hours_6_to_24 == 0.75
    assert d.hours_24_to_48 == 0.55
    assert d.hours_48_plus == 0.35


def test_confidence_decay_custom_values() -> None:
    raw = _base_raw(confidence_decay={
        "hours_0_to_6": 0.80,
        "hours_6_to_24": 0.60,
        "hours_24_to_48": 0.40,
        "hours_48_plus": 0.20,
    })
    config = PvFetcherConfig.model_validate(raw)
    assert config.confidence_decay.hours_0_to_6 == 0.80


def test_forecast_solar_api_key_defaults_to_none() -> None:
    config = PvFetcherConfig.model_validate(_base_raw())
    assert config.forecast_solar.api_key is None


def test_forecast_solar_api_key_accepted() -> None:
    raw = _base_raw(forecast_solar={"api_key": "abc123"})
    config = PvFetcherConfig.model_validate(raw)
    assert config.forecast_solar.api_key == "abc123"


def test_signal_mimir_defaults_to_false() -> None:
    config = PvFetcherConfig.model_validate(_base_raw())
    assert config.signal_mimir is False


def test_signal_mimir_without_explicit_trigger_uses_derived_topic() -> None:
    """When signal_mimir is True without an explicit trigger topic, the derived topic is used."""
    raw = _base_raw(signal_mimir=True)
    config = PvFetcherConfig.model_validate(raw)
    assert config.signal_mimir is True
    assert config.mimir_trigger_topic == "mimir/input/trigger"


def test_array_output_topic_derived_from_key() -> None:
    """When output_topic is omitted, it is derived from mimir_topic_prefix and the array key."""
    raw = _base_raw()
    del raw["arrays"]["my_array"]["output_topic"]
    config = PvFetcherConfig.model_validate(raw)
    assert config.arrays["my_array"].output_topic == "mimir/input/pv/my_array/forecast"


def test_array_output_topic_derived_custom_prefix() -> None:
    """Derivation respects a custom mimir_topic_prefix."""
    raw = _base_raw(mimir_topic_prefix="mymimir")
    del raw["arrays"]["my_array"]["output_topic"]
    config = PvFetcherConfig.model_validate(raw)
    assert config.arrays["my_array"].output_topic == "mymimir/input/pv/my_array/forecast"


def test_explicit_array_output_topic_not_overwritten() -> None:
    """An explicitly set output_topic is not overwritten by derivation."""
    raw = _base_raw()  # output_topic = "mimir/input/pv" already set
    config = PvFetcherConfig.model_validate(raw)
    assert config.arrays["my_array"].output_topic == "mimir/input/pv"


def test_signal_mimir_true_with_trigger_topic_accepted() -> None:
    raw = _base_raw(signal_mimir=True, mimir_trigger_topic="mimir/input/trigger")
    config = PvFetcherConfig.model_validate(raw)
    assert config.mimir_trigger_topic == "mimir/input/trigger"


def test_unknown_top_level_field_rejected() -> None:
    raw = _base_raw()
    raw["unexpected"] = "value"
    with pytest.raises(ValidationError):
        PvFetcherConfig.model_validate(raw)


def test_unknown_mqtt_field_rejected() -> None:
    raw = _base_raw()
    raw["mqtt"]["unexpected"] = "value"
    with pytest.raises(ValidationError):
        PvFetcherConfig.model_validate(raw)


def test_unknown_array_field_rejected() -> None:
    raw = _base_raw()
    raw["arrays"]["my_array"]["unexpected"] = "value"
    with pytest.raises(ValidationError):
        PvFetcherConfig.model_validate(raw)


def test_multiple_arrays_valid() -> None:
    raw = _base_raw(arrays={
        "roof": {
            "output_topic": "mimir/input/pv/roof",
            "latitude": 52.0, "longitude": 4.0,
            "declination": 35, "azimuth": 0, "peak_power_kwp": 5.0,
        },
        "garage": {
            "output_topic": "mimir/input/pv/garage",
            "latitude": 52.0, "longitude": 4.0,
            "declination": 15, "azimuth": -45, "peak_power_kwp": 2.0,
        },
    })
    config = PvFetcherConfig.model_validate(raw)
    assert len(config.arrays) == 2
