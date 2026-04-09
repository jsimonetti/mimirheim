"""Canonical MQTT topic naming functions for mimirheim and its helper daemons.

This module is the single source of truth for every MQTT topic path used in
the mimirheim ecosystem. All topic strings are pure functions of ``prefix`` (the
``mqtt.topic_prefix`` value, default ``"mimir"``) and, for device-specific
topics, the device name as it appears in the mimirheim config.

Both mimirheim itself and the helper daemons in ``mimirheim_helpers/`` import from this
module. Using these functions instead of inline f-strings guarantees that a
rename of any topic segment is a one-line change here, not a search-and-replace
across multiple codebases.

What this module does not do:
- It does not import from mimirheim, any helper tool, or paho.
- It does not perform any I/O.
- It does not define Pydantic models or any stateful objects.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Global (non-device) topics
# ---------------------------------------------------------------------------


def prices_topic(prefix: str) -> str:
    """Return the topic on which per-step price data is published.

    Corresponds to ``inputs.prices`` in the mimirheim configuration.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/input/prices"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/input/prices"


def trigger_topic(prefix: str) -> str:
    """Return the topic that triggers a mimirheim solve cycle.

    Publishing any message to this topic causes mimirheim to attempt a solve
    (if all readiness conditions are met). This topic is not configurable
    in mimirheim; it is always derived from the prefix.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/input/trigger"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/input/trigger"


def strategy_topic(prefix: str) -> str:
    """Return the topic for runtime strategy selection.

    mimirheim subscribes here. Publish one of ``"minimize_cost"``,
    ``"maximize_self_sufficiency"``, or ``"balanced"`` to change the active
    optimisation strategy without restarting.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/input/strategy"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/input/strategy"


def schedule_topic(prefix: str) -> str:
    """Return the topic on which the full horizon schedule is published.

    Corresponds to ``outputs.schedule`` in the mimirheim configuration.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/strategy/schedule"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/strategy/schedule"


def current_topic(prefix: str) -> str:
    """Return the topic on which the current-step strategy summary is published.

    Corresponds to ``outputs.current`` in the mimirheim configuration.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/strategy/current"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/strategy/current"


def last_solve_topic(prefix: str) -> str:
    """Return the topic on which the retained solve-status message is published.

    Corresponds to ``outputs.last_solve`` in the mimirheim configuration.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/status/last_solve"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/status/last_solve"


def availability_topic(prefix: str) -> str:
    """Return the topic for birth (``"online"``) and last-will (``"offline"``) messages.

    Corresponds to ``outputs.availability`` in the mimirheim configuration.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/status/availability"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/status/availability"


def dump_available_topic(prefix: str) -> str:
    """Return the topic on which mimirheim publishes dump-available notifications.

    Corresponds to ``reporting.notify_topic`` in the mimirheim configuration.
    mimirheim-reporter subscribes to this topic to learn about new report dumps.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.

    Returns:
        e.g. ``"mimir/status/dump_available"`` when prefix is ``"mimir"``.
    """
    return f"{prefix}/status/dump_available"


# ---------------------------------------------------------------------------
# Device input topics
# ---------------------------------------------------------------------------


def battery_soc_topic(prefix: str, name: str) -> str:
    """Return the topic for battery state-of-charge readings.

    Corresponds to ``batteries.{name}.inputs.soc.topic``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The battery device name as it appears in ``batteries:`` in mimirheim config.

    Returns:
        e.g. ``"mimir/input/battery/home_battery/soc"``.
    """
    return f"{prefix}/input/battery/{name}/soc"


def ev_soc_topic(prefix: str, name: str) -> str:
    """Return the topic for EV state-of-charge readings.

    Corresponds to ``ev_chargers.{name}.inputs.soc.topic``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The EV charger device name as it appears in ``ev_chargers:`` in mimirheim config.

    Returns:
        e.g. ``"mimir/input/ev/ev_charger/soc"``.
    """
    return f"{prefix}/input/ev/{name}/soc"


def ev_plugged_in_topic(prefix: str, name: str) -> str:
    """Return the topic for EV plug-state readings.

    Corresponds to ``ev_chargers.{name}.inputs.plugged_in_topic``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The EV charger device name.

    Returns:
        e.g. ``"mimir/input/ev/ev_charger/plugged_in"``.
    """
    return f"{prefix}/input/ev/{name}/plugged_in"


def hybrid_soc_topic(prefix: str, name: str) -> str:
    """Return the topic for hybrid inverter battery SOC readings.

    Corresponds to ``hybrid_inverters.{name}.inputs.soc.topic``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The hybrid inverter device name.

    Returns:
        e.g. ``"mimir/input/hybrid/hybrid_main/soc"``.
    """
    return f"{prefix}/input/hybrid/{name}/soc"


