"""Unit tests for helper_common.topics — canonical MQTT topic functions.

Each test verifies one function using the default prefix ``"mimirheim"`` and, where
the function takes a device name, a representative name. A secondary test
asserts that a non-default prefix is respected, confirming there is no
hardcoded string.
"""

from __future__ import annotations

import pytest
import helper_common.topics as topics


# ---------------------------------------------------------------------------
# Global topics
# ---------------------------------------------------------------------------


def test_prices_topic_default_prefix() -> None:
    assert topics.prices_topic("mimir") == "mimir/input/prices"


def test_prices_topic_custom_prefix() -> None:
    assert topics.prices_topic("mymimir") == "mymimir/input/prices"


def test_trigger_topic_default_prefix() -> None:
    assert topics.trigger_topic("mimir") == "mimir/input/trigger"


def test_trigger_topic_custom_prefix() -> None:
    assert topics.trigger_topic("mymimir") == "mymimir/input/trigger"


def test_strategy_topic_default_prefix() -> None:
    assert topics.strategy_topic("mimir") == "mimir/input/strategy"


def test_schedule_topic_default_prefix() -> None:
    assert topics.schedule_topic("mimir") == "mimir/strategy/schedule"


def test_current_topic_default_prefix() -> None:
    assert topics.current_topic("mimir") == "mimir/strategy/current"


def test_last_solve_topic_default_prefix() -> None:
    assert topics.last_solve_topic("mimir") == "mimir/status/last_solve"


def test_availability_topic_default_prefix() -> None:
    assert topics.availability_topic("mimir") == "mimir/status/availability"


def test_dump_available_topic_default_prefix() -> None:
    assert topics.dump_available_topic("mimir") == "mimir/status/dump_available"


def test_dump_available_topic_custom_prefix() -> None:
    assert topics.dump_available_topic("mymimir") == "mymimir/status/dump_available"


# ---------------------------------------------------------------------------
# Device input topics
# ---------------------------------------------------------------------------


def test_battery_soc_topic() -> None:
    assert topics.battery_soc_topic("mimir", "home_battery") == "mimir/input/battery/home_battery/soc"


def test_ev_soc_topic() -> None:
    assert topics.ev_soc_topic("mimir", "ev_charger") == "mimir/input/ev/ev_charger/soc"


def test_ev_plugged_in_topic() -> None:
    assert topics.ev_plugged_in_topic("mimir", "ev_charger") == "mimir/input/ev/ev_charger/plugged_in"


def test_hybrid_soc_topic() -> None:
    assert topics.hybrid_soc_topic("mimir", "hybrid_main") == "mimir/input/hybrid/hybrid_main/soc"


def test_hybrid_pv_forecast_topic() -> None:
    assert topics.hybrid_pv_forecast_topic("mimir", "hybrid_main") == "mimir/input/hybrid/hybrid_main/pv_forecast"


def test_pv_forecast_topic() -> None:
    assert topics.pv_forecast_topic("mimir", "roof_pv") == "mimir/input/pv/roof_pv/forecast"


def test_pv_forecast_topic_custom_prefix() -> None:
    assert topics.pv_forecast_topic("mymimir", "roof_pv") == "mymimir/input/pv/roof_pv/forecast"


def test_baseload_forecast_topic() -> None:
    assert topics.baseload_forecast_topic("mimir", "base_load") == "mimir/input/baseload/base_load/forecast"


def test_baseload_forecast_topic_custom_prefix() -> None:
    assert topics.baseload_forecast_topic("mymimir", "base_load") == "mymimir/input/baseload/base_load/forecast"


def test_deferrable_window_earliest_topic() -> None:
    assert (
        topics.deferrable_window_earliest_topic("mimir", "washing_machine")
        == "mimir/input/deferrable/washing_machine/window_earliest"
    )


def test_deferrable_window_latest_topic() -> None:
    assert (
        topics.deferrable_window_latest_topic("mimir", "washing_machine")
        == "mimir/input/deferrable/washing_machine/window_latest"
    )


