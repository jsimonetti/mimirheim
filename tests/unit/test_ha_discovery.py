"""Unit tests for mimirheim.io.ha_discovery.

Tests verify that ``publish_discovery`` uses the HA MQTT device JSON discovery
format (requires HA 2024.2+):

- A single retained QoS-1 payload is published to
  ``{discovery_prefix}/device/{device_id}/config``.
- The payload is valid JSON with top-level keys ``device``, ``origin``,
  ``availability``, and ``components``.
- The ``components`` map defines every expected entity with a ``platform`` key
  and a ``unique_id`` that matches the map key.
- Device-level availability is used; no per-component ``availability`` key is
  present.
- ``publish_discovery`` is a no-op when ``homeassistant.enabled`` is False.

``active_discovery_topics`` and ``cleanup_stale_discovery`` were removed in
plan 47 and are not tested here.
"""

import json
from typing import Any
from unittest.mock import MagicMock

from mimirheim.config.schema import MimirheimConfig
from mimirheim.io.ha_discovery import publish_discovery


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

def _make_config(ha_enabled: bool = True, device_id: str | None = None) -> MimirheimConfig:
    """Return a config with one battery, one PV array, and one static load."""
    ha_block: dict = {"enabled": ha_enabled, "device_name": "Test mimirheim"}
    if device_id is not None:
        ha_block["device_id"] = device_id

    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "mimir-test",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "homeassistant": ha_block,
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 10.0,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {
                    "soc": {"topic": "mimir/input/bat/soc", "unit": "kwh"},
                },
            },
        },
        "pv_arrays": {
            "roof_pv": {"max_power_kw": 5.0, "topic_forecast": "mimir/input/pv"},
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base"},
        },
    })


def _publish_and_get_payload(config: MimirheimConfig) -> dict[str, Any]:
    """Call publish_discovery and return the parsed JSON payload."""
    client = MagicMock()
    publish_discovery(client, config)
    return json.loads(client.publish.call_args_list[0].args[1])


def _publish_and_get_components(config: MimirheimConfig) -> dict[str, Any]:
    """Call publish_discovery and return the ``components`` map from the payload."""
    return _publish_and_get_payload(config)["components"]


def _make_config_new_devices() -> MimirheimConfig:
    """Return a config covering all plan 24-27 device types, each with inputs."""
    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "mimir-test",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "hybrid_inverters": {
            "hybrid1": {
                "capacity_kwh": 10.0,
                "max_charge_kw": 5.0,
                "max_discharge_kw": 5.0,
                "max_pv_kw": 6.0,
                "topic_pv_forecast": "mimir/input/hybrid1/pv_forecast",
                "inputs": {"soc": {"topic": "mimir/input/hybrid1/soc", "unit": "kwh"}},
            },
        },
        "thermal_boilers": {
            "dhw_boiler": {
                "volume_liters": 200.0,
                "elec_power_kw": 2.0,
                "setpoint_c": 60.0,
                "min_temp_c": 45.0,
                "cooling_rate_k_per_hour": 1.0,
                "inputs": {"topic_current_temp": "mimir/input/dhw_boiler/temp"},
            },
        },
        "space_heating_hps": {
            "sh_hp": {
                "elec_power_kw": 5.0,
                "cop": 3.5,
                "inputs": {"topic_heat_needed_kwh": "mimir/input/sh_hp/heat_needed"},
            },
        },
        "combi_heat_pumps": {
            "combi_hp": {
                "elec_power_kw": 5.0,
                "cop_dhw": 2.5,
                "cop_sh": 3.5,
                "volume_liters": 200.0,
                "setpoint_c": 60.0,
                "min_temp_c": 45.0,
                "cooling_rate_k_per_hour": 1.0,
                "inputs": {
                    "topic_current_temp": "mimir/input/combi_hp/temp",
                    "topic_heat_needed_kwh": "mimir/input/combi_hp/sh_demand",
                },
            },
        },
    })


