"""Unit tests for mimirheim.config.schema — all Pydantic config models.

Covers happy-path construction and validation rejection (sad paths) for every
model in the config schema. Tests are written before the implementation exists
and must fail with ImportError or similar until schema.py is created.
"""

import pytest
from pydantic import ValidationError

from mimirheim.config.schema import (
    BalancedWeightsConfig,
    BatteryCapabilitiesConfig,
    BatteryConfig,
    BatteryInputsConfig,
    ConstraintsConfig,
    DebugConfig,
    DeferrableLoadConfig,
    EfficiencySegment,
    EvCapabilitiesConfig,
    EvConfig,
    EvInputsConfig,
    EvOutputsConfig,
    GridConfig,
    HybridInverterConfig,
    MimirheimConfig,
    MqttConfig,
    ObjectivesConfig,
    OutputsConfig,
    PvCapabilitiesConfig,
    PvConfig,
    PvOutputsConfig,
    SocTopicConfig,
    SolverConfig,
    StaticLoadConfig,
)


# ---------------------------------------------------------------------------
# BatteryCapabilitiesConfig
# ---------------------------------------------------------------------------


def test_battery_capabilities_defaults() -> None:
    caps = BatteryCapabilitiesConfig()
    assert caps.staged_power is False
    assert caps.zero_exchange is False


def test_battery_capabilities_staged_power_true() -> None:
    caps = BatteryCapabilitiesConfig(staged_power=True)
    assert caps.staged_power is True


def test_battery_capabilities_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        BatteryCapabilitiesConfig(unknown=True)


def test_battery_config_with_capabilities() -> None:
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_charge_seg()],
        discharge_segments=[_discharge_seg()],
        capabilities=BatteryCapabilitiesConfig(staged_power=True),
    )
    assert cfg.capabilities.staged_power is True


# ---------------------------------------------------------------------------
# SocTopicConfig, BatteryInputsConfig, EvInputsConfig
# ---------------------------------------------------------------------------


def test_soc_topic_config_kwh() -> None:
    cfg = SocTopicConfig(topic="ha/sensor/battery_soc/state", unit="kwh")
    assert cfg.unit == "kwh"


def test_soc_topic_config_percent() -> None:
    cfg = SocTopicConfig(topic="ha/sensor/battery_soc/state", unit="percent")
    assert cfg.unit == "percent"


def test_soc_topic_config_invalid_unit_rejected() -> None:
    with pytest.raises(ValidationError):
        SocTopicConfig(topic="ha/sensor/battery_soc/state", unit="ampere")


def test_soc_topic_config_no_unit_defaults_to_percent() -> None:
    cfg = SocTopicConfig()
    assert cfg.unit == "percent"


def test_battery_inputs_config_valid() -> None:
    cfg = BatteryInputsConfig(
        soc=SocTopicConfig(topic="ha/sensor/battery_soc/state", unit="percent")
    )
    assert cfg.soc.unit == "percent"


def test_ev_inputs_config_valid() -> None:
    cfg = EvInputsConfig(
        soc=SocTopicConfig(topic="ha/sensor/ev_soc/state", unit="kwh"),
        plugged_in_topic="ha/binary_sensor/ev_plugged/state",
    )
    assert cfg.plugged_in_topic == "ha/binary_sensor/ev_plugged/state"


def test_battery_config_with_inputs() -> None:
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_charge_seg()],
        discharge_segments=[_discharge_seg()],
        inputs=BatteryInputsConfig(
            soc=SocTopicConfig(topic="ha/sensor/battery_soc/state", unit="kwh")
        ),
    )
    assert cfg.inputs is not None
    assert cfg.inputs.soc.topic == "ha/sensor/battery_soc/state"


def test_ev_config_with_inputs() -> None:
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[_charge_seg()],
        inputs=EvInputsConfig(
            soc=SocTopicConfig(topic="ha/sensor/ev_soc/state", unit="kwh"),
            plugged_in_topic="ha/binary_sensor/ev_plugged/state",
        ),
    )
    assert cfg.inputs is not None


# ---------------------------------------------------------------------------
# EfficiencySegment
# ---------------------------------------------------------------------------


def test_efficiency_segment_valid() -> None:
    seg = EfficiencySegment(power_max_kw=2.0, efficiency=0.95)
    assert seg.power_max_kw == 2.0
    assert seg.efficiency == 0.95


def test_efficiency_segment_zero_efficiency_rejected() -> None:
    with pytest.raises(ValidationError):
        EfficiencySegment(power_max_kw=2.0, efficiency=0.0)


def test_efficiency_segment_above_one_rejected() -> None:
    with pytest.raises(ValidationError):
        EfficiencySegment(power_max_kw=2.0, efficiency=1.01)


# ---------------------------------------------------------------------------
# BatteryConfig
# ---------------------------------------------------------------------------


def _charge_seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=3.0, efficiency=0.95)


def _discharge_seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=3.0, efficiency=0.95)


def test_battery_config_valid() -> None:
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_charge_seg(), EfficiencySegment(power_max_kw=1.0, efficiency=0.90)],
        discharge_segments=[_discharge_seg()],
    )
    assert cfg.capacity_kwh == 10.0
    assert len(cfg.charge_segments) == 2
    assert cfg.wear_cost_eur_per_kwh == 0.0
    assert cfg.capabilities.staged_power is False
    assert cfg.capabilities.zero_exchange is False


def test_battery_config_negative_capacity_rejected() -> None:
    with pytest.raises(ValidationError):
        BatteryConfig(
            capacity_kwh=-1.0,
            charge_segments=[_charge_seg()],
            discharge_segments=[_discharge_seg()],
        )


def test_battery_config_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        BatteryConfig(
            capacity_kwh=10.0,
            charge_segments=[_charge_seg()],
            discharge_segments=[_discharge_seg()],
            unknown_field="oops",
        )


# ---------------------------------------------------------------------------
# EvConfig
# ---------------------------------------------------------------------------


def test_ev_config_valid() -> None:
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[_charge_seg()],
    )
    assert cfg.discharge_segments == []


def test_ev_capabilities_defaults() -> None:
    """EvCapabilitiesConfig defaults: all capability flags are False."""
    caps = EvCapabilitiesConfig()
    assert caps.zero_exchange is False
    assert caps.v2h is False
    assert caps.loadbalance is False
    assert caps.staged_power is False


def test_ev_capabilities_zero_exchange_can_be_enabled() -> None:
    """EvCapabilitiesConfig accepts zero_exchange=True when v2h=True."""
    caps = EvCapabilitiesConfig(zero_exchange=True, v2h=True)
    assert caps.zero_exchange is True


def test_ev_capabilities_rejects_unknown_fields() -> None:
    """EvCapabilitiesConfig enforces extra='forbid'."""
    with pytest.raises(ValidationError):
        EvCapabilitiesConfig(unknown_cap=True)


def test_battery_capabilities_accepts_zero_exchange() -> None:
    """BatteryCapabilitiesConfig accepts zero_exchange=True."""
    caps = BatteryCapabilitiesConfig(zero_exchange=True)
    assert caps.zero_exchange is True


def test_battery_capabilities_zero_exchange_defaults_false() -> None:
    """BatteryCapabilitiesConfig.zero_exchange defaults to False."""
    caps = BatteryCapabilitiesConfig()
    assert caps.zero_exchange is False


def test_ev_outputs_exchange_mode_defaults_none() -> None:
    """EvOutputsConfig.exchange_mode defaults to None."""
    out = EvOutputsConfig()
    assert out.exchange_mode is None


def test_ev_outputs_exchange_mode_can_be_set() -> None:
    """EvOutputsConfig accepts a string topic for exchange_mode."""
    out = EvOutputsConfig(exchange_mode="mimir/ev/ev1/exchange_mode")
    assert out.exchange_mode == "mimir/ev/ev1/exchange_mode"


def test_ev_config_with_zero_exchange_capabilities_and_outputs() -> None:
    """EvConfig accepts EvCapabilitiesConfig with zero_exchange and EvOutputsConfig with exchange_mode."""
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[_charge_seg()],
        capabilities=EvCapabilitiesConfig(zero_exchange=True, v2h=True),
        outputs=EvOutputsConfig(exchange_mode="mimir/ev/ev1/exchange_mode"),
    )
    assert cfg.capabilities.zero_exchange is True
    assert cfg.outputs.exchange_mode == "mimir/ev/ev1/exchange_mode"


# ---------------------------------------------------------------------------
# PvConfig
# ---------------------------------------------------------------------------


def test_pv_config_valid() -> None:
    cfg = PvConfig(max_power_kw=6.0, topic_forecast="mimir/input/pv_forecast")
    assert cfg.max_power_kw == 6.0


def test_pv_capabilities_defaults_all_false() -> None:
    """PvCapabilitiesConfig defaults: all capabilities are off."""
    caps = PvCapabilitiesConfig()
    assert caps.power_limit is False
    assert caps.zero_export is False


def test_pv_capabilities_can_enable_power_limit() -> None:
    caps = PvCapabilitiesConfig(power_limit=True)
    assert caps.power_limit is True


def test_pv_capabilities_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PvCapabilitiesConfig(unknown=True)


def test_pv_outputs_defaults_to_none() -> None:
    """PvOutputsConfig fields default to None (outputs are optional)."""
    out = PvOutputsConfig()
    assert out.power_limit_kw is None
    assert out.zero_export_mode is None
    assert out.on_off_mode is None


def test_pv_outputs_can_set_on_off_mode_topic() -> None:
    out = PvOutputsConfig(on_off_mode="mimir/pv/array_a/on_off_mode")
    assert out.on_off_mode == "mimir/pv/array_a/on_off_mode"


def test_pv_on_off_capability_requires_output_topic() -> None:
    """capabilities.on_off=True without outputs.on_off_mode no longer raises.

    The per-device validator was removed in Plan 50. The output topic is
    auto-derived at MimirheimConfig validation time; direct PvConfig construction
    leaves the field as None.
    """
    cfg = PvConfig(
        max_power_kw=4.5,
        topic_forecast="mimir/input/pv_forecast",
        capabilities=PvCapabilitiesConfig(on_off=True),
        # on_off_mode not set — now accepted; derived when MimirheimConfig validates
    )
    assert cfg.capabilities.on_off is True
    assert cfg.outputs.on_off_mode is None  # derived later by MimirheimConfig