def test_deferrable_committed_start_topic() -> None:
    assert (
        topics.deferrable_committed_start_topic("mimir", "washing_machine")
        == "mimir/input/deferrable/washing_machine/committed_start"
    )


def test_thermal_boiler_temp_topic() -> None:
    assert topics.thermal_boiler_temp_topic("mimir", "dhw_boiler") == "mimir/input/thermal_boiler/dhw_boiler/temp_c"


def test_space_heating_heat_needed_topic() -> None:
    assert (
        topics.space_heating_heat_needed_topic("mimir", "sh_hp")
        == "mimir/input/space_heating/sh_hp/heat_needed_kwh"
    )


def test_space_heating_heat_produced_topic() -> None:
    assert (
        topics.space_heating_heat_produced_topic("mimir", "sh_hp")
        == "mimir/input/space_heating/sh_hp/heat_produced_today_kwh"
    )


def test_space_heating_btm_indoor_topic() -> None:
    assert (
        topics.space_heating_btm_indoor_topic("mimir", "sh_hp")
        == "mimir/input/space_heating/sh_hp/btm/indoor_temp_c"
    )


def test_space_heating_btm_outdoor_topic() -> None:
    assert (
        topics.space_heating_btm_outdoor_topic("mimir", "sh_hp")
        == "mimir/input/space_heating/sh_hp/btm/outdoor_forecast_c"
    )


def test_combi_hp_temp_topic() -> None:
    assert topics.combi_hp_temp_topic("mimir", "combi") == "mimir/input/combi_hp/combi/temp_c"


def test_combi_hp_heat_needed_topic() -> None:
    assert topics.combi_hp_heat_needed_topic("mimir", "combi") == "mimir/input/combi_hp/combi/sh_heat_needed_kwh"


def test_combi_hp_btm_indoor_topic() -> None:
    assert (
        topics.combi_hp_btm_indoor_topic("mimir", "combi")
        == "mimir/input/combi_hp/combi/btm/indoor_temp_c"
    )


def test_combi_hp_btm_outdoor_topic() -> None:
    assert (
        topics.combi_hp_btm_outdoor_topic("mimir", "combi")
        == "mimir/input/combi_hp/combi/btm/outdoor_forecast_c"
    )


# ---------------------------------------------------------------------------
# Device output topics
# ---------------------------------------------------------------------------


def test_battery_exchange_mode_topic() -> None:
    from helper_common.topics import battery_exchange_mode_topic
    assert battery_exchange_mode_topic("mimir", "home_battery") == "mimir/output/battery/home_battery/exchange_mode"


def test_ev_exchange_mode_topic() -> None:
    from helper_common.topics import ev_exchange_mode_topic
    assert ev_exchange_mode_topic("mimir", "ev_charger") == "mimir/output/ev/ev_charger/exchange_mode"


def test_ev_loadbalance_topic() -> None:
    from helper_common.topics import ev_loadbalance_topic
    assert ev_loadbalance_topic("mimir", "ev_charger") == "mimir/output/ev/ev_charger/loadbalance"


def test_pv_power_limit_topic() -> None:
    from helper_common.topics import pv_power_limit_topic
    assert pv_power_limit_topic("mimir", "roof_pv") == "mimir/output/pv/roof_pv/power_limit_kw"


def test_pv_zero_export_topic() -> None:
    from helper_common.topics import pv_zero_export_topic
    assert pv_zero_export_topic("mimir", "roof_pv") == "mimir/output/pv/roof_pv/zero_export_mode"


def test_pv_on_off_topic() -> None:
    from helper_common.topics import pv_on_off_topic
    assert pv_on_off_topic("mimir", "roof_pv") == "mimir/output/pv/roof_pv/on_off_mode"


def test_deferrable_recommended_start_topic() -> None:
    from helper_common.topics import deferrable_recommended_start_topic
    assert (
        deferrable_recommended_start_topic("mimir", "washing_machine")
        == "mimir/output/deferrable/washing_machine/recommended_start"
    )