def _make_config_pv_capabilities() -> MimirheimConfig:
    """Return a config with a PV array that has both output capabilities enabled."""
    return MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "pv_arrays": {
            "roof_pv": {
                "max_power_kw": 6.0,
                "topic_forecast": "mimir/input/pv_forecast",
                "capabilities": {"power_limit": True, "zero_export": True},
                "outputs": {
                    "power_limit_kw": "mimir/output/roof_pv/power_limit",
                    "zero_export_mode": "mimir/output/roof_pv/zero_export",
                },
            },
        },
    })


def _make_config_battery_zem() -> MimirheimConfig:
    """Return a config with a battery that has the zero_exchange capability enabled."""
    return MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 5.4,
                "charge_segments": [{"power_max_kw": 2.5, "efficiency": 0.92}],
                "discharge_segments": [{"power_max_kw": 2.5, "efficiency": 0.92}],
                "capabilities": {"zero_exchange": True},
                "outputs": {"exchange_mode": "mimir/output/home_battery/exchange_mode"},
            },
        },
    })


def _make_config_with_deferrable_rec_start() -> MimirheimConfig:
    """Config with a deferrable load that has topic_recommended_start_time configured."""
    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "mimir-test",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "deferrable_loads": {
            "wash": {
                "power_profile": [1.5, 1.5],
                "topic_window_earliest": "mimir/load/wash/window_earliest",
                "topic_window_latest": "mimir/load/wash/window_latest",
                "topic_recommended_start_time": "mimir/load/wash/recommended_start",
            }
        },
    })


# ---------------------------------------------------------------------------
# Basic publication shape
# ---------------------------------------------------------------------------


def test_no_publish_when_ha_disabled() -> None:
    """publish_discovery is a no-op when homeassistant.enabled is False."""
    config = _make_config(ha_enabled=False)
    client = MagicMock()
    publish_discovery(client, config)
    client.publish.assert_not_called()


def test_publish_discovery_publishes_exactly_one_payload() -> None:
    """publish_discovery makes exactly one client.publish() call.

    The HA device JSON format encodes the entire device in a single retained
    payload. There is no loop over entities.
    """
    config = _make_config()
    client = MagicMock()
    publish_discovery(client, config)
    assert client.publish.call_count == 1


def test_discovery_topic_is_device_json_format() -> None:
    """The single MQTT topic is {discovery_prefix}/device/{device_id}/config."""
    config = _make_config()
    client = MagicMock()
    publish_discovery(client, config)
    topic = client.publish.call_args_list[0].args[0]
    assert topic == "homeassistant/device/mimir-test/config", (
        f"Expected device JSON discovery topic, got: {topic!r}"
    )


def test_single_publish_is_retained_qos1() -> None:
    """The discovery publish uses qos=1 and retain=True."""
    config = _make_config()
    client = MagicMock()
    publish_discovery(client, config)
    c = client.publish.call_args_list[0]
    assert c.kwargs.get("qos") == 1, f"Expected qos=1: {c}"
    assert c.kwargs.get("retain") is True, f"Expected retain=True: {c}"


def test_payload_is_valid_json_with_device_and_components() -> None:
    """The payload has top-level keys device, origin, availability, and components."""
    payload = _publish_and_get_payload(_make_config())
    for key in ("device", "origin", "availability", "components"):
        assert key in payload, f"Payload missing key {key!r}: {list(payload.keys())}"
    assert isinstance(payload["components"], dict), "components must be a dict"


def test_device_block_has_correct_identifiers() -> None:
    """payload['device']['identifiers'] is [device_id]."""
    payload = _publish_and_get_payload(_make_config(device_id="my-mimirheim"))
    assert payload["device"]["identifiers"] == ["my-mimirheim"]


def test_device_block_uses_configured_device_id() -> None:
    """device.identifiers uses the explicit device_id when configured."""
    payload = _publish_and_get_payload(_make_config(device_id="my-unique-mimirheim"))
    assert payload["device"]["identifiers"] == ["my-unique-mimirheim"]