def hybrid_pv_forecast_topic(prefix: str, name: str) -> str:
    """Return the topic for the hybrid inverter PV forecast.

    Corresponds to ``hybrid_inverters.{name}.topic_pv_forecast``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The hybrid inverter device name.

    Returns:
        e.g. ``"mimir/input/hybrid/hybrid_main/pv_forecast"``.
    """
    return f"{prefix}/input/hybrid/{name}/pv_forecast"


def pv_forecast_topic(prefix: str, name: str) -> str:
    """Return the topic for a standalone PV array forecast.

    Corresponds to ``pv_arrays.{name}.topic_forecast``.

    This is the topic that PV forecast helper tools (forecast.solar fetcher,
    pv_ml_learner) should publish to by default.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The PV array device name as it appears in ``pv_arrays:`` in mimirheim config.

    Returns:
        e.g. ``"mimir/input/pv/roof_pv/forecast"``.
    """
    return f"{prefix}/input/pv/{name}/forecast"


def baseload_forecast_topic(prefix: str, name: str) -> str:
    """Return the topic for a static (base) load forecast.

    Corresponds to ``static_loads.{name}.topic_forecast``.

    This is the topic that baseload helper tools should publish to by default.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The static load device name as it appears in ``static_loads:`` in mimirheim config.

    Returns:
        e.g. ``"mimir/input/baseload/base_load/forecast"``.
    """
    return f"{prefix}/input/baseload/{name}/forecast"


def deferrable_window_earliest_topic(prefix: str, name: str) -> str:
    """Return the topic publishing the earliest permitted start for a deferrable load.

    Corresponds to ``deferrable_loads.{name}.topic_window_earliest``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The deferrable load device name.

    Returns:
        e.g. ``"mimir/input/deferrable/washing_machine/window_earliest"``.
    """
    return f"{prefix}/input/deferrable/{name}/window_earliest"


def deferrable_window_latest_topic(prefix: str, name: str) -> str:
    """Return the topic publishing the latest permitted end for a deferrable load.

    Corresponds to ``deferrable_loads.{name}.topic_window_latest``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The deferrable load device name.

    Returns:
        e.g. ``"mimir/input/deferrable/washing_machine/window_latest"``.
    """
    return f"{prefix}/input/deferrable/{name}/window_latest"


def deferrable_committed_start_topic(prefix: str, name: str) -> str:
    """Return the topic where the automation publishes the actual start time.

    Corresponds to ``deferrable_loads.{name}.topic_committed_start_time``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The deferrable load device name.

    Returns:
        e.g. ``"mimir/input/deferrable/washing_machine/committed_start"``.
    """
    return f"{prefix}/input/deferrable/{name}/committed_start"


def thermal_boiler_temp_topic(prefix: str, name: str) -> str:
    """Return the topic for the current thermal boiler tank temperature.

    Corresponds to ``thermal_boilers.{name}.inputs.topic_current_temp``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The thermal boiler device name.

    Returns:
        e.g. ``"mimir/input/thermal_boiler/dhw_boiler/temp_c"``.
    """
    return f"{prefix}/input/thermal_boiler/{name}/temp_c"


def space_heating_heat_needed_topic(prefix: str, name: str) -> str:
    """Return the topic for the space heating HP heat demand.

    Corresponds to ``space_heating_hps.{name}.inputs.topic_heat_needed_kwh``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The space heating HP device name.

    Returns:
        e.g. ``"mimir/input/space_heating/sh_hp/heat_needed_kwh"``.
    """
    return f"{prefix}/input/space_heating/{name}/heat_needed_kwh"


def space_heating_heat_produced_topic(prefix: str, name: str) -> str:
    """Return the informational topic for heat produced today by a space heating HP.

    Corresponds to ``space_heating_hps.{name}.inputs.topic_heat_produced_today_kwh``.
    This topic is not read by the solver; it carries informational data only.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The space heating HP device name.

    Returns:
        e.g. ``"mimir/input/space_heating/sh_hp/heat_produced_today_kwh"``.
    """
    return f"{prefix}/input/space_heating/{name}/heat_produced_today_kwh"


def space_heating_btm_indoor_topic(prefix: str, name: str) -> str:
    """Return the topic for the current indoor temperature of a space heating BTM.

    Corresponds to
    ``space_heating_hps.{name}.building_thermal.inputs.topic_current_indoor_temp_c``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The space heating HP device name.

    Returns:
        e.g. ``"mimir/input/space_heating/sh_hp/btm/indoor_temp_c"``.
    """
    return f"{prefix}/input/space_heating/{name}/btm/indoor_temp_c"


def space_heating_btm_outdoor_topic(prefix: str, name: str) -> str:
    """Return the topic for the outdoor temperature forecast of a space heating BTM.

    Corresponds to
    ``space_heating_hps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The space heating HP device name.

    Returns:
        e.g. ``"mimir/input/space_heating/sh_hp/btm/outdoor_forecast_c"``.
    """
    return f"{prefix}/input/space_heating/{name}/btm/outdoor_forecast_c"