def test_pv_on_off_capability_with_output_topic_valid() -> None:
    """capabilities.on_off=True with outputs.on_off_mode set is valid."""
    cfg = PvConfig(
        max_power_kw=4.5,
        topic_forecast="mimir/input/pv_forecast",
        capabilities=PvCapabilitiesConfig(on_off=True),
        outputs=PvOutputsConfig(on_off_mode="mimir/pv/roof/on_off_mode"),
    )
    assert cfg.capabilities.on_off is True
    assert cfg.outputs.on_off_mode == "mimir/pv/roof/on_off_mode"


def test_pv_outputs_can_set_power_limit_topic() -> None:
    out = PvOutputsConfig(power_limit_kw="mimir/pv/array_a/power_limit_kw")
    assert out.power_limit_kw == "mimir/pv/array_a/power_limit_kw"


def test_pv_outputs_can_set_zero_export_mode_topic() -> None:
    out = PvOutputsConfig(zero_export_mode="mimir/pv/array_a/zero_export_mode")
    assert out.zero_export_mode == "mimir/pv/array_a/zero_export_mode"


def test_pv_outputs_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        PvOutputsConfig(unknown="topic")


def test_pv_config_with_capabilities_and_outputs() -> None:
    cfg = PvConfig(
        max_power_kw=6.0,
        topic_forecast="mimir/input/pv_forecast",
        capabilities=PvCapabilitiesConfig(power_limit=True, zero_export=True),
        outputs=PvOutputsConfig(
            power_limit_kw="mimir/pv/a/power_limit_kw",
            zero_export_mode="mimir/pv/a/zero_export_mode",
        ),
    )
    assert cfg.capabilities.power_limit is True
    assert cfg.outputs.zero_export_mode == "mimir/pv/a/zero_export_mode"


def test_pv_production_stages_valid() -> None:
    """PvConfig accepts a valid production_stages list."""
    cfg = PvConfig(
        max_power_kw=4.5,
        topic_forecast="mimir/input/pv_forecast",
        production_stages=[0.0, 1.5, 3.0, 4.5],
    )
    assert cfg.production_stages == [0.0, 1.5, 3.0, 4.5]


def test_pv_production_stages_max_power_below_last_stage_raises() -> None:
    """max_power_kw must be at least as large as the last (highest) stage value."""
    with pytest.raises(ValidationError):
        PvConfig(
            max_power_kw=3.0,
            topic_forecast="mimir/input/pv_forecast",
            production_stages=[0.0, 1.5, 3.0, 4.5],  # last stage 4.5 > max_power_kw 3.0
        )


def test_pv_production_stages_missing_zero_raises() -> None:
    """production_stages must start with 0.0 (the off state)."""
    with pytest.raises(ValidationError):
        PvConfig(
            max_power_kw=3.0,
            topic_forecast="mimir/input/pv_forecast",
            production_stages=[1.5, 3.0],
        )


def test_pv_production_stages_not_strictly_increasing_raises() -> None:
    """production_stages must be strictly increasing."""
    with pytest.raises(ValidationError):
        PvConfig(
            max_power_kw=3.0,
            topic_forecast="mimir/input/pv_forecast",
            production_stages=[0.0, 3.0, 1.5],
        )


def test_pv_production_stages_and_power_limit_raises() -> None:
    """production_stages and capabilities.power_limit=True are mutually exclusive."""
    with pytest.raises(ValidationError):
        PvConfig(
            max_power_kw=4.5,
            topic_forecast="mimir/input/pv_forecast",
            production_stages=[0.0, 1.5, 4.5],
            capabilities=PvCapabilitiesConfig(power_limit=True),
        )


def test_pv_production_stages_and_on_off_raises() -> None:
    """production_stages and capabilities.on_off=True are mutually exclusive."""
    with pytest.raises(ValidationError):
        PvConfig(
            max_power_kw=4.5,
            topic_forecast="mimir/input/pv_forecast",
            production_stages=[0.0, 1.5, 4.5],
            capabilities=PvCapabilitiesConfig(on_off=True),
        )


# ---------------------------------------------------------------------------
# DeferrableLoadConfig
# ---------------------------------------------------------------------------


def test_deferrable_load_config_valid() -> None:
    cfg = DeferrableLoadConfig(
        power_profile=[1.8, 0.9, 0.9, 1.8, 1.8, 1.8, 1.8, 1.8],
        topic_window_earliest="mimir/input/washer/earliest",
        topic_window_latest="mimir/input/washer/latest",
    )
    assert len(cfg.power_profile) == 8


def test_deferrable_load_config_rejects_empty_profile() -> None:
    """power_profile must contain at least one entry."""
    with pytest.raises(ValidationError):
        DeferrableLoadConfig(
            power_profile=[],
            topic_window_earliest="mimir/input/washer/earliest",
            topic_window_latest="mimir/input/washer/latest",
        )


def test_deferrable_load_config_rejects_zero_power_in_profile() -> None:
    """All power_profile entries must be strictly positive."""
    with pytest.raises(ValidationError):
        DeferrableLoadConfig(
            power_profile=[1.0, 0.0, 1.0],
            topic_window_earliest="mimir/input/washer/earliest",
            topic_window_latest="mimir/input/washer/latest",
        )


# ---------------------------------------------------------------------------
# StaticLoadConfig
# ---------------------------------------------------------------------------


def test_static_load_config_valid() -> None:
    cfg = StaticLoadConfig(topic_forecast="mimir/input/base_load_forecast")
    assert cfg.topic_forecast == "mimir/input/base_load_forecast"


# ---------------------------------------------------------------------------
# GridConfig
# ---------------------------------------------------------------------------


def test_grid_config_valid() -> None:
    cfg = GridConfig(import_limit_kw=25.0, export_limit_kw=25.0)
    assert cfg.import_limit_kw == 25.0


# ---------------------------------------------------------------------------
# BalancedWeightsConfig and ObjectivesConfig
# ---------------------------------------------------------------------------


def test_balanced_weights_config_valid() -> None:
    cfg = BalancedWeightsConfig(cost_weight=1.0, self_sufficiency_weight=2.0)
    assert cfg.self_sufficiency_weight == 2.0


def test_objectives_config_without_balanced_weights() -> None:
    # balanced_weights is optional; omitting it must not raise
    cfg = ObjectivesConfig()
    assert cfg.balanced_weights is None


# ---------------------------------------------------------------------------
# MimirheimConfig
# ---------------------------------------------------------------------------


def _minimal_hioo_config() -> dict:
    return {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "objectives": {},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
    }


def test_hioo_config_valid() -> None:
    raw = _minimal_hioo_config()
    raw["batteries"] = {
        "battery_main": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
        }
    }
    raw["pv_arrays"] = {
        "pv_roof": {"max_power_kw": 6.0, "topic_forecast": "mimir/input/pv_forecast"},
    }
    raw["ev_chargers"] = {
        "ev_charger": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
        }
    }
    cfg = MimirheimConfig.model_validate(raw)
    assert "battery_main" in cfg.batteries
    assert "pv_roof" in cfg.pv_arrays
    assert "ev_charger" in cfg.ev_chargers


def test_hioo_config_minimal() -> None:
    cfg = MimirheimConfig.model_validate(_minimal_hioo_config())
    assert cfg.batteries == {}
    assert "base_load" in cfg.static_loads


def test_hioo_config_extra_field_rejected() -> None:
    raw = _minimal_hioo_config()
    raw["unexpected_section"] = True
    with pytest.raises(ValidationError):
        MimirheimConfig.model_validate(raw)


def test_hioo_config_duplicate_device_names_rejected() -> None:
    raw = _minimal_hioo_config()
    raw["batteries"] = {
        "my_device": {
            "capacity_kwh": 10.0,
            "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
            "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
        }
    }
    raw["ev_chargers"] = {
        "my_device": {  # same name as the battery above
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
        }
    }
    with pytest.raises(ValidationError):
        MimirheimConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# BatteryConfig — optimal SOC penalty (plan 21)
# ---------------------------------------------------------------------------


def _base_battery_config() -> dict:
    return {
        "capacity_kwh": 10.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
        "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
    }


def test_battery_optimal_lower_soc_kwh_defaults_to_zero() -> None:
    cfg = BatteryConfig.model_validate(_base_battery_config())
    assert cfg.optimal_lower_soc_kwh == 0.0


def test_battery_soc_low_penalty_defaults_to_zero() -> None:
    cfg = BatteryConfig.model_validate(_base_battery_config())
    assert cfg.soc_low_penalty_eur_per_kwh_h == 0.0


def test_battery_optimal_lower_soc_cannot_exceed_capacity() -> None:
    data = _base_battery_config()
    data["optimal_lower_soc_kwh"] = 11.0  # > capacity_kwh of 10.0
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


def test_battery_optimal_lower_soc_cannot_be_below_min_soc() -> None:
    data = _base_battery_config()
    data["min_soc_kwh"] = 2.0
    data["optimal_lower_soc_kwh"] = 1.0  # < min_soc_kwh of 2.0
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


# ---------------------------------------------------------------------------
# BatteryConfig — power derating near SOC extremes (plan 22)
# ---------------------------------------------------------------------------


def test_battery_derating_all_fields_none_by_default() -> None:
    cfg = BatteryConfig.model_validate(_base_battery_config())
    assert cfg.reduce_charge_above_soc_kwh is None
    assert cfg.reduce_charge_min_kw is None
    assert cfg.reduce_discharge_below_soc_kwh is None
    assert cfg.reduce_discharge_min_kw is None


def test_battery_charge_derating_requires_both_fields() -> None:
    """Setting only one of the charge derating pair must raise ValidationError."""
    data = _base_battery_config()
    data["reduce_charge_above_soc_kwh"] = 8.0
    # reduce_charge_min_kw omitted — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)

    data2 = _base_battery_config()
    data2["reduce_charge_min_kw"] = 1.0
    # reduce_charge_above_soc_kwh omitted — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data2)