def test_device_block_defaults_to_client_id() -> None:
    """device.identifiers falls back to mqtt.client_id when device_id is None."""
    payload = _publish_and_get_payload(_make_config())
    assert payload["device"]["identifiers"] == ["mimir-test"]


def test_custom_discovery_prefix() -> None:
    """discovery_prefix is used as the topic root instead of 'homeassistant'."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "discovery_prefix": "custom-prefix", "device_name": "Test"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
    })
    client = MagicMock()
    publish_discovery(client, config)
    topic = client.publish.call_args_list[0].args[0]
    assert topic.startswith("custom-prefix/"), (
        f"Expected 'custom-prefix' in topic: {topic!r}"
    )


def test_components_contains_expected_entities() -> None:
    """For 1 battery + 1 PV + 1 static_load, the components map has 8 entries:
    grid_import_kw, grid_export_kw, solve_status, strategy, trigger_run,
    and 3 device setpoints."""
    components = _publish_and_get_components(_make_config())
    assert len(components) == 8, (
        f"Expected 8 components, got {len(components)}: {list(components.keys())}"
    )
    device_id = "mimir-test"
    for uid in (
        f"{device_id}_grid_import_kw",
        f"{device_id}_grid_export_kw",
        f"{device_id}_solve_status",
        f"{device_id}_strategy",
        f"{device_id}_trigger_run",
        f"{device_id}_home_battery_setpoint_kw",
        f"{device_id}_roof_pv_setpoint_kw",
        f"{device_id}_base_load_setpoint_kw",
    ):
        assert uid in components, f"Expected component {uid!r} in components map"


def test_each_component_has_platform_and_unique_id() -> None:
    """Every entry in the components map has 'platform' and 'unique_id' keys."""
    components = _publish_and_get_components(_make_config())
    for uid, entity in components.items():
        assert "platform" in entity, f"Component {uid!r} missing 'platform'"
        assert "unique_id" in entity, f"Component {uid!r} missing 'unique_id'"


def test_component_unique_id_matches_map_key() -> None:
    """Each component's unique_id must equal the key it is stored under."""
    components = _publish_and_get_components(_make_config())
    for key, entity in components.items():
        assert entity["unique_id"] == key, (
            f"Component key {key!r} does not match unique_id {entity['unique_id']!r}"
        )


def test_device_level_availability_is_present() -> None:
    """Top-level 'availability' key is present; no per-component availability key exists.

    Device-level availability applies to all components automatically. Per-component
    availability keys would be redundant and are explicitly forbidden here.
    """
    payload = _publish_and_get_payload(_make_config())
    assert "availability" in payload, "Top-level availability key missing from payload"
    for uid, entity in payload["components"].items():
        assert "availability" not in entity, (
            f"Component {uid!r} must not have a per-component availability key"
        )


def test_availability_topic_matches_outputs_config() -> None:
    """payload['availability']['topic'] matches outputs.availability."""
    payload = _publish_and_get_payload(_make_config())
    assert payload["availability"]["topic"] == "mimir/status/availability"


# ---------------------------------------------------------------------------
# state_class discipline
# ---------------------------------------------------------------------------


def test_grid_sensors_have_no_state_class() -> None:
    """Grid import and export sensors must not carry state_class.

    These sensors show scheduled (forecasted) power values. state_class:
    'measurement' would cause HA to accumulate long-term statistics for
    forecasted values, which is meaningless and unwanted.
    """
    components = _publish_and_get_components(_make_config())
    for uid, entity in components.items():
        if "grid_import" in uid or "grid_export" in uid:
            assert "state_class" not in entity, (
                f"Grid sensor {uid!r} must not have state_class: {entity}"
            )


def test_setpoint_sensors_have_no_state_class() -> None:
    """Per-device setpoint sensors must not carry state_class."""
    components = _publish_and_get_components(_make_config())
    for uid, entity in components.items():
        if "setpoint" in uid:
            assert "state_class" not in entity, (
                f"Setpoint sensor {uid!r} must not have state_class: {entity}"
            )


# ---------------------------------------------------------------------------
# Strategy select entity
# ---------------------------------------------------------------------------


