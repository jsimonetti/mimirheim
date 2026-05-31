"""HA MQTT discovery publisher for mimirheim input helper daemons.

Each helper tool is represented in Home Assistant as a single device with one
button entity. Pressing the button publishes an empty retained message to the
tool's trigger topic, causing the daemon to run a fetch cycle immediately.

When ``stats_topic`` is also configured, four additional diagnostic sensor
entities are published under the same HA device: last run timestamp, run
duration, horizon length, and exit message.

Stale discovery topics (e.g. sensors from a previous config where stats_topic
was set but has since been removed) are deleted unconditionally on every call
to ``publish_trigger_discovery``. Because the full set of topics a given
``tool_name`` can ever occupy is statically known, no broker query is required.

This module has no imports from any specific helper tool.
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Stable identifiers for the four stats sensor fields. These determine both the
# MQTT discovery topic object_id suffix and the key used in the stats JSON
# payload value_template.
_STATS_SENSOR_IDS = (
    "last_run_ts",
    "duration_s",
    "horizon_hours",
    "exit_message",
)


def _all_possible_helper_discovery_topics(
    *,
    tool_name: str,
    discovery_prefix: str = "homeassistant",
) -> set[str]:
    """Return every HA discovery topic this tool could ever publish.

    This is the full static set: one button plus one sensor per stats field.
    It is independent of the current config; it represents every topic this
    ``tool_name`` could occupy on the broker under any configuration.

    Used by ``publish_trigger_discovery()`` to compute the deletion set:
    ``_all_possible_... - _active_...`` gives every topic that may be stale
    and should be erased. Deleting a topic with no retained message on the
    broker is a broker no-op, so the operation is unconditionally safe.

    Must be kept in sync with the publish calls in
    ``publish_trigger_discovery()``.  The test
    ``test_all_possible_discovery_topics_matches_publish_and_delete_targets``
    enforces this.

    Args:
        tool_name: Stable snake_case identifier for the tool.
        discovery_prefix: HA MQTT discovery topic prefix.

    Returns:
        Set of all MQTT topic strings this tool name could ever produce.
    """
    topics: set[str] = {f"{discovery_prefix}/button/{tool_name}/config"}
    for sensor_id in _STATS_SENSOR_IDS:
        topics.add(f"{discovery_prefix}/sensor/{tool_name}_{sensor_id}/config")
    topics.add(f"{discovery_prefix}/sensor/{tool_name}_forecast/config")
    return topics


def _active_helper_discovery_topics(
    *,
    tool_name: str,
    stats_topic: str | None,
    forecast_sensor: bool = False,
    trigger_button: bool = True,
    discovery_prefix: str = "homeassistant",
) -> set[str]:
    """Return the HA discovery topics that should exist given the current config.

    Args:
        tool_name: Stable snake_case identifier for the tool.
        stats_topic: When not None, stats sensor topics are included.
        forecast_sensor: When True, the forecast sensor topic is included.
        trigger_button: When False, the button topic is excluded from the
            active set and will be deleted if it exists on the broker.
            Set to False for forecast-only registrations (e.g. per-array
            forecast sensors that share a device with a separate trigger).
        discovery_prefix: HA MQTT discovery topic prefix.

    Returns:
        Set of MQTT topic strings that should currently be retained on the broker.
    """
    topics: set[str] = set()
    if trigger_button:
        topics.add(f"{discovery_prefix}/button/{tool_name}/config")
    if stats_topic is not None:
        for sensor_id in _STATS_SENSOR_IDS:
            topics.add(f"{discovery_prefix}/sensor/{tool_name}_{sensor_id}/config")
    if forecast_sensor:
        topics.add(f"{discovery_prefix}/sensor/{tool_name}_forecast/config")
    return topics


def publish_trigger_discovery(
    client: Any,
    *,
    tool_name: str,
    tool_label: str,
    trigger_topic: str | None = None,
    stats_topic: str | None = None,
    forecast_sensor: bool = False,
    output_topic: str | None = None,
    forecast_value_template: str = "{{ value_json[0].kw | default(0) | round(3) }}",
    forecast_unit: str = "kW",
    forecast_device_class: str | None = "power",
    device_id: str | None = None,
    device_label: str | None = None,
    discovery_prefix: str = "homeassistant",
) -> None:
    """Refresh HA MQTT discovery for this helper tool.

    Unconditionally deletes every topic in the full possible set that is not
    in the active set, then publishes the active set. Uses only the supplied
    client; no secondary connection or sleep is required.

    Deletion is idempotent: publishing an empty retained payload to a topic
    that holds no retained message is a broker no-op. This means this function
    can be called on every reconnect without risk of side effects.

    Args:
        client: A connected paho-mqtt ``Client`` instance.
        tool_name: Stable snake_case identifier used as the HA ``object_id``
            and device ``unique_id`` prefix. Use underscores, no spaces.
            Example: ``"nordpool_prices"``.
        tool_label: Human-readable display name shown in the HA UI.
            Example: ``"Nordpool Prices"``.
        trigger_topic: The MQTT topic that triggers the daemon. When provided,
            a button entity is published with this as its ``command_topic``.
            When ``None``, no button entity is published. Pass ``None`` for
            forecast-only registrations that share a device with a separate
            trigger button (e.g. per-array PV forecast sensors grouped under
            the pv_ml_learner device alongside the train/infer buttons).
        stats_topic: MQTT topic where the daemon publishes per-cycle stats
            JSON. When not None, four diagnostic sensor entities are published
            under the same HA device: last run timestamp, duration, horizon
            length, and exit message.  When None, any previously published
            sensor topics are erased from the broker.
        forecast_sensor: When True and ``output_topic`` is provided, a
            diagnostic sensor entity is published that reads the first element
            of the helper's forecast JSON array. Defaults to False.
        output_topic: The MQTT topic where the helper publishes its forecast
            payload. Used as both ``state_topic`` and
            ``json_attributes_topic`` for the forecast sensor. Required when
            ``forecast_sensor`` is True; ignored otherwise.
        forecast_value_template: Jinja2 value_template for the forecast sensor
            state. Defaults to the first element ``kw`` field (power helpers).
        forecast_unit: ``unit_of_measurement`` for the forecast sensor.
            Defaults to ``"kW"``.
        forecast_device_class: HA ``device_class`` for the forecast sensor.
            Pass ``None`` to omit the field (e.g. for price sensors).
            Defaults to ``"power"``.
        device_id: When supplied, used as the HA device ``identifiers`` value
            instead of ``tool_name``. Allows multiple
            ``publish_trigger_discovery()`` calls to group their entities under
            one HA device card. Entity topic paths continue to use
            ``tool_name`` regardless. When ``None`` (default), falls back to
            ``tool_name`` — preserving the existing behaviour for all
            single-button helpers.
        device_label: Display name for the shared HA device when ``device_id``
            is supplied. Falls back to ``tool_label`` when not provided.
        discovery_prefix: HA MQTT discovery topic prefix. Default:
            ``"homeassistant"``.
    """
    _forecast_active = forecast_sensor and output_topic is not None
    _trigger_button = trigger_topic is not None
    possible = _all_possible_helper_discovery_topics(
        tool_name=tool_name,
        discovery_prefix=discovery_prefix,
    )
    active = _active_helper_discovery_topics(
        tool_name=tool_name,
        stats_topic=stats_topic,
        forecast_sensor=_forecast_active,
        trigger_button=_trigger_button,
        discovery_prefix=discovery_prefix,
    )

    # Delete every possible topic that is not in the current active set.
    # This removes sensors that were present in a previous config but have
    # since been disabled (e.g. stats_topic removed from YAML).
    for stale_topic in sorted(possible - active):
        client.publish(stale_topic, None, qos=1, retain=True)
        logger.debug("Deleted stale HA discovery topic: %s", stale_topic)

    device_block: dict[str, Any] = {
        "identifiers": [device_id if device_id is not None else tool_name],
        "name": device_label if device_label is not None else tool_label,
        "manufacturer": "Mimirheim",
    }

    # --- Trigger button (only when trigger_topic is provided) ---
    # A forecast-only registration (trigger_topic=None) is used when an entity
    # should appear on an existing shared device without adding a new button.
    if _trigger_button:
        client.publish(
            f"{discovery_prefix}/button/{tool_name}/config",
            json.dumps({
                "name": f"{tool_label} Trigger",
                "unique_id": f"{tool_name}_trigger",
                "command_topic": trigger_topic,
                "payload_press": "",
                "retain": False,
                "device": device_block,
            }),
            qos=1,
            retain=True,
        )
        logger.debug("Published HA discovery for button/%s", tool_name)

    # --- Stats sensors (only when stats_topic is configured) ---
    # These four sensors expose the per-cycle statistics payload as readable
    # HA entities. entity_category "diagnostic" keeps them out of the default
    # HA dashboard summary card.
    if stats_topic is not None:
        _STATS_SENSORS: list[tuple[str, str, str, str | None, str | None]] = [
            (
                f"{tool_name}_last_run_ts",
                "Last Run",
                "{{ value_json.ts }}",
                None,
                None,
            ),
            (
                f"{tool_name}_duration_s",
                "Last Run Duration",
                "{{ value_json.duration_s | round(2) }}",
                "s",
                None,
            ),
            (
                f"{tool_name}_horizon_hours",
                "Horizon",
                "{{ value_json.horizon_hours }}",
                "h",
                None,
            ),
            (
                f"{tool_name}_exit_message",
                "Exit Message",
                "{{ value_json.exit_message }}",
                None,
                None,
            ),
        ]
        for sensor_id, name, template, unit, device_class in _STATS_SENSORS:
            sensor_payload: dict[str, Any] = {
                "name": name,
                "unique_id": sensor_id,
                "state_topic": stats_topic,
                "value_template": template,
                "entity_category": "diagnostic",
                "device": device_block,
            }
            if unit is not None:
                sensor_payload["unit_of_measurement"] = unit
            if device_class is not None:
                sensor_payload["device_class"] = device_class
            client.publish(
                f"{discovery_prefix}/sensor/{sensor_id}/config",
                json.dumps(sensor_payload),
                qos=1,
                retain=True,
            )
            logger.debug("Published HA discovery for sensor/%s", sensor_id)

    # --- Forecast sensor (only when enabled and output_topic is provided) ---
    if _forecast_active:
        forecast_sensor_id = f"{tool_name}_forecast"
        forecast_payload: dict[str, Any] = {
            "name": f"{tool_label} Forecast",
            "unique_id": forecast_sensor_id,
            "state_topic": output_topic,
            "json_attributes_topic": output_topic,
            # HA requires json_attributes_topic to deliver a JSON object (not an
            # array). The helper output payload is a JSON array, so this template
            # wraps the entire array under the key "forecast". Consumers can then
            # reference "forecast" as a JSON attribute path in apexcharts-card or
            # HA templates.
            "json_attributes_template": '{{ {"forecast": value_json} | tojson }}',
            "value_template": forecast_value_template,
            "unit_of_measurement": forecast_unit,
            "entity_category": "diagnostic",
            "enabled_by_default": True,
            "device": device_block,
        }
        if forecast_device_class is not None:
            forecast_payload["device_class"] = forecast_device_class
        client.publish(
            f"{discovery_prefix}/sensor/{forecast_sensor_id}/config",
            json.dumps(forecast_payload),
            qos=1,
            retain=True,
        )
        logger.debug("Published HA discovery for sensor/%s", forecast_sensor_id)