def test_battery_discharge_derating_requires_both_fields() -> None:
    """Setting only one of the discharge derating pair must raise ValidationError."""
    data = _base_battery_config()
    data["reduce_discharge_below_soc_kwh"] = 2.0
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)

    data2 = _base_battery_config()
    data2["reduce_discharge_min_kw"] = 0.5
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data2)


def test_battery_reduce_charge_above_must_be_in_range() -> None:
    """reduce_charge_above_soc_kwh must be strictly between min_soc_kwh and capacity_kwh."""
    base = {**_base_battery_config(), "reduce_charge_min_kw": 1.0}

    # Equal to capacity_kwh (10.0) — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_charge_above_soc_kwh": 10.0})

    # Below or equal to min_soc_kwh (0.0) — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_charge_above_soc_kwh": 0.0})


def test_battery_reduce_charge_min_must_be_positive_and_below_max() -> None:
    """reduce_charge_min_kw must be > 0 and < max_charge_kw (5.0 in base config)."""
    base = {**_base_battery_config(), "reduce_charge_above_soc_kwh": 8.0}

    # Equal to zero — must reject (ge=0 allows 0 but validator requires > 0)
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_charge_min_kw": 0.0})

    # Equal to or exceeds max_charge_kw (5.0) — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_charge_min_kw": 5.0})


def test_battery_reduce_discharge_below_must_be_in_range() -> None:
    """reduce_discharge_below_soc_kwh must be strictly between min_soc_kwh and capacity_kwh."""
    base = {**_base_battery_config(), "reduce_discharge_min_kw": 0.5}

    # Equal to capacity_kwh (10.0) — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_discharge_below_soc_kwh": 10.0})

    # Equal to min_soc_kwh (0.0) — must reject
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate({**base, "reduce_discharge_below_soc_kwh": 0.0})


# ---------------------------------------------------------------------------
# EfficiencyBreakpoint and BatteryConfig SOS2 curve fields (plan 23)
# ---------------------------------------------------------------------------

from mimirheim.config.schema import EfficiencyBreakpoint  # noqa: E402


def _bp(power_kw: float, efficiency: float) -> dict:
    return {"power_kw": power_kw, "efficiency": efficiency}


def _sos2_battery_config(
    charge_curve: list[dict] | None = None,
    discharge_curve: list[dict] | None = None,
) -> dict:
    """Return a BatteryConfig dict that uses SOS2 curves instead of segments."""
    base = {
        "capacity_kwh": 10.0,
        "charge_efficiency_curve": charge_curve or [_bp(0.0, 0.95), _bp(5.0, 0.90)],
        "discharge_efficiency_curve": discharge_curve or [_bp(0.0, 0.95), _bp(5.0, 0.90)],
    }
    return base


def test_efficiency_breakpoint_validates_power_nonnegative() -> None:
    with pytest.raises(ValidationError):
        EfficiencyBreakpoint.model_validate({"power_kw": -1.0, "efficiency": 0.95})


def test_efficiency_breakpoint_validates_efficiency_range() -> None:
    with pytest.raises(ValidationError):
        EfficiencyBreakpoint.model_validate({"power_kw": 1.0, "efficiency": 0.0})
    with pytest.raises(ValidationError):
        EfficiencyBreakpoint.model_validate({"power_kw": 1.0, "efficiency": 1.01})


def test_battery_sos2_curve_requires_minimum_two_breakpoints() -> None:
    data = _sos2_battery_config(
        charge_curve=[_bp(0.0, 0.95)],  # only one breakpoint — invalid
    )
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


def test_battery_sos2_curve_first_breakpoint_must_be_zero_power() -> None:
    data = _sos2_battery_config(
        charge_curve=[_bp(1.0, 0.95), _bp(5.0, 0.90)],  # first power != 0
    )
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


def test_battery_sos2_curve_powers_must_be_strictly_increasing() -> None:
    data = _sos2_battery_config(
        charge_curve=[_bp(0.0, 0.95), _bp(3.0, 0.92), _bp(3.0, 0.88)],  # duplicate
    )
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


def test_battery_sos2_requires_segments_or_curve_not_both() -> None:
    data = {
        "capacity_kwh": 10.0,
        "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
        "charge_efficiency_curve": [_bp(0.0, 0.95), _bp(5.0, 0.90)],
        "discharge_efficiency_curve": [_bp(0.0, 0.95), _bp(5.0, 0.90)],
    }
    with pytest.raises(ValidationError):
        BatteryConfig.model_validate(data)


# ---------------------------------------------------------------------------
# ThermalBoilerConfig validation
# ---------------------------------------------------------------------------


def _thermal_boiler_data(**overrides: object) -> dict:
    base = {
        "volume_liters": 200.0,
        "elec_power_kw": 3.0,
        "setpoint_c": 55.0,
        "min_temp_c": 40.0,
        "cooling_rate_k_per_hour": 2.0,
    }
    base.update(overrides)
    return base


def test_thermal_boiler_defaults_valid() -> None:
    """A minimal config with only required fields validates without error."""
    from mimirheim.config.schema import ThermalBoilerConfig

    cfg = ThermalBoilerConfig.model_validate(_thermal_boiler_data())
    assert cfg.cop == 1.0
    assert cfg.min_run_steps == 0
    assert cfg.wear_cost_eur_per_kwh == 0.0


def test_thermal_boiler_cop_must_be_positive() -> None:
    """cop <= 0 must raise ValidationError."""
    from mimirheim.config.schema import ThermalBoilerConfig

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(cop=0.0))

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(cop=-1.0))


def test_thermal_boiler_min_temp_below_setpoint() -> None:
    """min_temp_c >= setpoint_c must raise ValidationError."""
    from mimirheim.config.schema import ThermalBoilerConfig

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(min_temp_c=55.0, setpoint_c=55.0))

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(min_temp_c=60.0, setpoint_c=55.0))


def test_thermal_boiler_volume_must_be_positive() -> None:
    """volume_liters <= 0 must raise ValidationError."""
    from mimirheim.config.schema import ThermalBoilerConfig

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(volume_liters=0.0))

    with pytest.raises(ValidationError):
        ThermalBoilerConfig.model_validate(_thermal_boiler_data(volume_liters=-50.0))


# ---------------------------------------------------------------------------
# SpaceHeatingConfig validation
# ---------------------------------------------------------------------------


def _space_heating_on_off(**overrides: object) -> dict:
    base = {
        "elec_power_kw": 5.0,
        "cop": 3.5,
    }
    base.update(overrides)
    return base


def _space_heating_stages(
    stages: list[dict] | None = None,
) -> dict:
    if stages is None:
        stages = [
            {"elec_kw": 0.0, "cop": 0.0},
            {"elec_kw": 3.0, "cop": 3.0},
            {"elec_kw": 5.0, "cop": 3.5},
        ]
    return {"stages": stages}


def test_space_heating_stages_must_start_with_zero_power() -> None:
    """First stage elec_kw != 0.0 must raise ValidationError."""
    from mimirheim.config.schema import SpaceHeatingConfig

    data = _space_heating_stages(
        stages=[
            {"elec_kw": 1.0, "cop": 2.0},  # first stage not zero
            {"elec_kw": 5.0, "cop": 3.5},
        ]
    )
    with pytest.raises(ValidationError):
        SpaceHeatingConfig.model_validate(data)


def test_space_heating_stages_must_be_strictly_increasing_power() -> None:
    """Duplicate elec_kw values in stages must raise ValidationError."""
    from mimirheim.config.schema import SpaceHeatingConfig

    data = _space_heating_stages(
        stages=[
            {"elec_kw": 0.0, "cop": 0.0},
            {"elec_kw": 3.0, "cop": 3.0},
            {"elec_kw": 3.0, "cop": 3.5},  # duplicate power
        ]
    )
    with pytest.raises(ValidationError):
        SpaceHeatingConfig.model_validate(data)


def test_space_heating_on_off_and_stages_mutually_exclusive() -> None:
    """Providing both elec_power_kw and stages must raise ValidationError."""
    from mimirheim.config.schema import SpaceHeatingConfig

    data = {
        "elec_power_kw": 5.0,
        "cop": 3.5,
        "stages": [
            {"elec_kw": 0.0, "cop": 0.0},
            {"elec_kw": 5.0, "cop": 3.5},
        ],
    }
    with pytest.raises(ValidationError):
        SpaceHeatingConfig.model_validate(data)


def _combi_config(**overrides: object) -> dict:
    base: dict = {
        "elec_power_kw": 6.0,
        "cop_dhw": 2.8,
        "cop_sh": 3.8,
        "volume_liters": 200.0,
        "setpoint_c": 55.0,
        "min_temp_c": 40.0,
        "cooling_rate_k_per_hour": 2.0,
    }
    base.update(overrides)
    return base


def test_combi_cop_dhw_and_cop_sh_both_positive() -> None:
    """cop_dhw <= 0 or cop_sh <= 0 must raise ValidationError."""
    from mimirheim.config.schema import CombiHeatPumpConfig

    with pytest.raises(ValidationError):
        CombiHeatPumpConfig.model_validate(_combi_config(cop_dhw=0.0))

    with pytest.raises(ValidationError):
        CombiHeatPumpConfig.model_validate(_combi_config(cop_sh=-1.0))


def test_combi_min_temp_below_setpoint() -> None:
    """min_temp_c >= setpoint_c must raise ValidationError."""
    from mimirheim.config.schema import CombiHeatPumpConfig

    with pytest.raises(ValidationError):
        CombiHeatPumpConfig.model_validate(_combi_config(min_temp_c=55.0, setpoint_c=55.0))

    with pytest.raises(ValidationError):
        CombiHeatPumpConfig.model_validate(_combi_config(min_temp_c=60.0, setpoint_c=55.0))


# ---------------------------------------------------------------------------
# BuildingThermalConfig schema tests (plan 28)
# ---------------------------------------------------------------------------