def test_strategy_select_is_published() -> None:
    """The strategy component is in the components map with the correct fields.

    state_topic and command_topic must both be the input strategy topic.
    retain must be True so that a broker restart does not wipe the selection
    and cause the entity to show ``unknown`` after HA reloads its MQTT
    integration.
    """
    components = _publish_and_get_components(_make_config())
    strategy = next(
        (v for k, v in components.items() if "strategy" in k and "setpoint" not in k), None
    )
    assert strategy is not None, "Expected a strategy select component"
    assert strategy["platform"] == "select"
    assert strategy["state_topic"] == "mimir/input/strategy"
    assert strategy["command_topic"] == "mimir/input/strategy"
    assert set(strategy["options"]) == {"minimize_cost", "minimize_consumption", "balanced"}
    assert strategy.get("retain") is True, "retain must be True to survive broker restarts"
    assert "command_template" in strategy
    assert "value_template" in strategy


# ---------------------------------------------------------------------------
# Input sensors: no battery SOC duplication
# ---------------------------------------------------------------------------


def test_no_battery_soc_sensor_published() -> None:
    """No SOC sensor is published for batteries, even when inputs are configured.

    Battery SOC is already a native HA entity. Duplicating it via MQTT discovery
    would create a conflicting second entity on the same topic.
    """
    components = _publish_and_get_components(_make_config())
    soc_keys = [k for k in components if "soc_kwh" in k]
    assert soc_keys == [], f"Unexpected battery SOC component(s): {soc_keys}"


# ---------------------------------------------------------------------------
# Device setpoint state_topic prefix
# ---------------------------------------------------------------------------


def test_device_setpoint_topics_use_mqtt_prefix() -> None:
    """Per-device setpoint state_topics are {mqtt.topic_prefix}/device/{name}/setpoint."""
    components = _publish_and_get_components(_make_config())
    setpoint_state_topics = {
        v["state_topic"]
        for k, v in components.items()
        if "setpoint" in k
    }
    assert setpoint_state_topics == {
        "mimir/device/home_battery/setpoint",
        "mimir/device/roof_pv/setpoint",
        "mimir/device/base_load/setpoint",
    }


# ---------------------------------------------------------------------------
# Diagnostic entity_category
# ---------------------------------------------------------------------------


def test_solve_status_is_diagnostic() -> None:
    """The solve-status component must carry entity_category: diagnostic."""
    components = _publish_and_get_components(_make_config())
    status = next((v for k, v in components.items() if "_solve_status" in k), None)
    assert status is not None
    assert status.get("entity_category") == "diagnostic", (
        f"solve_status must have entity_category=diagnostic: {status}"
    )


def test_hybrid_inverter_pv_forecast_is_diagnostic() -> None:
    """The hybrid inverter PV forecast input sensor must be entity_category: diagnostic."""
    components = _publish_and_get_components(_make_config_new_devices())
    entity = next((v for k, v in components.items() if "hybrid1_input_forecast" in k), None)
    assert entity is not None
    assert entity.get("entity_category") == "diagnostic"


