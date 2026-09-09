"""Unit tests for pv_openmeteo.config.

Tests verify:
- A valid configuration loads correctly.
- The array key is a label and drives the derived output topic.
- Plane geometry bounds are enforced (declination 0-90, azimuth -180-180,
  peak_power_kwp > 0, efficiency_factor in (0, 1]).
- tracking accepts only the four values the library understands.
- An array must declare at least one plane.
- A shared array inverter and per-plane inverters are mutually exclusive.
- Plane latitude and longitude fall back to the site coordinates.
- Horizon points are ordered and bounded, and use_horizon is derived from them.
- confidence_decay and open_meteo defaults apply when the sections are omitted.
- Unknown fields anywhere in the document are rejected.
"""

import pytest
from pydantic import ValidationError

from pv_openmeteo.config import PvOpenMeteoConfig


def _base_raw(**overrides: object) -> dict:
    raw: dict = {
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "test-pv-openmeteo",
        },
        "trigger_topic": "mimir/input/tools/pv/trigger",
        "site": {"latitude": 52.37, "longitude": 4.89},
        "arrays": {
            "roof": {
                "planes": [
                    {
                        "declination": 35,
                        "azimuth": 0,
                        "peak_power_kwp": 5.0,
                    }
                ]
            }
        },
    }
    raw.update(overrides)
    return raw


def test_valid_config_loads() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw())
    assert config.mqtt.host == "localhost"
    assert list(config.arrays) == ["roof"]
    assert config.arrays["roof"].planes[0].peak_power_kwp == 5.0


def test_output_topic_derived_from_array_key() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw())
    assert config.arrays["roof"].output_topic == "mimir/input/pv/roof/forecast"


def test_output_topic_honours_explicit_value() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["output_topic"] = "somewhere/else"
    config = PvOpenMeteoConfig.model_validate(raw)
    assert config.arrays["roof"].output_topic == "somewhere/else"


def test_mimir_trigger_topic_derived_from_prefix() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw(mimir_topic_prefix="huis"))
    assert config.mimir_trigger_topic == "huis/input/trigger"
    assert config.arrays["roof"].output_topic == "huis/input/pv/roof/forecast"


def test_client_id_defaults_when_absent() -> None:
    raw = _base_raw()
    del raw["mqtt"]["client_id"]
    config = PvOpenMeteoConfig.model_validate(raw)
    assert config.mqtt.client_id == "mimir-pv-openmeteo"


def test_declination_must_be_0_to_90() -> None:
    for bad in [-1, 91]:
        raw = _base_raw()
        raw["arrays"]["roof"]["planes"][0]["declination"] = bad
        with pytest.raises(ValidationError):
            PvOpenMeteoConfig.model_validate(raw)


def test_azimuth_must_be_minus_180_to_180() -> None:
    for bad in [-181, 181, 270]:
        raw = _base_raw()
        raw["arrays"]["roof"]["planes"][0]["azimuth"] = bad
        with pytest.raises(ValidationError):
            PvOpenMeteoConfig.model_validate(raw)


def test_peak_power_must_be_positive() -> None:
    for bad in [0, -1.0]:
        raw = _base_raw()
        raw["arrays"]["roof"]["planes"][0]["peak_power_kwp"] = bad
        with pytest.raises(ValidationError):
            PvOpenMeteoConfig.model_validate(raw)


def test_efficiency_factor_bounds() -> None:
    for bad in [0.0, 1.01]:
        raw = _base_raw()
        raw["arrays"]["roof"]["planes"][0]["efficiency_factor"] = bad
        with pytest.raises(ValidationError):
            PvOpenMeteoConfig.model_validate(raw)


def test_tracking_rejects_unknown_mode() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["tracking"] = "sunflower"
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_tracking_accepts_library_modes() -> None:
    for mode in ["none", "azimuth", "tilt", "dual"]:
        raw = _base_raw()
        raw["arrays"]["roof"]["planes"][0]["tracking"] = mode
        config = PvOpenMeteoConfig.model_validate(raw)
        assert config.arrays["roof"].planes[0].tracking == mode


def test_array_requires_at_least_one_plane() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"] = []
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_shared_and_per_plane_inverter_are_mutually_exclusive() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["inverter_kwp"] = 5.0
    raw["arrays"]["roof"]["planes"][0]["inverter_kwp"] = 3.25
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_shared_inverter_alone_is_accepted() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["inverter_kwp"] = 5.0
    config = PvOpenMeteoConfig.model_validate(raw)
    assert config.arrays["roof"].inverter_kwp == 5.0