def test_btm_thermal_capacity_must_be_positive() -> None:
    """thermal_capacity_kwh_per_k=0 must raise ValidationError (must be > 0)."""
    from mimirheim.config.schema import BuildingThermalConfig

    with pytest.raises(ValidationError):
        BuildingThermalConfig.model_validate(
            {
                "thermal_capacity_kwh_per_k": 0,
                "heat_loss_coeff_kw_per_k": 0.8,
                "comfort_min_c": 18.0,
                "comfort_max_c": 24.0,
            }
        )


def test_btm_heat_loss_coeff_must_be_positive() -> None:
    """heat_loss_coeff_kw_per_k=0 must raise ValidationError (must be > 0)."""
    from mimirheim.config.schema import BuildingThermalConfig

    with pytest.raises(ValidationError):
        BuildingThermalConfig.model_validate(
            {
                "thermal_capacity_kwh_per_k": 5.0,
                "heat_loss_coeff_kw_per_k": 0,
                "comfort_min_c": 18.0,
                "comfort_max_c": 24.0,
            }
        )


def test_btm_comfort_min_must_be_below_max() -> None:
    """comfort_min_c >= comfort_max_c must raise ValidationError."""
    from mimirheim.config.schema import BuildingThermalConfig

    with pytest.raises(ValidationError):
        BuildingThermalConfig.model_validate(
            {
                "thermal_capacity_kwh_per_k": 5.0,
                "heat_loss_coeff_kw_per_k": 0.8,
                "comfort_min_c": 24.0,
                "comfort_max_c": 18.0,
            }
        )

    with pytest.raises(ValidationError):
        BuildingThermalConfig.model_validate(
            {
                "thermal_capacity_kwh_per_k": 5.0,
                "heat_loss_coeff_kw_per_k": 0.8,
                "comfort_min_c": 22.0,
                "comfort_max_c": 22.0,
            }
        )


def test_btm_space_heating_accepts_building_thermal_field() -> None:
    """SpaceHeatingConfig with a valid BuildingThermalConfig validates without error."""
    from mimirheim.config.schema import BuildingThermalConfig, SpaceHeatingConfig

    cfg = SpaceHeatingConfig.model_validate(
        {
            "elec_power_kw": 5.0,
            "cop": 3.5,
            "building_thermal": {
                "thermal_capacity_kwh_per_k": 5.0,
                "heat_loss_coeff_kw_per_k": 0.8,
            },
        }
    )
    assert isinstance(cfg.building_thermal, BuildingThermalConfig)
    assert cfg.building_thermal.comfort_min_c == 19.0  # default
    assert cfg.building_thermal.comfort_max_c == 24.0  # default


def test_btm_combi_hp_accepts_building_thermal_field() -> None:
    """CombiHeatPumpConfig with a valid BuildingThermalConfig validates without error."""
    from mimirheim.config.schema import BuildingThermalConfig, CombiHeatPumpConfig

    cfg = CombiHeatPumpConfig.model_validate(
        {
            **_combi_config(),
            "building_thermal": {
                "thermal_capacity_kwh_per_k": 8.0,
                "heat_loss_coeff_kw_per_k": 0.6,
                "comfort_min_c": 20.0,
                "comfort_max_c": 23.0,
            },
        }
    )
    assert isinstance(cfg.building_thermal, BuildingThermalConfig)
    assert cfg.building_thermal.thermal_capacity_kwh_per_k == 8.0


# ---------------------------------------------------------------------------
# SolverConfig
# ---------------------------------------------------------------------------


def test_solver_config_defaults() -> None:
    """SolverConfig uses safe defaults: 72-hour horizon cap and all-cores threading."""
    cfg = SolverConfig()
    assert cfg.max_horizon_steps == 288
    assert cfg.threads == -1


def test_solver_config_max_horizon_steps_at_minimum() -> None:
    """max_horizon_steps accepts the minimum allowed value of 96 (24 h)."""
    cfg = SolverConfig(max_horizon_steps=96)
    assert cfg.max_horizon_steps == 96


def test_solver_config_max_horizon_steps_below_minimum_rejected() -> None:
    """max_horizon_steps below 96 (less than one full day) is rejected."""
    with pytest.raises(ValidationError):
        SolverConfig(max_horizon_steps=95)


def test_solver_config_threads_below_minus_one_rejected() -> None:
    """threads values below -1 have no defined meaning and are rejected."""
    with pytest.raises(ValidationError):
        SolverConfig(threads=-2)


def test_solver_config_extra_field_rejected() -> None:
    """SolverConfig enforces extra=forbid so typos in field names are caught."""
    with pytest.raises(ValidationError):
        SolverConfig(unknown_field=1)


def test_hioo_config_carries_solver_defaults() -> None:
    """MimirheimConfig includes a solver field that defaults to SolverConfig defaults."""
    cfg = MimirheimConfig.model_validate(_minimal_hioo_config())
    assert cfg.solver.max_horizon_steps == 288
    assert cfg.solver.threads == -1


# ---------------------------------------------------------------------------
# DebugConfig
# ---------------------------------------------------------------------------


def test_debug_config_defaults() -> None:
    """DebugConfig defaults to disabled with no dump directory."""
    cfg = DebugConfig()
    assert cfg.enabled is False
    assert cfg.dump_dir is None
    assert cfg.max_dumps == 50


def test_debug_config_enabled_true() -> None:
    """enabled=True is accepted and round-trips correctly."""
    cfg = DebugConfig(enabled=True, dump_dir="/tmp/dumps", max_dumps=5)
    assert cfg.enabled is True
    assert cfg.max_dumps == 5


def test_debug_config_extra_field_rejected() -> None:
    """DebugConfig enforces extra=forbid so unknown fields are caught."""
    with pytest.raises(ValidationError):
        DebugConfig(unknown=True)


def test_hioo_config_debug_defaults_to_disabled() -> None:
    """MimirheimConfig.debug.enabled defaults to False so production runs are unaffected."""
    cfg = MimirheimConfig.model_validate(_minimal_hioo_config())
    assert cfg.debug.enabled is False


# ---------------------------------------------------------------------------
# BatteryConfig — min_charge_kw / min_discharge_kw (Plan 38C)
# ---------------------------------------------------------------------------


def test_battery_min_charge_kw_defaults_to_none() -> None:
    """BatteryConfig.min_charge_kw defaults to None (no floor constraint)."""
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        discharge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
    )
    assert cfg.min_charge_kw is None


def test_battery_min_discharge_kw_defaults_to_none() -> None:
    """BatteryConfig.min_discharge_kw defaults to None (no floor constraint)."""
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        discharge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
    )
    assert cfg.min_discharge_kw is None


def test_battery_min_charge_kw_accepts_positive_value() -> None:
    """BatteryConfig.min_charge_kw accepts a positive float."""
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        discharge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        min_charge_kw=1.38,
    )
    assert cfg.min_charge_kw == 1.38


def test_battery_min_discharge_kw_accepts_positive_value() -> None:
    """BatteryConfig.min_discharge_kw accepts a positive float."""
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        discharge_segments=[EfficiencySegment(power_max_kw=5.0, efficiency=0.95)],
        min_discharge_kw=0.5,
    )
    assert cfg.min_discharge_kw == 0.5


# ---------------------------------------------------------------------------
# EvConfig — min_charge_kw / min_discharge_kw (Plan 38C)
# ---------------------------------------------------------------------------


def test_ev_min_charge_kw_defaults_to_none() -> None:
    """EvConfig.min_charge_kw defaults to None (no floor constraint)."""
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[EfficiencySegment(power_max_kw=11.0, efficiency=0.93)],
        discharge_segments=[],
    )
    assert cfg.min_charge_kw is None


def test_ev_min_discharge_kw_defaults_to_none() -> None:
    """EvConfig.min_discharge_kw defaults to None (no floor constraint)."""
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[EfficiencySegment(power_max_kw=11.0, efficiency=0.93)],
        discharge_segments=[],
    )
    assert cfg.min_discharge_kw is None


def test_ev_min_charge_kw_accepts_positive_value() -> None:
    """EvConfig.min_charge_kw accepts a positive float."""
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[EfficiencySegment(power_max_kw=11.0, efficiency=0.93)],
        discharge_segments=[],
        min_charge_kw=1.4,
    )
    assert cfg.min_charge_kw == 1.4


def test_ev_min_discharge_kw_accepts_positive_value() -> None:
    """EvConfig.min_discharge_kw accepts a positive float on a V2H EV."""
    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[EfficiencySegment(power_max_kw=11.0, efficiency=0.93)],
        discharge_segments=[EfficiencySegment(power_max_kw=7.4, efficiency=0.90)],
        min_discharge_kw=1.4,
    )
    assert cfg.min_discharge_kw == 1.4


# ---------------------------------------------------------------------------
# PvCapabilitiesConfig — mutually exclusive power_limit and on_off (Plan 39)
# ---------------------------------------------------------------------------


def test_pv_power_limit_and_on_off_together_raises() -> None:
    """PvCapabilitiesConfig rejects power_limit=True and on_off=True simultaneously.

    The two modes are mutually exclusive: a continuous inverter uses a power
    limit setpoint (power_limit), while a binary on/off inverter is either at
    zero or full forecast (on_off). No real hardware drives both registers at
    the same time.
    """
    with pytest.raises(ValidationError, match="mutually exclusive"):
        PvCapabilitiesConfig(power_limit=True, on_off=True)


# ---------------------------------------------------------------------------
# Plan 42 — Mode semantics: renamed and new capability/output fields
# ---------------------------------------------------------------------------


def test_battery_zero_exchange_false_by_default() -> None:
    """BatteryCapabilitiesConfig.zero_exchange defaults to False."""
    from mimirheim.config.schema import BatteryCapabilitiesConfig

    caps = BatteryCapabilitiesConfig()
    assert caps.zero_exchange is False