def test_sh_hp_forecast_inputs_are_diagnostic() -> None:
    """Space heating HP heat-needed and outdoor-temp-forecast sensors must be diagnostic."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimirheim", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "space_heating_hps": {
            "sh_hp": {
                "elec_power_kw": 4.0,
                "cop": 3.5,
                "inputs": {"topic_heat_needed_kwh": "mimir/input/sh_hp/heat_needed"},
                "building_thermal": {
                    "thermal_capacity_kwh_per_k": 5.0,
                    "heat_loss_coeff_kw_per_k": 0.3,
                    "inputs": {
                        "topic_current_indoor_temp_c": "mimir/input/sh_hp/indoor_temp",
                        "topic_outdoor_temp_forecast_c": "mimir/input/sh_hp/outdoor_forecast",
                    },
                },
            },
        },
    })
    components = _publish_and_get_components(config)
    heat = next((v for k, v in components.items() if "sh_hp_heat_needed_kwh" in k), None)
    assert heat is not None and heat.get("entity_category") == "diagnostic"
    outdoor = next((v for k, v in components.items() if "sh_hp_outdoor_temp_forecast" in k), None)
    assert outdoor is not None and outdoor.get("entity_category") == "diagnostic"


def test_combi_hp_forecast_inputs_are_diagnostic() -> None:
    """Combi HP SH-heat-needed and outdoor-temp-forecast must be diagnostic."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimirheim", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "combi_heat_pumps": {
            "combi_hp": {
                "elec_power_kw": 6.0,
                "cop_dhw": 2.5,
                "cop_sh": 3.5,
                "volume_liters": 200.0,
                "setpoint_c": 55.0,
                "cooling_rate_k_per_hour": 0.5,
                "inputs": {
                    "topic_current_temp": "mimir/input/combi_hp/temp",
                    "topic_heat_needed_kwh": "mimir/input/combi_hp/sh_demand",
                },
                "building_thermal": {
                    "thermal_capacity_kwh_per_k": 5.0,
                    "heat_loss_coeff_kw_per_k": 0.3,
                    "inputs": {
                        "topic_current_indoor_temp_c": "mimir/input/combi_hp/indoor_temp",
                        "topic_outdoor_temp_forecast_c": "mimir/input/combi_hp/outdoor_forecast",
                    },
                },
            },
        },
    })
    components = _publish_and_get_components(config)
    heat = next((v for k, v in components.items() if "combi_hp_heat_needed_kwh" in k), None)
    assert heat is not None and heat.get("entity_category") == "diagnostic"
    outdoor = next((v for k, v in components.items() if "combi_hp_outdoor_temp_forecast" in k), None)
    assert outdoor is not None and outdoor.get("entity_category") == "diagnostic"


# ---------------------------------------------------------------------------
# New device types (plans 24-27)
# ---------------------------------------------------------------------------


def test_new_device_types_have_setpoint_sensors() -> None:
    """Hybrid inverters, thermal boilers, space heating HPs, and combi HPs each
    receive a setpoint component in the components map."""
    components = _publish_and_get_components(_make_config_new_devices())
    for device_name in ("hybrid1", "dhw_boiler", "sh_hp", "combi_hp"):
        matching = {k: v for k, v in components.items() if f"_{device_name}_setpoint" in k}
        assert len(matching) == 1, (
            f"Expected 1 setpoint component for {device_name!r}, got {len(matching)}"
        )
        entity = next(iter(matching.values()))
        assert entity["state_topic"] == f"mimir/device/{device_name}/setpoint"


