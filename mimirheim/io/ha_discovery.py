"""Home Assistant MQTT discovery publisher.

This module publishes a single MQTT discovery payload so that Home Assistant
automatically creates entities for all mimirheim outputs and control inputs without
manual HA YAML configuration.

The HA MQTT device JSON format is used (requires HA 2024.2+). A single
retained QoS-1 payload is published to:

    {discovery_prefix}/device/{device_id}/config

The payload describes the mimirheim device and all of its entities in one JSON
document. HA processes the ``components`` map and registers each entity under
the parent device. Device-level availability is used; every component inherits
it automatically without a per-component ``availability`` block.

Publishing an empty retained payload to the same topic removes the entire
device and all its entities from HA, which is simpler than the previous
per-entity format (no stale-topic cleanup needed).

HA MQTT device JSON discovery reference:
    https://www.home-assistant.io/integrations/mqtt/#discovery-messages

What this module does not do:

- It does not publish sensor state values. State comes from the normal mimirheim
  output topics (schedule, current, per-device setpoints).
- It does not manage entity lifecycle (deletion, renaming) beyond the single
  retained topic.
- It does not import from mimirheim.core.
"""

import json
import logging
from typing import Any

from mimirheim.config.schema import MimirheimConfig

logger = logging.getLogger("mimirheim.ha_discovery")