def test_battery_zero_exchange_requires_exchange_mode_topic() -> None:
    """BatteryConfig no longer raises when zero_exchange=True and exchange_mode=None.

    The per-device validator was removed in Plan 50. The output topic is now
    auto-derived at MimirheimConfig validation time. Direct construction of
    BatteryConfig leaves the topic as None until derivation runs.
    """
    from mimirheim.config.schema import BatteryCapabilitiesConfig, BatteryConfig, BatteryOutputsConfig

    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_charge_seg()],
        discharge_segments=[_charge_seg()],
        capabilities=BatteryCapabilitiesConfig(zero_exchange=True),
        outputs=BatteryOutputsConfig(exchange_mode=None),
    )
    assert cfg.capabilities.zero_exchange is True
    assert cfg.outputs.exchange_mode is None  # derived later by MimirheimConfig


def test_battery_zero_exchange_accepted_with_exchange_mode_topic() -> None:
    """BatteryConfig accepts zero_exchange=True when outputs.exchange_mode is set."""
    from mimirheim.config.schema import BatteryCapabilitiesConfig, BatteryConfig, BatteryOutputsConfig

    cfg = BatteryConfig(
        capacity_kwh=10.0,
        charge_segments=[_charge_seg()],
        discharge_segments=[_charge_seg()],
        capabilities=BatteryCapabilitiesConfig(zero_exchange=True),
        outputs=BatteryOutputsConfig(exchange_mode="mimir/battery/bat/exchange_mode"),
    )
    assert cfg.capabilities.zero_exchange is True
    assert cfg.outputs.exchange_mode == "mimir/battery/bat/exchange_mode"


def test_legacy_zero_export_mode_field_rejected_battery() -> None:
    """BatteryCapabilitiesConfig rejects the legacy zero_export_mode field name.

    The field was renamed to zero_exchange in Plan 42. extra='forbid' ensures
    that a config file still using the old name raises a ValidationError at
    startup rather than silently ignoring the stale value.
    """
    from mimirheim.config.schema import BatteryCapabilitiesConfig

    with pytest.raises(ValidationError):
        BatteryCapabilitiesConfig(zero_export_mode=True)


def test_ev_zero_exchange_requires_v2h() -> None:
    """EvCapabilitiesConfig rejects zero_exchange=True when v2h=False.

    Regulating grid exchange in both directions requires the ability to both
    charge and discharge. A charge-only EVSE cannot prevent export; it can
    only reduce import. Setting zero_exchange without v2h declares a
    physically impossible capability.
    """
    from mimirheim.config.schema import EvCapabilitiesConfig

    with pytest.raises(ValidationError, match="v2h"):
        EvCapabilitiesConfig(zero_exchange=True, v2h=False)


def test_ev_loadbalance_and_zero_exchange_both_true_accepted() -> None:
    """EvCapabilitiesConfig accepts loadbalance=True and zero_exchange=True together.

    The two modes are orthogonal. Hardware that supports bidirectional power
    may concurrently support a charge-following surplus mode. The arbitration
    engine (Plan 43) selects the active mode per step at runtime.
    """
    from mimirheim.config.schema import EvCapabilitiesConfig

    caps = EvCapabilitiesConfig(zero_exchange=True, v2h=True, loadbalance=True)
    assert caps.zero_exchange is True
    assert caps.loadbalance is True


def test_ev_loadbalance_requires_loadbalance_cmd_topic() -> None:
    """EvConfig no longer raises when loadbalance=True and loadbalance_cmd=None.

    The per-device validator was removed in Plan 50. The output topic is
    auto-derived at MimirheimConfig validation time.
    """
    from mimirheim.config.schema import EvCapabilitiesConfig, EvConfig, EvOutputsConfig

    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[_charge_seg()],
        capabilities=EvCapabilitiesConfig(loadbalance=True),
        outputs=EvOutputsConfig(loadbalance_cmd=None),
    )
    assert cfg.capabilities.loadbalance is True
    assert cfg.outputs.loadbalance_cmd is None  # derived later by MimirheimConfig


def test_ev_zero_exchange_requires_exchange_mode_topic() -> None:
    """EvConfig no longer raises when zero_exchange=True and exchange_mode=None.

    The per-device validator was removed in Plan 50. The output topic is
    auto-derived at MimirheimConfig validation time.
    """
    from mimirheim.config.schema import EvCapabilitiesConfig, EvConfig, EvOutputsConfig

    cfg = EvConfig(
        capacity_kwh=52.0,
        charge_segments=[_charge_seg()],
        capabilities=EvCapabilitiesConfig(zero_exchange=True, v2h=True),
        outputs=EvOutputsConfig(exchange_mode=None),
    )
    assert cfg.capabilities.zero_exchange is True
    assert cfg.outputs.exchange_mode is None  # derived later by MimirheimConfig


def test_legacy_zero_export_mode_field_rejected_ev() -> None:
    """EvCapabilitiesConfig rejects the legacy zero_export_mode field name.

    The field was removed in Plan 42. extra='forbid' ensures stale config
    files using the old name raise a ValidationError.
    """
    from mimirheim.config.schema import EvCapabilitiesConfig

    with pytest.raises(ValidationError):
        EvCapabilitiesConfig(zero_export_mode=True)


def test_pv_renamed_zero_export_accepted() -> None:
    """PvCapabilitiesConfig accepts the renamed zero_export field."""
    caps = PvCapabilitiesConfig(zero_export=True)
    assert caps.zero_export is True


def test_pv_legacy_zero_export_mode_field_rejected() -> None:
    """PvCapabilitiesConfig rejects the old zero_export_mode field name.

    The capability was renamed to zero_export in Plan 42 to align with the
    goal-oriented naming convention. extra='forbid' ensures the old name
    raises a ValidationError rather than being silently ignored.
    """
    with pytest.raises(ValidationError):
        PvCapabilitiesConfig(zero_export_mode=True)


# ---------------------------------------------------------------------------
# ControlConfig
# ---------------------------------------------------------------------------


def test_control_config_defaults_accepted() -> None:
    """ControlConfig can be constructed with no arguments; all fields have defaults."""
    from mimirheim.config.schema import ControlConfig

    cfg = ControlConfig()
    assert cfg.exchange_epsilon_kw == pytest.approx(0.05)
    assert cfg.headroom_margin_kw == pytest.approx(0.10)
    assert cfg.switch_delta == pytest.approx(0.05)
    assert cfg.min_enforcer_dwell_steps == 2


def test_control_config_custom_values_accepted() -> None:
    """ControlConfig accepts explicit values for all fields."""
    from mimirheim.config.schema import ControlConfig

    cfg = ControlConfig(
        exchange_epsilon_kw=0.10,
        headroom_margin_kw=0.20,
        switch_delta=0.02,
        min_enforcer_dwell_steps=4,
    )
    assert cfg.exchange_epsilon_kw == pytest.approx(0.10)
    assert cfg.min_enforcer_dwell_steps == 4


def test_control_config_extra_field_rejected() -> None:
    """ControlConfig rejects unknown fields (extra='forbid')."""
    from mimirheim.config.schema import ControlConfig

    with pytest.raises(ValidationError):
        ControlConfig(unknown_field=True)