def combi_hp_temp_topic(prefix: str, name: str) -> str:
    """Return the topic for the current DHW tank temperature of a combi heat pump.

    Corresponds to ``combi_heat_pumps.{name}.inputs.topic_current_temp``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The combi heat pump device name.

    Returns:
        e.g. ``"mimir/input/combi_hp/combi_hp/temp_c"``.
    """
    return f"{prefix}/input/combi_hp/{name}/temp_c"


def combi_hp_heat_needed_topic(prefix: str, name: str) -> str:
    """Return the topic for the SH heat demand of a combi heat pump.

    Corresponds to ``combi_heat_pumps.{name}.inputs.topic_heat_needed_kwh``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The combi heat pump device name.

    Returns:
        e.g. ``"mimir/input/combi_hp/combi_hp/sh_heat_needed_kwh"``.
    """
    return f"{prefix}/input/combi_hp/{name}/sh_heat_needed_kwh"


def combi_hp_btm_indoor_topic(prefix: str, name: str) -> str:
    """Return the topic for the current indoor temperature of a combi HP BTM.

    Corresponds to
    ``combi_heat_pumps.{name}.building_thermal.inputs.topic_current_indoor_temp_c``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The combi heat pump device name.

    Returns:
        e.g. ``"mimir/input/combi_hp/combi_hp/btm/indoor_temp_c"``.
    """
    return f"{prefix}/input/combi_hp/{name}/btm/indoor_temp_c"


def combi_hp_btm_outdoor_topic(prefix: str, name: str) -> str:
    """Return the topic for the outdoor temperature forecast of a combi HP BTM.

    Corresponds to
    ``combi_heat_pumps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The combi heat pump device name.

    Returns:
        e.g. ``"mimir/input/combi_hp/combi_hp/btm/outdoor_forecast_c"``.
    """
    return f"{prefix}/input/combi_hp/{name}/btm/outdoor_forecast_c"


# ---------------------------------------------------------------------------
# Device output topics
# ---------------------------------------------------------------------------


def battery_exchange_mode_topic(prefix: str, name: str) -> str:
    """Return the topic for the battery zero-exchange mode command.

    Corresponds to ``batteries.{name}.outputs.exchange_mode``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The battery device name.

    Returns:
        e.g. ``"mimir/output/battery/home_battery/exchange_mode"``.
    """
    return f"{prefix}/output/battery/{name}/exchange_mode"


def ev_exchange_mode_topic(prefix: str, name: str) -> str:
    """Return the topic for the EV charger zero-exchange mode command.

    Corresponds to ``ev_chargers.{name}.outputs.exchange_mode``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The EV charger device name.

    Returns:
        e.g. ``"mimir/output/ev/ev_charger/exchange_mode"``.
    """
    return f"{prefix}/output/ev/{name}/exchange_mode"


def ev_loadbalance_topic(prefix: str, name: str) -> str:
    """Return the topic for the EV charger load-balance mode command.

    Corresponds to ``ev_chargers.{name}.outputs.loadbalance_cmd``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The EV charger device name.

    Returns:
        e.g. ``"mimir/output/ev/ev_charger/loadbalance"``.
    """
    return f"{prefix}/output/ev/{name}/loadbalance"


def pv_power_limit_topic(prefix: str, name: str) -> str:
    """Return the topic for the PV array production limit setpoint.

    Corresponds to ``pv_arrays.{name}.outputs.power_limit_kw``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The PV array device name.

    Returns:
        e.g. ``"mimir/output/pv/roof_pv/power_limit_kw"``.
    """
    return f"{prefix}/output/pv/{name}/power_limit_kw"


def pv_zero_export_topic(prefix: str, name: str) -> str:
    """Return the topic for the PV array zero-export mode command.

    Corresponds to ``pv_arrays.{name}.outputs.zero_export_mode``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The PV array device name.

    Returns:
        e.g. ``"mimir/output/pv/roof_pv/zero_export_mode"``.
    """
    return f"{prefix}/output/pv/{name}/zero_export_mode"


def pv_on_off_topic(prefix: str, name: str) -> str:
    """Return the topic for the PV array on/off command.

    Corresponds to ``pv_arrays.{name}.outputs.on_off_mode``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The PV array device name.

    Returns:
        e.g. ``"mimir/output/pv/roof_pv/on_off_mode"``.
    """
    return f"{prefix}/output/pv/{name}/on_off_mode"


def deferrable_recommended_start_topic(prefix: str, name: str) -> str:
    """Return the topic where mimirheim publishes the recommended start time.

    Corresponds to ``deferrable_loads.{name}.topic_recommended_start_time``.

    Args:
        prefix: The ``mqtt.topic_prefix`` value from mimirheim config.
        name: The deferrable load device name.

    Returns:
        e.g. ``"mimir/output/deferrable/washing_machine/recommended_start"``.
    """
    return f"{prefix}/output/deferrable/{name}/recommended_start"
