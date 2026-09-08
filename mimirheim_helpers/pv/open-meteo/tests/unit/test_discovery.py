"""Unit tests for PvOpenMeteoDaemon._publish_discovery.

The base class builds its forecast sensor from a single top-level
``output_topic``, which this helper does not have: every array publishes to its
own topic. These tests verify the override that registers one sensor per array.

Tests verify:
- The trigger button from the base class is still published.
- One forecast sensor is published per array, reading that array's topic.
- Every entity is grouped under one HA device.
- No sensors are published when discovery or the forecast sensor is switched off.
"""

import json
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt

from pv_openmeteo.__main__ import PvOpenMeteoDaemon
from pv_openmeteo.config import PvOpenMeteoConfig


def _make_daemon(ha: dict | None) -> PvOpenMeteoDaemon:
    raw: dict = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "trigger_topic": "test/trigger",
        "site": {"latitude": 52.0, "longitude": 5.0},
        "arrays": {
            "solaredge": {"planes": [{"declination": 25, "azimuth": -90, "peak_power_kwp": 1.04}]},
            "enphase": {"planes": [{"declination": 45, "azimuth": 90, "peak_power_kwp": 4.35}]},
        },
    }
    if ha is not None:
        raw["ha_discovery"] = ha
    daemon = PvOpenMeteoDaemon(PvOpenMeteoConfig.model_validate(raw))
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    daemon._client = client
    return daemon


def _published(daemon: PvOpenMeteoDaemon) -> dict[str, dict]:
    """Return the non-empty retained discovery payloads, keyed by topic."""
    out: dict[str, dict] = {}
    for call in daemon._client.publish.call_args_list:
        topic, payload = call.args[0], call.args[1]
        if payload:
            out[topic] = json.loads(payload)
    return out


def test_button_and_one_forecast_sensor_per_array() -> None:
    daemon = _make_daemon({"enabled": True})
    daemon._publish_discovery()
    published = _published(daemon)

    assert "homeassistant/button/pv_open_meteo/config" in published
    solaredge = published["homeassistant/sensor/pv_open_meteo_solaredge_forecast/config"]
    enphase = published["homeassistant/sensor/pv_open_meteo_enphase_forecast/config"]
    assert solaredge["state_topic"] == "mimir/input/pv/solaredge/forecast"
    assert enphase["state_topic"] == "mimir/input/pv/enphase/forecast"
    assert solaredge["unit_of_measurement"] == "kW"
    assert solaredge["device_class"] == "power"


def test_all_entities_share_one_ha_device() -> None:
    daemon = _make_daemon({"enabled": True, "device_name": "PV Open-Meteo"})
    daemon._publish_discovery()
    published = _published(daemon)

    identifiers = {
        tuple(payload["device"]["identifiers"]) for payload in published.values()
    }
    assert identifiers == {("pv_open_meteo",)}
    names = {payload["device"]["name"] for payload in published.values()}
    assert names == {"PV Open-Meteo"}


def test_no_forecast_sensors_when_the_option_is_off() -> None:
    daemon = _make_daemon({"enabled": True, "forecast_sensor": False})
    daemon._publish_discovery()
    published = _published(daemon)

    assert "homeassistant/button/pv_open_meteo/config" in published
    assert not [t for t in published if t.startswith("homeassistant/sensor/")]


def test_nothing_published_when_discovery_is_disabled() -> None:
    daemon = _make_daemon(None)
    daemon._publish_discovery()
    assert _published(daemon) == {}