def test_hioo_config_control_field_defaults() -> None:
    """MimirheimConfig.control defaults to a ControlConfig with standard values."""
    raw = {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.control.exchange_epsilon_kw == pytest.approx(0.05)
    assert config.control.min_enforcer_dwell_steps == 2


def test_hioo_config_control_field_overridden() -> None:
    """MimirheimConfig.control can be overridden in the YAML config dict."""
    raw = {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "outputs": {
            "schedule": "mimir/strategy/schedule",
            "current": "mimir/strategy/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "static_loads": {
            "base_load": {"topic_forecast": "mimir/input/base_load_forecast"},
        },
        "control": {
            "exchange_epsilon_kw": 0.10,
            "headroom_margin_kw": 0.30,
            "switch_delta": 0.01,
            "min_enforcer_dwell_steps": 5,
        },
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.control.exchange_epsilon_kw == pytest.approx(0.10)
    assert config.control.headroom_margin_kw == pytest.approx(0.30)
    assert config.control.min_enforcer_dwell_steps == 5


# ---------------------------------------------------------------------------
# ObjectivesConfig.exchange_shaping_weight
# ---------------------------------------------------------------------------


def test_exchange_shaping_weight_defaults_to_zero() -> None:
    """ObjectivesConfig.exchange_shaping_weight defaults to 0.0 (disabled)."""
    from mimirheim.config.schema import ObjectivesConfig

    cfg = ObjectivesConfig()
    assert cfg.exchange_shaping_weight == pytest.approx(0.0)


def test_exchange_shaping_weight_rejected_when_negative() -> None:
    """ObjectivesConfig.exchange_shaping_weight rejects negative values."""
    from mimirheim.config.schema import ObjectivesConfig

    with pytest.raises(ValidationError):
        ObjectivesConfig(exchange_shaping_weight=-1e-4)


# ---------------------------------------------------------------------------
# ReportingConfig
# ---------------------------------------------------------------------------


def test_reporting_config_defaults() -> None:
    """ReportingConfig defaults: disabled, no dir, 200 max dumps, notify_topic=None.

    notify_topic is None at ReportingConfig level; it is derived from
    mqtt.topic_prefix by MimirheimConfig._derive_global_topics at load time.
    """
    from mimirheim.config.schema import ReportingConfig

    cfg = ReportingConfig()
    assert cfg.enabled is False
    assert cfg.dump_dir is None
    assert cfg.max_dumps == 200
    assert cfg.notify_topic is None


def test_reporting_config_dir_required_when_enabled() -> None:
    """ReportingConfig raises when enabled=True but dump_dir is not set."""
    from mimirheim.config.schema import ReportingConfig

    with pytest.raises(ValidationError):
        ReportingConfig(enabled=True)


def test_reporting_config_enabled_with_dir(tmp_path: pytest.TempPathFactory) -> None:
    """ReportingConfig accepts enabled=True when dump_dir is provided."""
    from mimirheim.config.schema import ReportingConfig

    cfg = ReportingConfig(enabled=True, dump_dir=tmp_path)
    assert cfg.enabled is True
    assert cfg.dump_dir == tmp_path


def test_reporting_config_extra_field_rejected() -> None:
    """ReportingConfig rejects unknown fields (extra='forbid')."""
    from mimirheim.config.schema import ReportingConfig

    with pytest.raises(ValidationError):
        ReportingConfig(unknown_field=True)


def test_hioo_config_reporting_field_defaults() -> None:
    """MimirheimConfig.reporting defaults to a disabled ReportingConfig."""
    from mimirheim.config.schema import MimirheimConfig

    config = MimirheimConfig.model_validate(_minimal_hioo_config())
    assert config.reporting.enabled is False
    assert config.reporting.dump_dir is None


# ---------------------------------------------------------------------------
# Plan 49 — Global topic auto-derivation
# ---------------------------------------------------------------------------

def _minimal_hioo_config_no_outputs() -> dict:
    """Minimal MimirheimConfig dict with no outputs section and no explicit topics."""
    return {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "objectives": {},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
        "static_loads": {
            "base_load": {},
        },
    }


def test_outputs_fields_are_optional() -> None:
    """OutputsConfig can be constructed with all fields set to None."""
    from mimirheim.config.schema import OutputsConfig

    cfg = OutputsConfig()
    assert cfg.schedule is None
    assert cfg.current is None
    assert cfg.last_solve is None
    assert cfg.availability is None


def test_outputs_all_derived_from_prefix() -> None:
    """When outputs section is omitted, all four output topics are derived from mqtt.topic_prefix."""
    from mimirheim.config.schema import MimirheimConfig

    config = MimirheimConfig.model_validate(_minimal_hioo_config_no_outputs())
    assert config.outputs.schedule == "mimir/strategy/schedule"
    assert config.outputs.current == "mimir/strategy/current"
    assert config.outputs.last_solve == "mimir/status/last_solve"
    assert config.outputs.availability == "mimir/status/availability"


def test_outputs_explicit_topic_overrides_derived() -> None:
    """An explicit outputs.schedule value is kept; others are derived."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _minimal_hioo_config_no_outputs()
    raw["outputs"] = {"schedule": "custom/my_schedule"}
    config = MimirheimConfig.model_validate(raw)
    assert config.outputs.schedule == "custom/my_schedule"
    assert config.outputs.current == "mimir/strategy/current"
    assert config.outputs.last_solve == "mimir/status/last_solve"
    assert config.outputs.availability == "mimir/status/availability"


def test_outputs_custom_prefix_reflected_in_derived_topics() -> None:
    """When mqtt.topic_prefix is 'myhome', derived output topics use that prefix."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _minimal_hioo_config_no_outputs()
    raw["mqtt"]["topic_prefix"] = "myhome"
    config = MimirheimConfig.model_validate(raw)
    assert config.outputs.schedule == "myhome/strategy/schedule"
    assert config.outputs.current == "myhome/strategy/current"
    assert config.outputs.last_solve == "myhome/status/last_solve"
    assert config.outputs.availability == "myhome/status/availability"


def test_inputs_prices_derived_from_prefix() -> None:
    """When inputs section is omitted, inputs.prices is derived from prefix."""
    from mimirheim.config.schema import MimirheimConfig

    config = MimirheimConfig.model_validate(_minimal_hioo_config_no_outputs())
    assert config.inputs.prices == "mimir/input/prices"


def test_inputs_prices_explicit_override_preserved() -> None:
    """An explicit inputs.prices value is kept unchanged."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _minimal_hioo_config_no_outputs()
    raw["inputs"] = {"prices": "shared/nordpool/prices"}
    config = MimirheimConfig.model_validate(raw)
    assert config.inputs.prices == "shared/nordpool/prices"


def test_reporting_notify_topic_derived_from_prefix() -> None:
    """When reporting.notify_topic is not set, it is derived from prefix."""
    from mimirheim.config.schema import MimirheimConfig

    config = MimirheimConfig.model_validate(_minimal_hioo_config_no_outputs())
    assert config.reporting.notify_topic == "mimir/status/dump_available"


def test_reporting_notify_topic_explicit_override_preserved() -> None:
    """An explicit reporting.notify_topic value is kept unchanged."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _minimal_hioo_config_no_outputs()
    raw["reporting"] = {"notify_topic": "custom/reporter/notify"}
    config = MimirheimConfig.model_validate(raw)
    assert config.reporting.notify_topic == "custom/reporter/notify"


def test_reporting_notify_topic_uses_custom_prefix() -> None:
    """When mqtt.topic_prefix is 'home/v2', the notify topic uses that prefix."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _minimal_hioo_config_no_outputs()
    raw["mqtt"]["topic_prefix"] = "home/v2"
    config = MimirheimConfig.model_validate(raw)
    assert config.reporting.notify_topic == "home/v2/status/dump_available"


# ---------------------------------------------------------------------------
# Plan 50 — Device-level topic auto-derivation
# ---------------------------------------------------------------------------

def _minimal_battery(name: str = "bat") -> dict:
    return {
        "capacity_kwh": 10.0,
        "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
        "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
    }


def _base_no_outputs() -> dict:
    return {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim"},
    }


def test_battery_soc_topic_derived_from_prefix_and_name() -> None:
    """Battery SOC topic derived to {prefix}/input/battery/{name}/soc when unset."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {
        "battery_main": {
            **_minimal_battery(),
            "inputs": {"soc": {"unit": "percent"}},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["battery_main"].inputs.soc.topic == "mimir/input/battery/battery_main/soc"


def test_battery_inputs_omitted_topics_still_derived() -> None:
    """Battery SOC topic is derived when the inputs key is completely omitted."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {"bat": _minimal_battery()}
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat"].inputs is not None
    assert config.batteries["bat"].inputs.soc.unit == "percent"
    assert config.batteries["bat"].inputs.soc.topic == "mimir/input/battery/bat/soc"


def test_battery_soc_explicit_topic_preserved() -> None:
    """Explicit inputs.soc.topic is kept unchanged."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {
        "bat": {
            **_minimal_battery(),
            "inputs": {"soc": {"topic": "ha/sensor/bat_soc/state", "unit": "percent"}},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat"].inputs.soc.topic == "ha/sensor/bat_soc/state"


def test_battery_exchange_mode_derived() -> None:
    """outputs.exchange_mode derived to {prefix}/output/battery/{name}/exchange_mode."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {"bat": _minimal_battery()}
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat"].outputs.exchange_mode == "mimir/output/battery/bat/exchange_mode"


def test_battery_exchange_mode_also_derived_when_capability_disabled() -> None:
    """exchange_mode is derived even when zero_exchange=False."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {
        "bat": {**_minimal_battery(), "capabilities": {"zero_exchange": False}}
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat"].outputs.exchange_mode == "mimir/output/battery/bat/exchange_mode"


def test_battery_exchange_mode_explicit_topic_preserved() -> None:
    """Explicit outputs.exchange_mode is kept."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {
        "bat": {
            **_minimal_battery(),
            "capabilities": {"zero_exchange": True},
            "outputs": {"exchange_mode": "custom/bat/exchange"},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat"].outputs.exchange_mode == "custom/bat/exchange"


def test_battery_zero_exchange_no_longer_requires_explicit_topic() -> None:
    """capabilities.zero_exchange=True without explicit exchange_mode does not raise."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["batteries"] = {
        "bat": {**_minimal_battery(), "capabilities": {"zero_exchange": True}}
    }
    config = MimirheimConfig.model_validate(raw)
    # topic is derived automatically
    assert config.batteries["bat"].outputs.exchange_mode is not None


def test_ev_soc_topic_derived() -> None:
    """inputs.soc.topic derived to {prefix}/input/ev/{name}/soc."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "inputs": {"soc": {"unit": "kwh"}, "plugged_in_topic": None},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].inputs.soc.topic == "mimir/input/ev/ev1/soc"


def test_ev_inputs_omitted_topics_still_derived() -> None:
    """EV SOC and plugged_in topics are derived when the inputs key is completely omitted."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].inputs is not None
    assert config.ev_chargers["ev1"].inputs.soc.unit == "percent"
    assert config.ev_chargers["ev1"].inputs.soc.topic == "mimir/input/ev/ev1/soc"
    assert config.ev_chargers["ev1"].inputs.plugged_in_topic == "mimir/input/ev/ev1/plugged_in"


def test_ev_plugged_in_topic_derived() -> None:
    """inputs.plugged_in_topic derived to {prefix}/input/ev/{name}/plugged_in."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "inputs": {"soc": {"unit": "kwh"}},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].inputs.plugged_in_topic == "mimir/input/ev/ev1/plugged_in"


def test_ev_exchange_mode_derived() -> None:
    """outputs.exchange_mode derived to {prefix}/output/ev/{name}/exchange_mode."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].outputs.exchange_mode == "mimir/output/ev/ev1/exchange_mode"


def test_ev_loadbalance_cmd_derived() -> None:
    """outputs.loadbalance_cmd derived to {prefix}/output/ev/{name}/loadbalance."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].outputs.loadbalance_cmd == "mimir/output/ev/ev1/loadbalance"


def test_ev_zero_exchange_no_longer_requires_explicit_topic() -> None:
    """capabilities.zero_exchange=True without explicit exchange_mode does not raise."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"zero_exchange": True, "v2h": True},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].outputs.exchange_mode is not None


def test_ev_loadbalance_no_longer_requires_explicit_topic() -> None:
    """capabilities.loadbalance=True without explicit loadbalance_cmd does not raise."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["ev_chargers"] = {
        "ev1": {
            "capacity_kwh": 52.0,
            "charge_segments": [{"power_max_kw": 11.0, "efficiency": 0.93}],
            "capabilities": {"loadbalance": True},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.ev_chargers["ev1"].outputs.loadbalance_cmd is not None


def test_pv_forecast_topic_derived() -> None:
    """topic_forecast derived to {prefix}/input/pv/{name}/forecast."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {"pv_roof": {"max_power_kw": 4.5}}
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].topic_forecast == "mimir/input/pv/pv_roof/forecast"


def test_pv_forecast_explicit_topic_preserved() -> None:
    """Explicit topic_forecast on PV array is preserved."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {"pv_roof": {"max_power_kw": 4.5, "topic_forecast": "my/pv/forecast"}}
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].topic_forecast == "my/pv/forecast"


def test_pv_power_limit_kw_topic_derived() -> None:
    """outputs.power_limit_kw derived to {prefix}/output/pv/{name}/power_limit_kw."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {"pv_roof": {"max_power_kw": 4.5}}
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].outputs.power_limit_kw == "mimir/output/pv/pv_roof/power_limit_kw"


def test_pv_zero_export_mode_topic_derived() -> None:
    """outputs.zero_export_mode derived to {prefix}/output/pv/{name}/zero_export_mode."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {"pv_roof": {"max_power_kw": 4.5}}
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].outputs.zero_export_mode == "mimir/output/pv/pv_roof/zero_export_mode"


def test_pv_on_off_mode_topic_derived() -> None:
    """outputs.on_off_mode derived to {prefix}/output/pv/{name}/on_off_mode."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {"pv_roof": {"max_power_kw": 4.5}}
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].outputs.on_off_mode == "mimir/output/pv/pv_roof/on_off_mode"