def publish_discovery(client: Any, config: MimirheimConfig) -> None:
    """Publish a single HA MQTT device JSON discovery payload for all mimirheim entities.

    This function is idempotent: calling it multiple times (e.g. on every
    reconnect) overwrites the same retained topic with the same payload,
    which is harmless and keeps HA in sync after a broker restart.

    The single retained payload is published to:

        {discovery_prefix}/device/{device_id}/config

    The ``components`` map inside the payload contains all mimirheim entities:
    grid power sensors, solve status, strategy selector, trigger button,
    per-device setpoints, input sensors where applicable, and output sensors
    for capabilities that are enabled.

    All availability tracking is at device level. No per-component
    ``availability`` key is included; HA applies device availability to every
    component automatically.

    This function is a no-op when ``config.homeassistant.enabled`` is False.

    Args:
        client: A paho-mqtt ``Client`` instance with an active connection.
            The function publishes one payload with ``qos=1, retain=True``.
        config: Static system configuration.
    """
    if not config.homeassistant.enabled:
        return

    ha = config.homeassistant
    disc_prefix = ha.discovery_prefix
    device_id = ha.device_id or config.mqtt.client_id
    mqtt_prefix = config.mqtt.topic_prefix

    # components accumulates every entity description keyed by its unique_id.
    # The _add() helper stamps platform and unique_id onto each entity dict
    # before inserting it, so callers do not need to set those fields.
    components: dict[str, dict[str, Any]] = {}

    def _add(unique_id: str, platform: str, entity: dict[str, Any]) -> None:
        # platform is required by the HA device JSON format to identify the
        # MQTT integration handler (sensor, binary_sensor, button, select,
        # text, etc.). unique_id links the component to its HA entity registry
        # entry, enabling renaming and customisation to survive re-discovery.
        entity["platform"] = platform
        entity["unique_id"] = unique_id
        components[unique_id] = entity

    # --- Grid sensors (sourced from the current-step summary topic) ---
    # No state_class: these are schedule outputs (forecasted power at step t=0),
    # not hardware measurements. state_class: "measurement" would cause HA to
    # accumulate long-term statistics for forecasted values, which is unwanted.

    _add(f"{device_id}_grid_import_kw", "sensor", {
        "name": "Grid Import Forecast",
        "state_topic": config.outputs.current,
        "value_template": "{{ value_json.grid_import_kw | round(2) }}",
        "unit_of_measurement": "kW",
        "device_class": "power",
        "entity_category": "diagnostic",
    })

    _add(f"{device_id}_grid_export_kw", "sensor", {
        "name": "Grid Export Forecast",
        "state_topic": config.outputs.current,
        "value_template": "{{ value_json.grid_export_kw | round(2) }}",
        "unit_of_measurement": "kW",
        "device_class": "power",
        "entity_category": "diagnostic",
    })

    _add(f"{device_id}_solve_status", "sensor", {
        "name": "Solve Status",
        "state_topic": config.outputs.last_solve,
        "value_template": "{{ value_json.status }}",
        "json_attributes_topic": config.outputs.last_solve,
        "entity_category": "diagnostic",
    })

    # --- Trigger Run button ---
    # An MQTT button entity that publishes an empty payload to the trigger
    # topic when pressed in the HA UI. This starts a new solve cycle
    # immediately without waiting for the next scheduled trigger.

    _add(f"{device_id}_trigger_run", "button", {
        "name": "Trigger Run",
        "command_topic": f"{mqtt_prefix}/input/trigger",
        "payload_press": "",
        "retain": False,
    })

    # --- Strategy selector ---
    # An MQTT select entity that both displays the active strategy and allows
    # changing it from the HA UI.
    #
    # state_topic and command_topic are the same input topic.
    # HA publishes the selection JSON-encoded via command_template; mimirheim's
    # parse_strategy reads it from the same topic each solve cycle.
    #
    # retain: True is required so that the broker holds the last selection.
    # Without it, a broker restart wipes the message: mimirheim silently falls
    # back to "minimize_cost" and HA shows "unknown" after reloading MQTT
    # because there is no retained message to seed the state_topic.
    strategy_topic = f"{mqtt_prefix}/input/strategy"
    _add(f"{device_id}_strategy", "select", {
        "name": "Strategy",
        "state_topic": strategy_topic,
        "command_topic": strategy_topic,
        "value_template": "{{ value_json.strategy }}",
        "command_template": '{"strategy": "{{ value }}"}',
        "options": ["minimize_cost", "minimize_consumption", "balanced"],
        "optimistic": False,
        "retain": True,
    })

    # --- Per-device setpoint sensors ---
    # No state_class: these values come from the solver schedule, not hardware.
    # The state_topic for each device is {prefix}/device/{name}/setpoint.

    all_device_names = [
        *config.batteries,
        *config.pv_arrays,
        *config.ev_chargers,
        *config.deferrable_loads,
        *config.static_loads,
        *config.hybrid_inverters,
        *config.thermal_boilers,
        *config.space_heating_hps,
        *config.combi_heat_pumps,
    ]

    for device_name in all_device_names:
        unique_id = f"{device_id}_{device_name}_setpoint_kw"
        _add(unique_id, "sensor", {
            "name": f"{device_name} setpoint",
            "state_topic": f"{mqtt_prefix}/device/{device_name}/setpoint",
            "value_template": "{{ value_json.kw | round(2) }}",
            "unit_of_measurement": "kW",
            "device_class": "power",
        })

    # --- Hybrid inverter input sensors ---
    # The SOC topic is a native HA battery entity; we do not duplicate it.
    # The PV forecast is not tracked by HA natively, so a diagnostic sensor
    # is published to make the raw value visible in the HA device card.

    for name, hi_cfg in config.hybrid_inverters.items():
        _add(f"{device_id}_{name}_input_forecast", "sensor", {
            "name": f"{name} PV forecast",
            "state_topic": hi_cfg.topic_pv_forecast,
            "entity_category": "diagnostic",
        })

    # --- Thermal boiler input sensors ---
    # The tank temperature is a native HA sensor entity; we do not duplicate it.

    # --- Space heating heat pump input sensors ---
    # The heat-needed value is an external model output (degree-days derived)
    # that HA does not track natively. The outdoor temperature forecast is
    # also not a native HA entity for this mimirheim device. Both are published as
    # diagnostic sensors for visibility.
    # The indoor temperature is already a native HA thermostat/sensor entity
    # and is not duplicated here.

    for name, sh_cfg in config.space_heating_hps.items():
        if sh_cfg.inputs is not None:
            _add(f"{device_id}_{name}_heat_needed_kwh", "sensor", {
                "name": f"{name} heat needed",
                "state_topic": sh_cfg.inputs.topic_heat_needed_kwh,
                "unit_of_measurement": "kWh",
                "entity_category": "diagnostic",
            })
        if sh_cfg.building_thermal is not None and sh_cfg.building_thermal.inputs is not None:
            _add(f"{device_id}_{name}_outdoor_temp_forecast", "sensor", {
                "name": f"{name} outdoor temperature forecast",
                "state_topic": sh_cfg.building_thermal.inputs.topic_outdoor_temp_forecast_c,
                "entity_category": "diagnostic",
            })

    # --- Combi heat pump input sensors ---
    # The DHW tank temperature and indoor temperature are native HA sensor
    # entities and are not duplicated here. The SH heat-needed value and the
    # outdoor temperature forecast are external model inputs that HA does not
    # track natively.

    for name, chp_cfg in config.combi_heat_pumps.items():
        if chp_cfg.inputs is not None:
            _add(f"{device_id}_{name}_heat_needed_kwh", "sensor", {
                "name": f"{name} SH heat needed",
                "state_topic": chp_cfg.inputs.topic_heat_needed_kwh,
                "unit_of_measurement": "kWh",
                "entity_category": "diagnostic",
            })
        if chp_cfg.building_thermal is not None and chp_cfg.building_thermal.inputs is not None:
            _add(f"{device_id}_{name}_outdoor_temp_forecast", "sensor", {
                "name": f"{name} outdoor temperature forecast",
                "state_topic": chp_cfg.building_thermal.inputs.topic_outdoor_temp_forecast_c,
                "entity_category": "diagnostic",
            })

    # --- Deferrable load window and start-time text entities ---
    # HA does not provide a datetime MQTT platform for MQTT discovery.
    # The correct supported platform for a settable ISO 8601 string is
    # "text". The user (or an automation) types an ISO 8601 UTC datetime
    # string into the HA UI; HA retains and publishes it to the
    # command_topic, which is the same topic mimirheim reads for that field.
    # parse_datetime() in input_parser.py accepts both offset-aware and
    # naive (UTC-assumed) strings, so any standard ISO 8601 value works.

    for name, dl_cfg in config.deferrable_loads.items():
        _add(f"{device_id}_{name}_window_earliest_input", "text", {
            "name": f"{name} window earliest",
            "state_topic": dl_cfg.topic_window_earliest,
            "command_topic": dl_cfg.topic_window_earliest,
            "retain": True,
            "entity_category": "config",
        })
        _add(f"{device_id}_{name}_window_latest_input", "text", {
            "name": f"{name} window latest",
            "state_topic": dl_cfg.topic_window_latest,
            "command_topic": dl_cfg.topic_window_latest,
            "retain": True,
            "entity_category": "config",
        })
        if dl_cfg.topic_committed_start_time is not None:
            _add(f"{device_id}_{name}_start_time_input", "text", {
                "name": f"{name} committed start time",
                "state_topic": dl_cfg.topic_committed_start_time,
                "command_topic": dl_cfg.topic_committed_start_time,
                "retain": True,
                "entity_category": "config",
            })
        if dl_cfg.topic_recommended_start_time is not None:
            _add(f"{device_id}_{name}_recommended_start", "sensor", {
                "name": f"{name} recommended start",
                "state_topic": dl_cfg.topic_recommended_start_time,
                "device_class": "timestamp",
            })

    # --- PV output sensors ---
    # These sensors mirror the control outputs mimirheim publishes to PV inverters:
    # the production power limit (kW), zero-export mode, and on/off mode.
    # They are solver outputs, not hardware measurements, so no state_class.

    for name, pv_cfg in config.pv_arrays.items():
        if pv_cfg.capabilities.power_limit and pv_cfg.outputs.power_limit_kw is not None:
            _add(f"{device_id}_{name}_power_limit_kw", "sensor", {
                "name": f"{name} power limit",
                "state_topic": pv_cfg.outputs.power_limit_kw,
                "unit_of_measurement": "kW",
                "device_class": "power",
            })
        if pv_cfg.capabilities.zero_export and pv_cfg.outputs.zero_export_mode is not None:
            _add(f"{device_id}_{name}_zero_export_mode", "binary_sensor", {
                "name": f"{name} zero export mode",
                "state_topic": pv_cfg.outputs.zero_export_mode,
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "running",
            })
        if pv_cfg.capabilities.on_off and pv_cfg.outputs.on_off_mode is not None:
            _add(f"{device_id}_{name}_on_off_mode", "binary_sensor", {
                "name": f"{name} on/off mode",
                "state_topic": pv_cfg.outputs.on_off_mode,
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "running",
            })

    # --- EV charger output sensors ---
    # These sensors mirror the control outputs mimirheim publishes to EV chargers:
    # exchange_mode (zero-exchange activation) and loadbalance_cmd.
    # They are solver outputs, not hardware measurements, so no state_class.

    for name, ev_cfg in config.ev_chargers.items():
        if ev_cfg.capabilities.zero_exchange and ev_cfg.outputs.exchange_mode is not None:
            _add(f"{device_id}_{name}_exchange_mode", "binary_sensor", {
                "name": f"{name} exchange mode",
                "state_topic": ev_cfg.outputs.exchange_mode,
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "running",
            })
        if ev_cfg.capabilities.loadbalance and ev_cfg.outputs.loadbalance_cmd is not None:
            _add(f"{device_id}_{name}_loadbalance_cmd", "binary_sensor", {
                "name": f"{name} load balance mode",
                "state_topic": ev_cfg.outputs.loadbalance_cmd,
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "running",
            })

    # --- Battery output sensors ---
    # These sensors mirror the control output mimirheim publishes to battery
    # inverters: the exchange_mode flag for zero-exchange operation.
    # It is a solver output, not a hardware measurement, so no state_class.

    for name, bat_cfg in config.batteries.items():
        if bat_cfg.capabilities.zero_exchange and bat_cfg.outputs.exchange_mode is not None:
            _add(f"{device_id}_{name}_exchange_mode", "binary_sensor", {
                "name": f"{name} exchange mode",
                "state_topic": bat_cfg.outputs.exchange_mode,
                "payload_on": "true",
                "payload_off": "false",
                "device_class": "running",
            })

    payload: dict[str, Any] = {
        "device": {
            "identifiers": [device_id],
            "name": ha.device_name,
            "manufacturer": "Mimirheim",
        },
        "origin": {"name": "Mimirheim"},
        "availability": {
            "topic": config.outputs.availability,
            "payload_available": "online",
            "payload_not_available": "offline",
        },
        "components": components,
    }

    topic = f"{disc_prefix}/device/{device_id}/config"
    client.publish(topic, json.dumps(payload), qos=1, retain=True)
    logger.info(
        "Published HA device JSON discovery to %s with %d component(s).",
        topic,
        len(components),
    )