def test_hybrid_inverter_pv_forecast_sensor() -> None:
    """A forecast sensor is in components for each hybrid inverter's PV input topic."""
    components = _publish_and_get_components(_make_config_new_devices())
    matching = {k: v for k, v in components.items() if "hybrid1_input_forecast" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/input/hybrid1/pv_forecast"


def test_no_thermal_boiler_temperature_sensor_published() -> None:
    """No temperature sensor is published for thermal boilers.

    Tank temperature is already a native HA sensor entity.
    """
    components = _publish_and_get_components(_make_config_new_devices())
    assert not any("dhw_boiler_current_temp_c" in k for k in components)


def test_space_heating_heat_needed_sensor() -> None:
    """A heat-needed sensor is in components for each space heating HP with inputs."""
    components = _publish_and_get_components(_make_config_new_devices())
    matching = {k: v for k, v in components.items() if "sh_hp_heat_needed_kwh" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/input/sh_hp/heat_needed"
    assert entity["unit_of_measurement"] == "kWh"


def test_combi_hp_input_sensors() -> None:
    """Combi HP SH heat-needed is in components; DHW tank temp is not."""
    components = _publish_and_get_components(_make_config_new_devices())
    assert not any("combi_hp_current_temp_c" in k for k in components), (
        "DHW tank temp must not be published (native HA entity)"
    )
    matching = {k: v for k, v in components.items() if "combi_hp_heat_needed_kwh" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/input/combi_hp/sh_demand"
    assert entity["unit_of_measurement"] == "kWh"


# ---------------------------------------------------------------------------
# PV output topics (plan 18)
# ---------------------------------------------------------------------------


def test_pv_power_limit_sensor_published() -> None:
    """power_limit_kw sensor is in components for a PV array with that capability."""
    components = _publish_and_get_components(_make_config_pv_capabilities())
    matching = {k: v for k, v in components.items() if "roof_pv_power_limit_kw" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/roof_pv/power_limit"
    assert entity["unit_of_measurement"] == "kW"
    assert entity["device_class"] == "power"


def test_pv_zero_export_mode_binary_sensor_published() -> None:
    """zero_export_mode binary_sensor is in components for a PV array with that capability."""
    components = _publish_and_get_components(_make_config_pv_capabilities())
    matching = {k: v for k, v in components.items() if "roof_pv_zero_export_mode" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/roof_pv/zero_export"
    assert entity["payload_on"] == "true"
    assert entity["payload_off"] == "false"
    assert entity["platform"] == "binary_sensor"


def test_pv_no_output_sensors_when_capability_disabled() -> None:
    """No output sensors are published for a PV array without capabilities enabled."""
    components = _publish_and_get_components(_make_config())
    assert not any(
        x in k for k in components
        for x in ("power_limit_kw", "zero_export_mode", "on_off_mode")
    )


def test_pv_on_off_mode_binary_sensor_published() -> None:
    """on_off_mode binary_sensor is in components for a PV array with on_off capability."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "pv_arrays": {
            "roof_pv": {
                "max_power_kw": 6.0,
                "topic_forecast": "mimir/input/pv_forecast",
                "capabilities": {"on_off": True},
                "outputs": {"on_off_mode": "mimir/output/roof_pv/on_off_mode"},
            },
        },
    })
    components = _publish_and_get_components(config)
    matching = {k: v for k, v in components.items() if "roof_pv_on_off_mode" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/roof_pv/on_off_mode"
    assert entity["payload_on"] == "true"
    assert entity["payload_off"] == "false"
    assert entity["platform"] == "binary_sensor"


def test_pv_is_curtailed_binary_sensor_published_for_staged() -> None:
    """is_curtailed binary_sensor is published for a staged PV array."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "pv_arrays": {
            "roof_pv": {
                "max_power_kw": 4.5,
                "topic_forecast": "mimir/input/pv_forecast",
                "production_stages": [0.0, 1.5, 3.0, 4.5],
                "outputs": {"is_curtailed": "mimir/output/roof_pv/is_curtailed"},
            },
        },
    })
    components = _publish_and_get_components(config)
    matching = {k: v for k, v in components.items() if "roof_pv_is_curtailed" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/roof_pv/is_curtailed"
    assert entity["payload_on"] == "true"
    assert entity["payload_off"] == "false"
    assert entity["device_class"] == "problem"
    assert entity["platform"] == "binary_sensor"


def test_pv_is_curtailed_binary_sensor_published_for_power_limit() -> None:
    """is_curtailed binary_sensor is published for a power_limit PV array."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "pv_arrays": {
            "roof_pv": {
                "max_power_kw": 6.0,
                "topic_forecast": "mimir/input/pv_forecast",
                "capabilities": {"power_limit": True},
                "outputs": {"is_curtailed": "mimir/output/roof_pv/is_curtailed"},
            },
        },
    })
    components = _publish_and_get_components(config)
    matching = {k: v for k, v in components.items() if "roof_pv_is_curtailed" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/roof_pv/is_curtailed"
    assert entity["device_class"] == "problem"
    assert entity["platform"] == "binary_sensor"


def test_pv_is_curtailed_not_published_for_fixed_mode() -> None:
    """is_curtailed is not published for a fixed-mode PV array (no capabilities)."""
    components = _publish_and_get_components(_make_config())
    assert not any("is_curtailed" in k for k in components)


# ---------------------------------------------------------------------------
# Battery output topics: zero_exchange
# ---------------------------------------------------------------------------


def test_battery_zero_export_mode_binary_sensor_published() -> None:
    """exchange_mode binary_sensor is in components for a battery with zero_exchange."""
    components = _publish_and_get_components(_make_config_battery_zem())
    matching = {k: v for k, v in components.items() if "home_battery_exchange_mode" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["state_topic"] == "mimir/output/home_battery/exchange_mode"
    assert entity["payload_on"] == "true"
    assert entity["payload_off"] == "false"
    assert entity["device_class"] == "running"
    assert entity["platform"] == "binary_sensor"


def test_battery_zero_export_mode_not_published_when_capability_disabled() -> None:
    """No exchange_mode component when the zero_exchange capability is not enabled."""
    components = _publish_and_get_components(_make_config())
    assert not any("exchange_mode" in k for k in components)


# ---------------------------------------------------------------------------
# EV output topics
# ---------------------------------------------------------------------------


def test_ev_exchange_mode_and_loadbalance_in_components() -> None:
    """exchange_mode and loadbalance_cmd binary_sensor components are published
    for an EV charger with both capabilities enabled."""
    config = MimirheimConfig.model_validate({
        "mqtt": {"host": "localhost", "port": 1883, "client_id": "mimir-test", "topic_prefix": "mimir"},
        "outputs": {
            "schedule": "mimir/schedule", "current": "mimir/current",
            "last_solve": "mimir/status/last_solve", "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "ev_chargers": {
            "ev1": {
                "capacity_kwh": 75.0,
                "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.92}],
                "discharge_segments": [{"power_max_kw": 9.0, "efficiency": 0.90}],
                "capabilities": {"zero_exchange": True, "loadbalance": True, "v2h": True},
                "outputs": {
                    "exchange_mode": "mimir/output/ev1/exchange_mode",
                    "loadbalance_cmd": "mimir/output/ev1/loadbalance",
                },
            },
        },
    })
    components = _publish_and_get_components(config)
    em = next((v for k, v in components.items() if "ev1_exchange_mode" in k), None)
    lb = next((v for k, v in components.items() if "ev1_loadbalance_cmd" in k), None)
    assert em is not None and em["platform"] == "binary_sensor"
    assert lb is not None and lb["platform"] == "binary_sensor"


# ---------------------------------------------------------------------------
# Deferrable load window and start-time entities
# ---------------------------------------------------------------------------


def test_deferrable_recommended_start_sensor_published() -> None:
    """A timestamp sensor is in components for topic_recommended_start_time."""
    components = _publish_and_get_components(_make_config_with_deferrable_rec_start())
    matching = {k: v for k, v in components.items() if "wash_recommended_start" in k}
    assert len(matching) == 1
    entity = next(iter(matching.values()))
    assert entity["device_class"] == "timestamp"
    assert entity["state_topic"] == "mimir/load/wash/recommended_start"
    assert "entity_category" not in entity, (
        "recommended_start must not have entity_category so it appears in the "
        f"main section of the device card: {entity}"
    )


def test_deferrable_window_inputs_have_entity_category_config() -> None:
    """Window earliest, latest, and committed start text entities have entity_category: config."""
    config = MimirheimConfig.model_validate({
        "mqtt": {
            "host": "localhost",
            "port": 1883,
            "client_id": "mimir-test",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "homeassistant": {"enabled": True, "device_name": "Test mimirheim"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "deferrable_loads": {
            "wash": {
                "power_profile": [1.5, 1.5],
                "topic_window_earliest": "mimir/load/wash/window_earliest",
                "topic_window_latest": "mimir/load/wash/window_latest",
                "topic_committed_start_time": "mimir/load/wash/committed_start",
            }
        },
    })
    components = _publish_and_get_components(config)
    for suffix in ("window_earliest_input", "window_latest_input", "start_time_input"):
        matching = {k: v for k, v in components.items() if f"wash_{suffix}" in k}
        assert len(matching) == 1, f"Expected one component for {suffix}"
        entity = next(iter(matching.values()))
        assert entity.get("entity_category") == "config", (
            f"{suffix} must have entity_category=config: {entity}"
        )