def test_per_plane_inverter_may_be_set_on_a_subset() -> None:
    """A plane without its own inverter is unclamped; the library maps None to infinity."""
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"].append(
        {"declination": 35, "azimuth": 90, "peak_power_kwp": 2.0, "inverter_kwp": 1.5}
    )
    config = PvOpenMeteoConfig.model_validate(raw)
    assert config.arrays["roof"].planes[0].inverter_kwp is None
    assert config.arrays["roof"].planes[1].inverter_kwp == 1.5


def test_plane_coordinates_default_to_site() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw())
    plane = config.arrays["roof"].planes[0]
    assert plane.latitude == 52.37
    assert plane.longitude == 4.89


def test_plane_coordinates_can_override_site() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["latitude"] = 51.0
    raw["arrays"]["roof"]["planes"][0]["longitude"] = 3.5
    config = PvOpenMeteoConfig.model_validate(raw)
    plane = config.arrays["roof"].planes[0]
    assert plane.latitude == 51.0
    assert plane.longitude == 3.5


def test_use_horizon_is_derived_from_the_horizon_points() -> None:
    raw = _base_raw()
    assert PvOpenMeteoConfig.model_validate(raw).arrays["roof"].planes[0].use_horizon is False

    raw["arrays"]["roof"]["planes"][0]["horizon"] = [
        {"azimuth": 0, "elevation": 5},
        {"azimuth": 180, "elevation": 12},
        {"azimuth": 360, "elevation": 5},
    ]
    config = PvOpenMeteoConfig.model_validate(raw)
    assert config.arrays["roof"].planes[0].use_horizon is True


def test_horizon_requires_two_points_in_ascending_azimuth() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["horizon"] = [{"azimuth": 0, "elevation": 5}]
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)

    raw["arrays"]["roof"]["planes"][0]["horizon"] = [
        {"azimuth": 180, "elevation": 5},
        {"azimuth": 90, "elevation": 5},
    ]
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_horizon_azimuth_uses_compass_degrees() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["horizon"] = [
        {"azimuth": -1, "elevation": 5},
        {"azimuth": 361, "elevation": 5},
    ]
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_api_defaults() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw())
    assert config.open_meteo.api_key is None
    assert config.open_meteo.base_url == "https://api.open-meteo.com"
    assert config.open_meteo.weather_model is None
    assert config.open_meteo.forecast_days == 3
    assert config.open_meteo.past_hours == 1.0


def test_forecast_days_bounds() -> None:
    for bad in [0, 17]:
        raw = _base_raw(open_meteo={"forecast_days": bad})
        with pytest.raises(ValidationError):
            PvOpenMeteoConfig.model_validate(raw)


def test_confidence_decay_defaults_apply() -> None:
    config = PvOpenMeteoConfig.model_validate(_base_raw())
    assert config.confidence_decay.hours_0_to_6 == 0.90
    assert config.confidence_decay.hours_6_to_24 == 0.75
    assert config.confidence_decay.hours_24_to_48 == 0.55
    assert config.confidence_decay.hours_48_plus == 0.35


def test_signal_mimir_defaults_to_false() -> None:
    assert PvOpenMeteoConfig.model_validate(_base_raw()).signal_mimir is False


def test_unknown_top_level_field_rejected() -> None:
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(_base_raw(nonsense=1))


def test_unknown_plane_field_rejected() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["tilt"] = 35
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_unknown_array_field_rejected() -> None:
    raw = _base_raw()
    raw["arrays"]["roof"]["kwp"] = 5.0
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_horizon_rejects_duplicate_bearings() -> None:
    """numpy.interp needs a strictly increasing axis; equal bearings are ambiguous."""
    raw = _base_raw()
    raw["arrays"]["roof"]["planes"][0]["horizon"] = [
        {"azimuth": 0, "elevation": 5},
        {"azimuth": 90, "elevation": 10},
        {"azimuth": 90, "elevation": 20},
        {"azimuth": 360, "elevation": 5},
    ]
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(raw)


def test_empty_arrays_map_rejected() -> None:
    """A daemon with no arrays would accept every trigger and publish nothing."""
    with pytest.raises(ValidationError):
        PvOpenMeteoConfig.model_validate(_base_raw(arrays={}))