def test_pv_on_off_no_longer_requires_explicit_topic() -> None:
    """capabilities.on_off=True without explicit on_off_mode does not raise."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["pv_arrays"] = {
        "pv_roof": {
            "max_power_kw": 4.5,
            "capabilities": {"on_off": True},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.pv_arrays["pv_roof"].outputs.on_off_mode is not None


def test_static_load_forecast_topic_derived() -> None:
    """topic_forecast derived to {prefix}/input/baseload/{name}/forecast."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["static_loads"] = {"base_load": {}}
    config = MimirheimConfig.model_validate(raw)
    assert config.static_loads["base_load"].topic_forecast == "mimir/input/baseload/base_load/forecast"


def test_hybrid_soc_topic_derived() -> None:
    """inputs.soc.topic derived to {prefix}/input/hybrid/{name}/soc."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["hybrid_inverters"] = {
        "hybrid_main": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 8.0,
            "inputs": {"soc": {"unit": "kwh"}},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.hybrid_inverters["hybrid_main"].inputs.soc.topic == "mimir/input/hybrid/hybrid_main/soc"


def test_hybrid_inputs_omitted_topics_still_derived() -> None:
    """Hybrid SOC topic is derived when the inputs key is completely omitted."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["hybrid_inverters"] = {
        "hybrid_main": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 8.0,
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.hybrid_inverters["hybrid_main"].inputs is not None
    assert config.hybrid_inverters["hybrid_main"].inputs.soc.unit == "percent"
    assert config.hybrid_inverters["hybrid_main"].inputs.soc.topic == "mimir/input/hybrid/hybrid_main/soc"


def test_hybrid_pv_forecast_topic_derived() -> None:
    """topic_pv_forecast derived to {prefix}/input/hybrid/{name}/pv_forecast."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["hybrid_inverters"] = {
        "hybrid_main": {
            "capacity_kwh": 10.0,
            "max_charge_kw": 5.0,
            "max_discharge_kw": 5.0,
            "max_pv_kw": 8.0,
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.hybrid_inverters["hybrid_main"].topic_pv_forecast == "mimir/input/hybrid/hybrid_main/pv_forecast"


def test_deferrable_window_earliest_topic_derived() -> None:
    """topic_window_earliest derived to {prefix}/input/deferrable/{name}/window_earliest."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["deferrable_loads"] = {
        "washer": {"power_profile": [2.0, 0.5, 0.5, 2.0]}
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.deferrable_loads["washer"].topic_window_earliest == "mimir/input/deferrable/washer/window_earliest"


def test_deferrable_window_latest_topic_derived() -> None:
    """topic_window_latest derived to {prefix}/input/deferrable/{name}/window_latest."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["deferrable_loads"] = {
        "washer": {"power_profile": [2.0, 0.5, 0.5, 2.0]}
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.deferrable_loads["washer"].topic_window_latest == "mimir/input/deferrable/washer/window_latest"


def test_deferrable_committed_start_topic_derived() -> None:
    """topic_committed_start_time derived to {prefix}/input/deferrable/{name}/committed_start."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["deferrable_loads"] = {
        "washer": {"power_profile": [2.0, 0.5, 0.5, 2.0]}
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.deferrable_loads["washer"].topic_committed_start_time == "mimir/input/deferrable/washer/committed_start"


def test_deferrable_recommended_start_topic_derived() -> None:
    """topic_recommended_start_time derived to {prefix}/output/deferrable/{name}/recommended_start."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["deferrable_loads"] = {
        "washer": {"power_profile": [2.0, 0.5, 0.5, 2.0]}
    }
    config = MimirheimConfig.model_validate(raw)
    assert (
        config.deferrable_loads["washer"].topic_recommended_start_time
        == "mimir/output/deferrable/washer/recommended_start"
    )


def test_deferrable_explicit_topic_preserves_override() -> None:
    """Explicit topic_window_earliest is not overwritten by derivation."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["deferrable_loads"] = {
        "washer": {
            "power_profile": [2.0, 0.5, 0.5, 2.0],
            "topic_window_earliest": "ha/input_datetime/wash_earliest/state",
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.deferrable_loads["washer"].topic_window_earliest == "ha/input_datetime/wash_earliest/state"
    # other topics still derived
    assert config.deferrable_loads["washer"].topic_window_latest == "mimir/input/deferrable/washer/window_latest"


def test_thermal_boiler_temp_topic_derived() -> None:
    """inputs.topic_current_temp derived to {prefix}/input/thermal_boiler/{name}/temp_c."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["thermal_boilers"] = {
        "dhw": {
            "volume_liters": 200.0,
            "elec_power_kw": 2.0,
            "setpoint_c": 60.0,
            "cooling_rate_k_per_hour": 2.0,
            "inputs": {},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.thermal_boilers["dhw"].inputs.topic_current_temp == "mimir/input/thermal_boiler/dhw/temp_c"


def test_thermal_boiler_inputs_omitted_topics_still_derived() -> None:
    """When inputs: is omitted entirely, topics are still derived via default_factory."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["thermal_boilers"] = {
        "dhw": {
            "volume_liters": 200.0,
            "elec_power_kw": 2.0,
            "setpoint_c": 60.0,
            "cooling_rate_k_per_hour": 2.0,
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.thermal_boilers["dhw"].inputs is not None
    assert config.thermal_boilers["dhw"].inputs.topic_current_temp == "mimir/input/thermal_boiler/dhw/temp_c"


def test_space_heating_heat_needed_topic_derived() -> None:
    """inputs.topic_heat_needed_kwh derived to {prefix}/input/space_heating/{name}/heat_needed_kwh."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["space_heating_hps"] = {
        "sh_hp": {
            "elec_power_kw": 8.0,
            "cop": 3.5,
            "inputs": {},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.space_heating_hps["sh_hp"].inputs.topic_heat_needed_kwh == "mimir/input/space_heating/sh_hp/heat_needed_kwh"


def test_space_heating_inputs_omitted_topics_still_derived() -> None:
    """When inputs: is omitted, topics are still derived via default_factory."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["space_heating_hps"] = {
        "sh_hp": {
            "elec_power_kw": 8.0,
            "cop": 3.5,
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.space_heating_hps["sh_hp"].inputs is not None
    assert config.space_heating_hps["sh_hp"].inputs.topic_heat_needed_kwh == "mimir/input/space_heating/sh_hp/heat_needed_kwh"
    assert config.space_heating_hps["sh_hp"].inputs.topic_heat_produced_today_kwh == "mimir/input/space_heating/sh_hp/heat_produced_today_kwh"


def test_space_heating_heat_produced_topic_derived() -> None:
    """inputs.topic_heat_produced_today_kwh derived to {prefix}/input/space_heating/{name}/heat_produced_today_kwh."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["space_heating_hps"] = {
        "sh_hp": {
            "elec_power_kw": 8.0,
            "cop": 3.5,
            "inputs": {},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.space_heating_hps["sh_hp"].inputs.topic_heat_produced_today_kwh == "mimir/input/space_heating/sh_hp/heat_produced_today_kwh"


def test_space_heating_btm_indoor_temp_topic_derived() -> None:
    """building_thermal.inputs.topic_current_indoor_temp_c derived."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["space_heating_hps"] = {
        "sh_hp": {
            "elec_power_kw": 8.0,
            "cop": 3.5,
            "inputs": {},
            "building_thermal": {
                "thermal_capacity_kwh_per_k": 8.0,
                "heat_loss_coeff_kw_per_k": 0.6,
                "inputs": {},
            },
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert (
        config.space_heating_hps["sh_hp"].building_thermal.inputs.topic_current_indoor_temp_c
        == "mimir/input/space_heating/sh_hp/btm/indoor_temp_c"
    )


def test_space_heating_btm_outdoor_forecast_topic_derived() -> None:
    """building_thermal.inputs.topic_outdoor_temp_forecast_c derived."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["space_heating_hps"] = {
        "sh_hp": {
            "elec_power_kw": 8.0,
            "cop": 3.5,
            "inputs": {},
            "building_thermal": {
                "thermal_capacity_kwh_per_k": 8.0,
                "heat_loss_coeff_kw_per_k": 0.6,
                "inputs": {},
            },
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert (
        config.space_heating_hps["sh_hp"].building_thermal.inputs.topic_outdoor_temp_forecast_c
        == "mimir/input/space_heating/sh_hp/btm/outdoor_forecast_c"
    )


def test_combi_hp_temp_topic_derived() -> None:
    """inputs.topic_current_temp derived to {prefix}/input/combi_hp/{name}/temp_c."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["combi_heat_pumps"] = {
        "chp": {
            "elec_power_kw": 6.0,
            "cop_dhw": 2.8,
            "cop_sh": 3.8,
            "volume_liters": 200.0,
            "setpoint_c": 55.0,
            "cooling_rate_k_per_hour": 2.0,
            "inputs": {},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.combi_heat_pumps["chp"].inputs.topic_current_temp == "mimir/input/combi_hp/chp/temp_c"


def test_combi_hp_inputs_omitted_topics_still_derived() -> None:
    """When inputs: is omitted, topics are still derived via default_factory."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["combi_heat_pumps"] = {
        "chp": {
            "elec_power_kw": 6.0,
            "cop_dhw": 2.8,
            "cop_sh": 3.8,
            "volume_liters": 200.0,
            "setpoint_c": 55.0,
            "cooling_rate_k_per_hour": 2.0,
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.combi_heat_pumps["chp"].inputs is not None
    assert config.combi_heat_pumps["chp"].inputs.topic_current_temp == "mimir/input/combi_hp/chp/temp_c"
    assert config.combi_heat_pumps["chp"].inputs.topic_heat_needed_kwh == "mimir/input/combi_hp/chp/sh_heat_needed_kwh"


def test_combi_hp_sh_heat_needed_topic_derived() -> None:
    """inputs.topic_heat_needed_kwh derived to {prefix}/input/combi_hp/{name}/sh_heat_needed_kwh."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["combi_heat_pumps"] = {
        "chp": {
            "elec_power_kw": 6.0,
            "cop_dhw": 2.8,
            "cop_sh": 3.8,
            "volume_liters": 200.0,
            "setpoint_c": 55.0,
            "cooling_rate_k_per_hour": 2.0,
            "inputs": {},
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.combi_heat_pumps["chp"].inputs.topic_heat_needed_kwh == "mimir/input/combi_hp/chp/sh_heat_needed_kwh"


def test_combi_hp_btm_topics_derived() -> None:
    """combi_hp building_thermal.inputs topics derived under combi_hp/{name}/btm/."""
    from mimirheim.config.schema import MimirheimConfig

    raw = _base_no_outputs()
    raw["combi_heat_pumps"] = {
        "chp": {
            "elec_power_kw": 6.0,
            "cop_dhw": 2.8,
            "cop_sh": 3.8,
            "volume_liters": 200.0,
            "setpoint_c": 55.0,
            "cooling_rate_k_per_hour": 2.0,
            "inputs": {},
            "building_thermal": {
                "thermal_capacity_kwh_per_k": 8.0,
                "heat_loss_coeff_kw_per_k": 0.6,
                "inputs": {},
            },
        }
    }
    config = MimirheimConfig.model_validate(raw)
    assert (
        config.combi_heat_pumps["chp"].building_thermal.inputs.topic_current_indoor_temp_c
        == "mimir/input/combi_hp/chp/btm/indoor_temp_c"
    )
    assert (
        config.combi_heat_pumps["chp"].building_thermal.inputs.topic_outdoor_temp_forecast_c
        == "mimir/input/combi_hp/chp/btm/outdoor_forecast_c"
    )


def test_custom_prefix_propagates_to_all_device_topics() -> None:
    """When mqtt.topic_prefix is 'home/v2', all derived device topics use that prefix."""
    from mimirheim.config.schema import MimirheimConfig

    raw = {
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "mqtt": {"host": "localhost", "client_id": "mimirheim", "topic_prefix": "home/v2"},
        "batteries": {
            "bat1": {
                "capacity_kwh": 10.0,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "inputs": {"soc": {"unit": "kwh"}},
            }
        },
        "static_loads": {"bl": {}},
    }
    config = MimirheimConfig.model_validate(raw)
    assert config.batteries["bat1"].inputs.soc.topic == "home/v2/input/battery/bat1/soc"
    assert config.batteries["bat1"].outputs.exchange_mode == "home/v2/output/battery/bat1/exchange_mode"
    assert config.static_loads["bl"].topic_forecast == "home/v2/input/baseload/bl/forecast"


# ---------------------------------------------------------------------------
# MqttConfig TLS fields
# ---------------------------------------------------------------------------

def test_mqtt_tls_defaults_to_false() -> None:
    """tls and tls_allow_insecure both default to False."""
    from mimirheim.config.schema import MqttConfig

    cfg = MqttConfig.model_validate({"host": "localhost", "client_id": "mimirheim"})
    assert cfg.tls is False
    assert cfg.tls_allow_insecure is False


def test_mqtt_tls_true_accepted() -> None:
    """tls: true enables TLS independently of tls_allow_insecure."""
    from mimirheim.config.schema import MqttConfig

    cfg = MqttConfig.model_validate({"host": "localhost", "client_id": "mimirheim", "tls": True})
    assert cfg.tls is True
    assert cfg.tls_allow_insecure is False


def test_mqtt_tls_allow_insecure_without_tls_accepted() -> None:
    """tls_allow_insecure can be set even when tls is False (harmless, no effect at runtime)."""
    from mimirheim.config.schema import MqttConfig

    cfg = MqttConfig.model_validate(
        {"host": "localhost", "client_id": "mimirheim", "tls": False, "tls_allow_insecure": True}
    )
    assert cfg.tls is False
    assert cfg.tls_allow_insecure is True


def test_mqtt_tls_and_insecure_together_accepted() -> None:
    """tls: true with tls_allow_insecure: true is the self-signed-cert configuration."""
    from mimirheim.config.schema import MqttConfig

    cfg = MqttConfig.model_validate(
        {"host": "localhost", "client_id": "mimirheim", "tls": True, "tls_allow_insecure": True}
    )
    assert cfg.tls is True
    assert cfg.tls_allow_insecure is True


def test_mqtt_unknown_field_rejected() -> None:
    """extra='forbid' rejects unknown fields on MqttConfig."""
    import pytest
    from pydantic import ValidationError
    from mimirheim.config.schema import MqttConfig

    with pytest.raises(ValidationError):
        MqttConfig.model_validate(
            {"host": "localhost", "client_id": "mimirheim", "enable_ssl": True}
        )


# ---------------------------------------------------------------------------
# HybridInverterConfig — new fields added in plan 54
# ---------------------------------------------------------------------------


def _minimal_hybrid(**overrides) -> dict:
    base = {
        "capacity_kwh": 10.0,
        "min_soc_kwh": 1.0,
        "max_charge_kw": 5.0,
        "max_discharge_kw": 5.0,
        "max_pv_kw": 6.0,
    }
    base.update(overrides)
    return base


class TestHybridInverterConfigPlan54:
    def test_minimal_config_accepted(self) -> None:
        """Existing minimal config with no new fields parses correctly."""
        cfg = HybridInverterConfig.model_validate(_minimal_hybrid())
        assert cfg.capacity_kwh == 10.0

    def test_optimal_lower_soc_kwh_accepted(self) -> None:
        """optimal_lower_soc_kwh between min_soc_kwh and capacity_kwh is accepted."""
        cfg = HybridInverterConfig.model_validate(
            _minimal_hybrid(optimal_lower_soc_kwh=5.0)
        )
        assert cfg.optimal_lower_soc_kwh == 5.0

    def test_optimal_lower_soc_kwh_below_min_rejected(self) -> None:
        """optimal_lower_soc_kwh < min_soc_kwh raises ValidationError."""
        with pytest.raises(ValidationError):
            HybridInverterConfig.model_validate(
                _minimal_hybrid(min_soc_kwh=3.0, optimal_lower_soc_kwh=2.0)
            )

    def test_optimal_lower_soc_kwh_above_capacity_rejected(self) -> None:
        """optimal_lower_soc_kwh > capacity_kwh raises ValidationError."""
        with pytest.raises(ValidationError):
            HybridInverterConfig.model_validate(
                _minimal_hybrid(capacity_kwh=10.0, optimal_lower_soc_kwh=11.0)
            )

    def test_derating_fields_accepted_when_paired(self) -> None:
        """reduce_charge_above_soc_kwh + reduce_charge_min_kw accepted together."""
        cfg = HybridInverterConfig.model_validate(
            _minimal_hybrid(
                reduce_charge_above_soc_kwh=7.0,
                reduce_charge_min_kw=1.0,
            )
        )
        assert cfg.reduce_charge_above_soc_kwh == 7.0

    def test_derating_only_one_charge_field_rejected(self) -> None:
        """reduce_charge_above_soc_kwh without reduce_charge_min_kw raises."""
        with pytest.raises(ValidationError):
            HybridInverterConfig.model_validate(
                _minimal_hybrid(reduce_charge_above_soc_kwh=7.0)
            )

    def test_derating_charge_soc_out_of_range_rejected(self) -> None:
        """reduce_charge_above_soc_kwh outside (min_soc, capacity) raises."""
        with pytest.raises(ValidationError):
            HybridInverterConfig.model_validate(
                _minimal_hybrid(
                    min_soc_kwh=1.0,
                    capacity_kwh=10.0,
                    # Equal to capacity_kwh — not strictly inside.
                    reduce_charge_above_soc_kwh=10.0,
                    reduce_charge_min_kw=1.0,
                )
            )

    def test_min_charge_kw_accepted(self) -> None:
        """min_charge_kw >= 0 is accepted."""
        cfg = HybridInverterConfig.model_validate(_minimal_hybrid(min_charge_kw=1.5))
        assert cfg.min_charge_kw == 1.5

    def test_min_discharge_kw_accepted(self) -> None:
        """min_discharge_kw >= 0 is accepted."""
        cfg = HybridInverterConfig.model_validate(_minimal_hybrid(min_discharge_kw=0.5))
        assert cfg.min_discharge_kw == 0.5

    def test_capabilities_defaults(self) -> None:
        """HybridInverterConfig.capabilities has zero_exchange=False by default."""
        cfg = HybridInverterConfig.model_validate(_minimal_hybrid())
        assert cfg.capabilities.zero_exchange is False

    def test_outputs_defaults(self) -> None:
        """HybridInverterConfig.outputs has exchange_mode=None by default."""
        cfg = HybridInverterConfig.model_validate(_minimal_hybrid())
        assert cfg.outputs.exchange_mode is None

    def test_unknown_field_rejected(self) -> None:
        """extra='forbid' rejects unknown fields."""
        with pytest.raises(ValidationError):
            HybridInverterConfig.model_validate(
                _minimal_hybrid(nonexistent_field=True)
            )
