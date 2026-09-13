"""FormSpec authored for MimirheimConfig, mimirheim core's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``mimirheim.config.schema`` per
ADR-0002 (``mimirheim_shared/docs/adr``): ``MimirheimConfig`` itself carries no
FormSpec-level presentation metadata. This module is what mimirheim core's
``describe()`` step (``mimirheim.io.config_service``) combines with
``MimirheimConfig`` to build its Config Service Descriptor.

Per ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete`` checks a
FormSpec recursively against its model, at every depth. This module gives every
nested pydantic model reachable from ``MimirheimConfig`` its own FormSpec, so
the whole tree recurses down to scalars and enum selects with no
``shape_override`` anywhere: every device section, every device's own
capability/input/output sub-models, and every core config section. FormSpecs
are defined bottom-up (innermost model first) so a later constant can
reference an earlier one, and the same constant is reused wherever the same
pydantic model is nested more than once (e.g. ``SOC_TOPIC_CONFIG_FORM_SPEC``
under both battery and EV inputs, ``BATTERY_INPUTS_FORM_SPEC`` under both
``BatteryConfig`` and ``HybridInverterConfig``, ``BUILDING_THERMAL_CONFIG_FORM_SPEC``
under both space heating and combi heat pump) rather than re-authored per site.
"""

from __future__ import annotations

from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

# ---------------------------------------------------------------------------
# Battery: leaf models
# ---------------------------------------------------------------------------

EFFICIENCY_SEGMENT_FORM_SPEC = FormSpec(
    fields={
        "power_max_kw": FieldSpec(
            label="Max power (kW)", description="Maximum power through this segment in kW.", tier=Tier.BASIC
        ),
        "efficiency": FieldSpec(
            label="Efficiency", description="Round-trip efficiency fraction [0, 1].", tier=Tier.BASIC
        ),
    }
)

EFFICIENCY_BREAKPOINT_FORM_SPEC = FormSpec(
    fields={
        "power_kw": FieldSpec(
            label="Power (kW)", description="AC power in kW at this breakpoint.", tier=Tier.BASIC
        ),
        "efficiency": FieldSpec(
            label="Efficiency", description="Round-trip efficiency fraction.", tier=Tier.BASIC
        ),
    }
)

SOC_TOPIC_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "topic": FieldSpec(
            label="SOC MQTT topic",
            description="MQTT topic publishing the SOC value. Defaults to a derived path when not set.",
            tier=Tier.EXPERT,
        ),
        "unit": FieldSpec(
            label="SOC unit",
            description=(
                "Unit of the published value. 'percent' when the sensor publishes a "
                "0-100 percentage; 'kwh' when it publishes an absolute energy value."
            ),
            tier=Tier.EXPERT,
            option_labels={"kwh": "kWh", "percent": "Percent"},
        ),
    }
)

BATTERY_CAPABILITIES_FORM_SPEC = FormSpec(
    fields={
        "staged_power": FieldSpec(
            label="Staged power control",
            description="Hardware accepts only discrete power stages, not continuous values.",
            tier=Tier.EXPERT,
        ),
        "zero_exchange": FieldSpec(
            label="Zero-exchange mode",
            description="Battery inverter supports a closed-loop zero-exchange firmware mode.",
            tier=Tier.EXPERT,
        ),
    }
)

BATTERY_OUTPUTS_FORM_SPEC = FormSpec(
    fields={
        "exchange_mode": FieldSpec(
            label="Exchange mode topic",
            description="MQTT topic for the closed-loop zero-exchange mode boolean flag.",
            tier=Tier.EXPERT,
        ),
        "soc_ratchet": FieldSpec(
            label="SOC ratchet status topic",
            description="MQTT topic for the full-charge policy status, published retained and read back on startup.",
            tier=Tier.EXPERT,
        ),
    }
)

SOC_RATCHET_FORM_SPEC = FormSpec(
    fields={
        "enabled": FieldSpec(
            label="Enable SOC ratchet",
            description="Enable the periodic full-charge policy. Off by default.",
            tier=Tier.BASIC,
        ),
        "target_interval_days": FieldSpec(
            label="Target interval (days)",
            description="Days the battery may go without a full charge before the floor climbs.",
            tier=Tier.BASIC,
        ),
        "full_threshold_pct": FieldSpec(
            label="Full threshold (%)",
            description="Observed SOC, in percent of capacity, that counts as a full charge.",
            tier=Tier.BASIC,
        ),
        "step_pct": FieldSpec(
            label="Ratchet step (%)",
            description="Percentage points of capacity added to the floor per missed interval.",
            tier=Tier.EXPERT,
        ),
        "cap_pct": FieldSpec(
            label="Ratchet cap (%)",
            description="Ceiling on the dynamic floor, in percent of capacity.",
            tier=Tier.EXPERT,
        ),
        "target_pct": FieldSpec(
            label="Charge target (%)",
            description="SOC, in percent of capacity, the plan is asked to reach and hold.",
            tier=Tier.EXPERT,
        ),
        "hold_hours": FieldSpec(
            label="Hold at target (hours)",
            description="Hours the SOC must stay at or above the target once reached. 0 = a single touch.",
            tier=Tier.EXPERT,
        ),
    }
)

BATTERY_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "soc": FieldSpec(
            label="SOC topic config",
            description="Battery state-of-charge MQTT topic configuration. Defaults to derived topic with percent unit.",
            tier=Tier.EXPERT,
            nested_form_spec=SOC_TOPIC_CONFIG_FORM_SPEC,
        ),
    }
)

BATTERY_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "capacity_kwh": FieldSpec(
            label="Capacity (kWh)", description="Usable capacity in kWh.", tier=Tier.BASIC
        ),
        "min_soc_kwh": FieldSpec(
            label="Minimum SOC (kWh)", description="Minimum SOC in kWh.", tier=Tier.BASIC
        ),
        "charge_segments": FieldSpec(
            label="Charge segments",
            description=(
                "Stacked-segment efficiency model for charging. Mutually exclusive with "
                "charge_efficiency_curve."
            ),
            tier=Tier.BASIC,
            nested_form_spec=EFFICIENCY_SEGMENT_FORM_SPEC,
            visible_if=Comparison(
                field="charge_efficiency_curve", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "discharge_segments": FieldSpec(
            label="Discharge segments",
            description=(
                "Stacked-segment efficiency model for discharging. Mutually exclusive with "
                "discharge_efficiency_curve."
            ),
            tier=Tier.BASIC,
            nested_form_spec=EFFICIENCY_SEGMENT_FORM_SPEC,
            visible_if=Comparison(
                field="discharge_efficiency_curve", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "charge_efficiency_curve": FieldSpec(
            label="Charge efficiency curve",
            description="SOS2 piecewise-linear efficiency curve for charging. Mutually exclusive with charge_segments.",
            tier=Tier.EXPERT,
            nested_form_spec=EFFICIENCY_BREAKPOINT_FORM_SPEC,
            visible_if=Comparison(
                field="charge_segments", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "discharge_efficiency_curve": FieldSpec(
            label="Discharge efficiency curve",
            description="SOS2 piecewise-linear efficiency curve for discharging. Mutually exclusive with discharge_segments.",
            tier=Tier.EXPERT,
            nested_form_spec=EFFICIENCY_BREAKPOINT_FORM_SPEC,
            visible_if=Comparison(
                field="discharge_segments", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Battery degradation cost per kWh of energy throughput, in EUR.",
            tier=Tier.BASIC,
        ),
        "optimal_lower_soc_kwh": FieldSpec(
            label="Preferred minimum SOC (kWh)",
            description="Preferred minimum state of charge in kWh, enforced as a soft lower bound.",
            tier=Tier.EXPERT,
        ),
        "soc_low_penalty_eur_per_kwh_h": FieldSpec(
            label="Low SOC penalty (€/kWh·h)",
            description="Penalty rate for SOC below optimal_lower_soc_kwh, in EUR per kWh of deficit per hour.",
            tier=Tier.EXPERT,
        ),
        "reduce_charge_above_soc_kwh": FieldSpec(
            label="Derate charge above SOC (kWh)",
            description="SOC level above which max charge power begins to decrease.",
            tier=Tier.EXPERT,
        ),
        "reduce_charge_min_kw": FieldSpec(
            label="Derated charge minimum (kW)",
            description="Max charge power in kW at full capacity, once derating has kicked in.",
            tier=Tier.EXPERT,
        ),
        "reduce_discharge_below_soc_kwh": FieldSpec(
            label="Derate discharge below SOC (kWh)",
            description="SOC level below which max discharge power begins to decrease.",
            tier=Tier.EXPERT,
        ),
        "reduce_discharge_min_kw": FieldSpec(
            label="Derated discharge minimum (kW)",
            description="Max discharge power in kW at minimum SOC, once derating has kicked in.",
            tier=Tier.EXPERT,
        ),
        "capabilities": FieldSpec(
            label="Hardware capabilities",
            description="Hardware capability flags.",
            tier=Tier.EXPERT,
            nested_form_spec=BATTERY_CAPABILITIES_FORM_SPEC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration for battery state readings.",
            tier=Tier.EXPERT,
            nested_form_spec=BATTERY_INPUTS_FORM_SPEC,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic configuration for battery control signals.",
            tier=Tier.EXPERT,
            nested_form_spec=BATTERY_OUTPUTS_FORM_SPEC,
        ),
        "soc_ratchet": FieldSpec(
            label="SOC ratchet",
            description="Periodic full-charge policy. Disabled by default.",
            tier=Tier.EXPERT,
            nested_form_spec=SOC_RATCHET_FORM_SPEC,
        ),
        "min_charge_kw": FieldSpec(
            label="Minimum charge power (kW)",
            description="Minimum charge power in kW when the battery is actively charging.",
            tier=Tier.EXPERT,
        ),
        "min_discharge_kw": FieldSpec(
            label="Minimum discharge power (kW)",
            description="Minimum discharge power in kW when the battery is actively discharging.",
            tier=Tier.EXPERT,
        ),
    }
)

# ---------------------------------------------------------------------------
# EV charger
# ---------------------------------------------------------------------------

EV_CAPABILITIES_FORM_SPEC = FormSpec(
    fields={
        "staged_power": FieldSpec(
            label="Staged power control",
            description="Hardware accepts only discrete power stages, not continuous values.",
            tier=Tier.EXPERT,
        ),
        "zero_exchange": FieldSpec(
            label="Zero-exchange mode",
            description="EVSE supports a closed-loop zero-exchange firmware mode. Requires v2h=True.",
            tier=Tier.EXPERT,
        ),
        "v2h": FieldSpec(
            label="Vehicle-to-home (V2H)",
            description="Hardware supports vehicle-to-home discharge (bidirectional power flow).",
            tier=Tier.EXPERT,
        ),
        "loadbalance": FieldSpec(
            label="Load balance mode",
            description="EVSE firmware supports autonomous charge-only excess-PV following mode.",
            tier=Tier.EXPERT,
        ),
    }
)

EV_OUTPUTS_FORM_SPEC = FormSpec(
    fields={
        "exchange_mode": FieldSpec(
            label="Exchange mode topic",
            description="MQTT topic for the closed-loop zero-exchange mode boolean flag.",
            tier=Tier.EXPERT,
        ),
        "loadbalance_cmd": FieldSpec(
            label="Load balance topic",
            description="MQTT topic for the load-balance mode enable boolean flag.",
            tier=Tier.EXPERT,
        ),
    }
)

EV_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "soc": FieldSpec(
            label="SOC topic config",
            description="Vehicle SOC MQTT topic configuration. Defaults to derived topic with percent unit.",
            tier=Tier.EXPERT,
            nested_form_spec=SOC_TOPIC_CONFIG_FORM_SPEC,
        ),
        "plugged_in_topic": FieldSpec(
            label="Plug state topic",
            description="MQTT topic for the EV plug state.",
            tier=Tier.EXPERT,
        ),
    }
)

EV_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "capacity_kwh": FieldSpec(
            label="Vehicle capacity (kWh)", description="Vehicle battery capacity in kWh.", tier=Tier.BASIC
        ),
        "min_soc_kwh": FieldSpec(
            label="Minimum SOC (kWh)", description="Minimum SOC in kWh.", tier=Tier.BASIC
        ),
        "charge_segments": FieldSpec(
            label="Charge segments",
            description="Piecewise efficiency segments for charging.",
            tier=Tier.BASIC,
            nested_form_spec=EFFICIENCY_SEGMENT_FORM_SPEC,
        ),
        "discharge_segments": FieldSpec(
            label="Discharge segments (V2H)",
            description="Piecewise efficiency segments for V2H discharge. Empty = no V2H.",
            tier=Tier.BASIC,
            nested_form_spec=EFFICIENCY_SEGMENT_FORM_SPEC,
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Vehicle battery degradation cost per kWh of energy throughput, in EUR.",
            tier=Tier.BASIC,
        ),
        "capabilities": FieldSpec(
            label="Hardware capabilities",
            description="Hardware capability flags.",
            tier=Tier.EXPERT,
            nested_form_spec=EV_CAPABILITIES_FORM_SPEC,
        ),
        "min_charge_kw": FieldSpec(
            label="Minimum EVSE charge power (kW)",
            description="Minimum charge power in kW when the EVSE is actively charging.",
            tier=Tier.EXPERT,
        ),
        "min_discharge_kw": FieldSpec(
            label="Minimum V2H discharge power (kW)",
            description="Minimum V2H discharge power in kW when the EVSE is actively discharging.",
            tier=Tier.EXPERT,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic configuration.",
            tier=Tier.EXPERT,
            nested_form_spec=EV_OUTPUTS_FORM_SPEC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration for EV state readings.",
            tier=Tier.EXPERT,
            nested_form_spec=EV_INPUTS_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# PV array
# ---------------------------------------------------------------------------

PV_CAPABILITIES_FORM_SPEC = FormSpec(
    fields={
        "power_limit": FieldSpec(
            label="Power limit control",
            description="Inverter accepts a continuous production limit setpoint in kW.",
            tier=Tier.EXPERT,
        ),
        "zero_export": FieldSpec(
            label="Zero-export mode",
            description="Inverter has a boolean zero-export mode register.",
            tier=Tier.EXPERT,
        ),
        "on_off": FieldSpec(
            label="On/off control",
            description="Inverter supports discrete on/off control. Mutually exclusive with power_limit.",
            tier=Tier.EXPERT,
        ),
    }
)

PV_OUTPUTS_FORM_SPEC = FormSpec(
    fields={
        "power_limit_kw": FieldSpec(
            label="Power limit topic",
            description="MQTT topic for the production limit setpoint in kW.",
            tier=Tier.EXPERT,
        ),
        "zero_export_mode": FieldSpec(
            label="Zero-export mode topic",
            description="MQTT topic for the zero-export mode boolean command.",
            tier=Tier.EXPERT,
        ),
        "on_off_mode": FieldSpec(
            label="On/off mode topic",
            description="MQTT topic for the on/off command.",
            tier=Tier.EXPERT,
        ),
        "is_curtailed": FieldSpec(
            label="Is curtailed topic",
            description="MQTT topic for the curtailment status boolean.",
            tier=Tier.EXPERT,
        ),
    }
)

PV_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "max_power_kw": FieldSpec(
            label="Peak power (kW)", description="Array peak output in kW.", tier=Tier.BASIC
        ),
        "topic_forecast": FieldSpec(
            label="Forecast topic",
            description="MQTT topic for the per-step PV power forecast in kW.",
            tier=Tier.EXPERT,
        ),
        "production_stages": FieldSpec(
            label="Production stages (kW)",
            description=(
                "Discrete power levels the inverter accepts, in ascending order, starting "
                "with 0.0. Mutually exclusive with capabilities.power_limit and capabilities.on_off."
            ),
            tier=Tier.EXPERT,
        ),
        "capabilities": FieldSpec(
            label="Hardware capabilities",
            description="Hardware capability flags.",
            tier=Tier.EXPERT,
            nested_form_spec=PV_CAPABILITIES_FORM_SPEC,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic names for PV control commands.",
            tier=Tier.EXPERT,
            nested_form_spec=PV_OUTPUTS_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Hybrid inverter (integrated PV + battery DC bus)
# ---------------------------------------------------------------------------

HYBRID_INVERTER_CAPABILITIES_FORM_SPEC = FormSpec(
    fields={
        "zero_exchange": FieldSpec(
            label="Zero-exchange mode",
            description="Inverter supports a closed-loop zero-exchange firmware mode.",
            tier=Tier.EXPERT,
        ),
    }
)

HYBRID_INVERTER_OUTPUTS_FORM_SPEC = FormSpec(
    fields={
        "exchange_mode": FieldSpec(
            label="Exchange mode topic",
            description="MQTT topic for the closed-loop zero-exchange mode boolean flag.",
            tier=Tier.EXPERT,
        ),
    }
)

HYBRID_INVERTER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "capacity_kwh": FieldSpec(
            label="Battery capacity (kWh)", description="Usable battery capacity in kWh.", tier=Tier.BASIC
        ),
        "min_soc_kwh": FieldSpec(
            label="Minimum SOC (kWh)", description="Minimum SOC in kWh.", tier=Tier.BASIC
        ),
        "max_charge_kw": FieldSpec(
            label="Max charge power (kW)",
            description="Maximum DC charge power to battery cells in kW.",
            tier=Tier.BASIC,
        ),
        "max_discharge_kw": FieldSpec(
            label="Max discharge power (kW)",
            description="Maximum DC discharge power from battery cells in kW.",
            tier=Tier.BASIC,
        ),
        "battery_charge_efficiency": FieldSpec(
            label="Battery charge efficiency",
            description="Efficiency of battery charge process (DC bus to cell storage).",
            tier=Tier.EXPERT,
        ),
        "battery_discharge_efficiency": FieldSpec(
            label="Battery discharge efficiency",
            description="Efficiency of battery discharge process (cell to DC bus).",
            tier=Tier.EXPERT,
        ),
        "inverter_efficiency": FieldSpec(
            label="Inverter efficiency",
            description="AC-to-DC and DC-to-AC inverter conversion efficiency.",
            tier=Tier.EXPERT,
        ),
        "max_pv_kw": FieldSpec(
            label="PV peak power (kW)",
            description="Peak PV power at MPPT input in kW. Used to clip forecasts.",
            tier=Tier.BASIC,
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Battery degradation cost per kWh of DC throughput (charge + discharge), in EUR.",
            tier=Tier.BASIC,
        ),
        "topic_pv_forecast": FieldSpec(
            label="PV forecast topic",
            description="MQTT topic for per-step PV DC power forecast in kW.",
            tier=Tier.EXPERT,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration for live battery SOC readings.",
            tier=Tier.EXPERT,
            nested_form_spec=BATTERY_INPUTS_FORM_SPEC,
        ),
        "optimal_lower_soc_kwh": FieldSpec(
            label="Preferred minimum SOC (kWh)",
            description="Preferred minimum state of charge in kWh, enforced as a soft lower bound.",
            tier=Tier.EXPERT,
        ),
        "soc_low_penalty_eur_per_kwh_h": FieldSpec(
            label="Low SOC penalty (€/kWh·h)",
            description="Penalty rate for SOC below optimal_lower_soc_kwh, in EUR per kWh of deficit per hour.",
            tier=Tier.EXPERT,
        ),
        "reduce_charge_above_soc_kwh": FieldSpec(
            label="Derate charge above SOC (kWh)",
            description="SOC level above which max charge power begins to decrease.",
            tier=Tier.EXPERT,
        ),
        "reduce_charge_min_kw": FieldSpec(
            label="Derated charge minimum (kW)",
            description="Max charge power in kW at full capacity, once derating has kicked in.",
            tier=Tier.EXPERT,
        ),
        "reduce_discharge_below_soc_kwh": FieldSpec(
            label="Derate discharge below SOC (kWh)",
            description="SOC level below which max discharge power begins to decrease.",
            tier=Tier.EXPERT,
        ),
        "reduce_discharge_min_kw": FieldSpec(
            label="Derated discharge minimum (kW)",
            description="Max discharge power in kW at minimum SOC, once derating has kicked in.",
            tier=Tier.EXPERT,
        ),
        "min_charge_kw": FieldSpec(
            label="Minimum charge power (kW)",
            description="Minimum DC charge power in kW when the inverter is actively charging.",
            tier=Tier.EXPERT,
        ),
        "min_discharge_kw": FieldSpec(
            label="Minimum discharge power (kW)",
            description="Minimum DC discharge power in kW when the inverter is actively discharging.",
            tier=Tier.EXPERT,
        ),
        "capabilities": FieldSpec(
            label="Hardware capabilities",
            description="Hardware capability flags.",
            tier=Tier.EXPERT,
            nested_form_spec=HYBRID_INVERTER_CAPABILITIES_FORM_SPEC,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic configuration for hybrid inverter control signals.",
            tier=Tier.EXPERT,
            nested_form_spec=HYBRID_INVERTER_OUTPUTS_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Deferrable and static loads
# ---------------------------------------------------------------------------

DEFERRABLE_LOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "power_profile": FieldSpec(
            label="Power profile (kW per step)",
            description="Per-step power draw in kW, one entry per step of the run cycle.",
            tier=Tier.BASIC,
        ),
        "topic_window_earliest": FieldSpec(
            label="Earliest start topic",
            description="MQTT topic for earliest start datetime.",
            tier=Tier.EXPERT,
        ),
        "topic_window_latest": FieldSpec(
            label="Latest end topic",
            description="MQTT topic for latest end datetime.",
            tier=Tier.EXPERT,
        ),
        "topic_committed_start_time": FieldSpec(
            label="Committed start time topic",
            description="Optional MQTT topic for the actual start datetime published by the automation when the load physically begins.",
            tier=Tier.EXPERT,
        ),
        "topic_recommended_start_time": FieldSpec(
            label="Recommended start time topic",
            description="Optional MQTT topic to which mimirheim publishes the solver-recommended start datetime after each solve.",
            tier=Tier.EXPERT,
        ),
    }
)

STATIC_LOAD_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "topic_forecast": FieldSpec(
            label="Forecast topic",
            description="MQTT topic for the per-step base load forecast in kW.",
            tier=Tier.EXPERT,
        ),
    }
)

# ---------------------------------------------------------------------------
# Building thermal model (shared by space heating and combi heat pump)
# ---------------------------------------------------------------------------

BUILDING_THERMAL_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "topic_current_indoor_temp_c": FieldSpec(
            label="Indoor temperature topic",
            description="MQTT topic for current indoor temperature in degrees C, retained.",
            tier=Tier.EXPERT,
        ),
        "topic_outdoor_temp_forecast_c": FieldSpec(
            label="Outdoor temperature forecast topic",
            description="MQTT topic for per-step outdoor temperature forecast, JSON array of floats, retained.",
            tier=Tier.EXPERT,
        ),
    }
)

BUILDING_THERMAL_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "thermal_capacity_kwh_per_k": FieldSpec(
            label="Thermal capacity (kWh/K)", description="Building thermal mass in kWh/K.", tier=Tier.BASIC
        ),
        "heat_loss_coeff_kw_per_k": FieldSpec(
            label="Heat loss coefficient (kW/K)",
            description="Building heat loss coefficient in kW/K.",
            tier=Tier.BASIC,
        ),
        "comfort_min_c": FieldSpec(
            label="Minimum comfort (°C)",
            description="Minimum acceptable indoor temperature in degrees C.",
            tier=Tier.EXPERT,
        ),
        "comfort_max_c": FieldSpec(
            label="Maximum comfort (°C)",
            description="Maximum acceptable indoor temperature in degrees C.",
            tier=Tier.EXPERT,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topics for the building thermal model.",
            tier=Tier.EXPERT,
            nested_form_spec=BUILDING_THERMAL_INPUTS_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Thermal boiler
# ---------------------------------------------------------------------------

THERMAL_BOILER_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "topic_current_temp": FieldSpec(
            label="Current temperature topic",
            description="MQTT topic publishing the current water temperature in degrees C, retained.",
            tier=Tier.EXPERT,
        ),
    }
)

THERMAL_BOILER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "volume_liters": FieldSpec(
            label="Tank volume (L)", description="Water volume of the tank in litres.", tier=Tier.BASIC
        ),
        "elec_power_kw": FieldSpec(
            label="Electrical power (kW)",
            description="Rated electrical power of the heating element in kW.",
            tier=Tier.BASIC,
        ),
        "cop": FieldSpec(
            label="Coefficient of performance (COP)",
            description="Coefficient of performance. 1.0 = resistive; 2+ = heat pump.",
            tier=Tier.EXPERT,
        ),
        "setpoint_c": FieldSpec(
            label="Target temperature (°C)", description="Target hot water temperature in degrees C.", tier=Tier.BASIC
        ),
        "min_temp_c": FieldSpec(
            label="Minimum temperature (°C)",
            description="Minimum allowable water temperature in degrees C.",
            tier=Tier.EXPERT,
        ),
        "cooling_rate_k_per_hour": FieldSpec(
            label="Cooling rate (K/h)",
            description="Tank temperature decay rate in K/hour when the heater is off.",
            tier=Tier.BASIC,
        ),
        "min_run_steps": FieldSpec(
            label="Minimum run steps",
            description="Minimum consecutive active steps once started. 0 = free cycling.",
            tier=Tier.EXPERT,
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Cycling cost per kWh of electrical consumption, in EUR.",
            tier=Tier.BASIC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration for live water temperature readings.",
            tier=Tier.EXPERT,
            nested_form_spec=THERMAL_BOILER_INPUTS_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Space heating heat pump
# ---------------------------------------------------------------------------

HEATING_STAGE_FORM_SPEC = FormSpec(
    fields={
        "elec_kw": FieldSpec(
            label="Electrical power (kW)", description="Electrical power at this stage in kW.", tier=Tier.BASIC
        ),
        "cop": FieldSpec(label="COP", description="COP at this stage.", tier=Tier.BASIC),
    }
)

SPACE_HEATING_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "topic_heat_needed_kwh": FieldSpec(
            label="Heat needed topic",
            description="MQTT topic publishing remaining heat needed this horizon in kWh, retained.",
            tier=Tier.EXPERT,
        ),
        "topic_heat_produced_today_kwh": FieldSpec(
            label="Heat produced today topic",
            description="Optional informational topic for accumulated heat produced today in kWh.",
            tier=Tier.EXPERT,
        ),
    }
)

SPACE_HEATING_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "elec_power_kw": FieldSpec(
            label="Electrical power (kW)",
            description="Rated electrical power for on/off mode in kW. Mutually exclusive with stages.",
            tier=Tier.BASIC,
        ),
        "cop": FieldSpec(
            label="COP (on/off mode)",
            description="Coefficient of performance for on/off mode. Mutually exclusive with stages.",
            tier=Tier.BASIC,
        ),
        "stages": FieldSpec(
            label="Operating stages",
            description="Operating points for SOS2 power-stage mode. Mutually exclusive with elec_power_kw and cop.",
            tier=Tier.BASIC,
            nested_form_spec=HEATING_STAGE_FORM_SPEC,
        ),
        "min_run_steps": FieldSpec(
            label="Minimum run steps",
            description="Minimum consecutive active steps once started.",
            tier=Tier.EXPERT,
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Cycling cost per kWh of electrical consumption, in EUR.",
            tier=Tier.BASIC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration.",
            tier=Tier.EXPERT,
            nested_form_spec=SPACE_HEATING_INPUTS_FORM_SPEC,
        ),
        "building_thermal": FieldSpec(
            label="Building thermal model",
            description=(
                "Optional building thermal model. When set, the solver tracks indoor "
                "temperature instead of the degree-days total-heat lower bound."
            ),
            tier=Tier.EXPERT,
            nested_form_spec=BUILDING_THERMAL_CONFIG_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Combi heat pump (DHW + space heating)
# ---------------------------------------------------------------------------

COMBI_HEAT_PUMP_INPUTS_FORM_SPEC = FormSpec(
    fields={
        "topic_current_temp": FieldSpec(
            label="DHW temperature topic",
            description="MQTT topic for DHW water temperature in degrees C, retained.",
            tier=Tier.EXPERT,
        ),
        "topic_heat_needed_kwh": FieldSpec(
            label="Heat needed topic",
            description="MQTT topic for space heating demand in kWh this horizon, retained.",
            tier=Tier.EXPERT,
        ),
    }
)

COMBI_HEAT_PUMP_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "elec_power_kw": FieldSpec(
            label="Electrical power (kW)", description="Rated electrical power in kW.", tier=Tier.BASIC
        ),
        "cop_dhw": FieldSpec(
            label="COP (DHW mode)", description="COP in DHW mode.", tier=Tier.BASIC
        ),
        "cop_sh": FieldSpec(
            label="COP (space heating mode)", description="COP in space heating mode.", tier=Tier.BASIC
        ),
        "volume_liters": FieldSpec(
            label="DHW tank volume (L)", description="DHW tank water volume in litres.", tier=Tier.BASIC
        ),
        "setpoint_c": FieldSpec(
            label="DHW target temperature (°C)", description="DHW target temperature in degrees C.", tier=Tier.BASIC
        ),
        "min_temp_c": FieldSpec(
            label="DHW minimum temperature (°C)",
            description="DHW minimum temperature in degrees C.",
            tier=Tier.EXPERT,
        ),
        "cooling_rate_k_per_hour": FieldSpec(
            label="Cooling rate (K/h)", description="Tank cooling rate in K/h.", tier=Tier.BASIC
        ),
        "min_run_steps": FieldSpec(
            label="Minimum run steps",
            description="Minimum consecutive active steps, across both modes combined.",
            tier=Tier.EXPERT,
        ),
        "wear_cost_eur_per_kwh": FieldSpec(
            label="Wear cost (€/kWh)",
            description="Cycling cost per kWh of electrical consumption, in EUR.",
            tier=Tier.BASIC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration.",
            tier=Tier.EXPERT,
            nested_form_spec=COMBI_HEAT_PUMP_INPUTS_FORM_SPEC,
        ),
        "building_thermal": FieldSpec(
            label="Building thermal model",
            description="Optional building thermal model for the SH mode. DHW mode is unaffected.",
            tier=Tier.EXPERT,
            nested_form_spec=BUILDING_THERMAL_CONFIG_FORM_SPEC,
        ),
    }
)

# ---------------------------------------------------------------------------
# Grid, objectives
# ---------------------------------------------------------------------------

GRID_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "import_limit_kw": FieldSpec(
            label="Import limit (kW)", description="Maximum grid import power in kW.", tier=Tier.BASIC
        ),
        "export_limit_kw": FieldSpec(
            label="Export limit (kW)", description="Maximum grid export power in kW.", tier=Tier.BASIC
        ),
    }
)

BALANCED_WEIGHTS_FORM_SPEC = FormSpec(
    fields={
        "cost_weight": FieldSpec(
            label="Cost weight", description="Weight on cost/revenue terms.", tier=Tier.EXPERT
        ),
        "self_sufficiency_weight": FieldSpec(
            label="Self-sufficiency weight",
            description="Weight on grid import penalty terms.",
            tier=Tier.EXPERT,
        ),
    }
)

OBJECTIVES_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "balanced_weights": FieldSpec(
            label="Balanced strategy weights",
            description="Objective weights for the balanced strategy. Null = equal weighting.",
            tier=Tier.EXPERT,
            nested_form_spec=BALANCED_WEIGHTS_FORM_SPEC,
        ),
        "min_dispatch_gain_eur": FieldSpec(
            label="Minimum dispatch gain (€)",
            description="Minimum benefit in EUR over the naive baseline required to dispatch storage.",
            tier=Tier.EXPERT,
        ),
        "exchange_shaping_weight": FieldSpec(
            label="Exchange shaping weight",
            description="Weight for the optional secondary exchange-minimisation term.",
            tier=Tier.EXPERT,
        ),
    }
)

# ---------------------------------------------------------------------------
# Remaining core config sections (constraints, solver, readiness, control,
# MQTT, output/input topics, Home Assistant, debug, reporting)
# ---------------------------------------------------------------------------

CONSTRAINTS_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "max_import_kw": FieldSpec(
            label="Max import cap (kW)", description="Hard cap on grid import in kW.", tier=Tier.EXPERT
        ),
        "max_export_kw": FieldSpec(
            label="Max export cap (kW)", description="Hard cap on grid export in kW.", tier=Tier.EXPERT
        ),
    }
)

SOLVER_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "max_horizon_steps": FieldSpec(
            label="Max horizon steps",
            description="Hard cap on the solve horizon in 15-minute steps. 96 = 24 h, 192 = 48 h, 288 = 72 h.",
            tier=Tier.EXPERT,
        ),
        "threads": FieldSpec(
            label="Solver threads",
            description="CBC solver threads. -1 = use all available CPU cores.",
            tier=Tier.EXPERT,
        ),
        "time_limit_seconds": FieldSpec(
            label="Solver time limit (s)",
            description="Wall-clock budget for one solve cycle, in seconds.",
            tier=Tier.EXPERT,
        ),
    }
)

READINESS_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "min_horizon_hours": FieldSpec(
            label="Minimum horizon (h)",
            description="Minimum forecast coverage in hours to attempt a solve.",
            tier=Tier.EXPERT,
        ),
        "warn_below_hours": FieldSpec(
            label="Warn below (h)",
            description="Log a warning when available horizon is below this value in hours.",
            tier=Tier.EXPERT,
        ),
        "max_gap_hours": FieldSpec(
            label="Max gap (h)",
            description="Log a warning when any gap between consecutive forecast points exceeds this value in hours.",
            tier=Tier.EXPERT,
        ),
    }
)

CONTROL_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "exchange_epsilon_kw": FieldSpec(
            label="Exchange epsilon (kW)",
            description="Grid exchange below this value in kW is treated as near-zero.",
            tier=Tier.EXPERT,
        ),
        "headroom_margin_kw": FieldSpec(
            label="Headroom margin (kW)",
            description="Minimum absorption headroom in kW for a device to be eligible as enforcer.",
            tier=Tier.EXPERT,
        ),
        "switch_delta": FieldSpec(
            label="Switch delta",
            description="Challenger must exceed current enforcer score by this amount to trigger a switch.",
            tier=Tier.EXPERT,
        ),
        "min_enforcer_dwell_steps": FieldSpec(
            label="Min enforcer dwell steps",
            description="Minimum consecutive steps a device remains enforcer once selected.",
            tier=Tier.EXPERT,
        ),
    }
)

# Mimirheim core's own MqttConfig (mimirheim/config/schema.py), a separate
# class from helper_common's shared MqttConfig used by the helper daemons.
CORE_MQTT_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "host": FieldSpec(
            label="Broker host", description="MQTT broker hostname or IP address.", tier=Tier.BASIC
        ),
        "port": FieldSpec(
            label="Broker port", description="MQTT broker port.", tier=Tier.EXPERT
        ),
        "client_id": FieldSpec(
            label="Client ID",
            description="MQTT client identifier. Defaults to 'mimir' when not set.",
            tier=Tier.BASIC,
        ),
        "topic_prefix": FieldSpec(
            label="Topic prefix", description="Topic prefix for all mimirheim topics.", tier=Tier.EXPERT
        ),
        "username": FieldSpec(
            label="Username", description="Broker username. Omit for anonymous access.", tier=Tier.EXPERT
        ),
        "password": FieldSpec(
            label="Password", description="Broker password.", tier=Tier.EXPERT
        ),
        "tls": FieldSpec(
            label="Enable TLS", description="Enable TLS for the broker connection.", tier=Tier.EXPERT
        ),
        "tls_allow_insecure": FieldSpec(
            label="Allow insecure TLS",
            description="Skip broker certificate verification when TLS is enabled.",
            tier=Tier.EXPERT,
        ),
    }
)

OUTPUTS_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "schedule": FieldSpec(
            label="Schedule topic", description="Topic for the full horizon schedule.", tier=Tier.EXPERT
        ),
        "current": FieldSpec(
            label="Current topic",
            description="Topic for the current-step strategy summary.",
            tier=Tier.EXPERT,
        ),
        "last_solve": FieldSpec(
            label="Last solve topic",
            description="Topic for the retained solve-status message.",
            tier=Tier.EXPERT,
        ),
        "availability": FieldSpec(
            label="Availability topic",
            description="Topic for birth ('online') and last-will ('offline') messages.",
            tier=Tier.EXPERT,
        ),
    }
)

INPUTS_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "prices": FieldSpec(
            label="Prices topics",
            description="Price topics in priority order. Merged per step by highest confidence; earlier entries win ties.",
            tier=Tier.EXPERT,
        ),
    }
)

# Mimirheim core's own HomeAssistantConfig, a separate class from
# helper_common's shared HomeAssistantConfig used by the helper daemons.
CORE_HOME_ASSISTANT_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "enabled": FieldSpec(
            label="Enable HA discovery", description="Enable HA MQTT discovery. Default: false.", tier=Tier.EXPERT
        ),
        "discovery_prefix": FieldSpec(
            label="Discovery prefix",
            description="Topic prefix used by HA for discovery. Default: 'homeassistant'.",
            tier=Tier.EXPERT,
        ),
        "device_name": FieldSpec(
            label="HA device name", description="Human-readable device name shown in HA.", tier=Tier.EXPERT
        ),
        "device_id": FieldSpec(
            label="HA device ID",
            description="Stable device identifier for the HA device registry. Defaults to mqtt.client_id.",
            tier=Tier.EXPERT,
        ),
    }
)

DEBUG_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "enabled": FieldSpec(
            label="Enable debug dumps", description="Enable DEBUG logging and solve dumps.", tier=Tier.EXPERT
        ),
        "dump_dir": FieldSpec(
            label="Dump directory", description="Directory for solve dumps. Null = disabled.", tier=Tier.EXPERT
        ),
        "max_dumps": FieldSpec(
            label="Max debug dumps", description="Maximum retained dump pairs.", tier=Tier.EXPERT
        ),
    }
)

REPORTING_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "enabled": FieldSpec(
            label="Enable reporting",
            description="Enable production dump writing and MQTT notification.",
            tier=Tier.EXPERT,
        ),
        "dump_dir": FieldSpec(
            label="Report dump directory",
            description="Directory for solve dumps shared with mimirheim-reporter. Null = disabled.",
            tier=Tier.EXPERT,
        ),
        "max_dumps": FieldSpec(
            label="Max reports", description="Maximum retained dump pairs. 0 = unlimited.", tier=Tier.EXPERT
        ),
        "notify_topic": FieldSpec(
            label="Notify topic",
            description="MQTT topic for dump-available notifications.",
            tier=Tier.EXPERT,
        ),
    }
)

# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

MIMIRHEIM_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "batteries": FieldSpec(
            label="Batteries",
            description="Named battery devices.",
            tier=Tier.BASIC,
            nested_form_spec=BATTERY_CONFIG_FORM_SPEC,
        ),
        "pv_arrays": FieldSpec(
            label="PV arrays",
            description="Named PV array devices.",
            tier=Tier.BASIC,
            nested_form_spec=PV_CONFIG_FORM_SPEC,
        ),
        "ev_chargers": FieldSpec(
            label="EV chargers",
            description="Named EV charger devices.",
            tier=Tier.BASIC,
            nested_form_spec=EV_CONFIG_FORM_SPEC,
        ),
        "deferrable_loads": FieldSpec(
            label="Deferrable loads",
            description="Named deferrable load devices (e.g. a dishwasher or washing machine with a scheduling window).",
            tier=Tier.EXPERT,
            nested_form_spec=DEFERRABLE_LOAD_CONFIG_FORM_SPEC,
        ),
        "static_loads": FieldSpec(
            label="Static loads",
            description="Named static (forecast-only) load devices.",
            tier=Tier.BASIC,
            nested_form_spec=STATIC_LOAD_CONFIG_FORM_SPEC,
        ),
        "hybrid_inverters": FieldSpec(
            label="Hybrid inverters",
            description="Named hybrid inverter devices (combined PV and battery behind one inverter).",
            tier=Tier.BASIC,
            nested_form_spec=HYBRID_INVERTER_CONFIG_FORM_SPEC,
        ),
        "thermal_boilers": FieldSpec(
            label="Thermal boilers",
            description="Named domestic hot water thermal boiler devices.",
            tier=Tier.EXPERT,
            nested_form_spec=THERMAL_BOILER_CONFIG_FORM_SPEC,
        ),
        "space_heating_hps": FieldSpec(
            label="Space heating heat pumps",
            description="Named space heating heat pump devices.",
            tier=Tier.EXPERT,
            nested_form_spec=SPACE_HEATING_CONFIG_FORM_SPEC,
        ),
        "combi_heat_pumps": FieldSpec(
            label="Combi heat pumps",
            description="Named combi heat pump devices (domestic hot water and space heating combined).",
            tier=Tier.EXPERT,
            nested_form_spec=COMBI_HEAT_PUMP_CONFIG_FORM_SPEC,
        ),
        "grid": FieldSpec(
            label="Grid connection",
            description="Grid connection parameters (import/export limits). Exactly one per mimirheim instance.",
            tier=Tier.BASIC,
            nested_form_spec=GRID_CONFIG_FORM_SPEC,
        ),
        "objectives": FieldSpec(
            label="Objectives",
            description="Objective function parameters, such as the weights used by the balanced strategy.",
            tier=Tier.EXPERT,
            nested_form_spec=OBJECTIVES_CONFIG_FORM_SPEC,
        ),
        "constraints": FieldSpec(
            label="Constraints",
            description="Hard constraints on grid import/export power, enforced independently of strategy.",
            tier=Tier.EXPERT,
            nested_form_spec=CONSTRAINTS_CONFIG_FORM_SPEC,
        ),
        "solver": FieldSpec(
            label="Solver",
            description="Solver tuning parameters for the CBC MILP backend (horizon cap, thread count, time limit).",
            tier=Tier.EXPERT,
            nested_form_spec=SOLVER_CONFIG_FORM_SPEC,
        ),
        "readiness": FieldSpec(
            label="Readiness",
            description="Forecast coverage thresholds controlling when mimirheim is willing to solve.",
            tier=Tier.EXPERT,
            nested_form_spec=READINESS_CONFIG_FORM_SPEC,
        ),
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tier=Tier.BASIC,
            nested_form_spec=CORE_MQTT_CONFIG_FORM_SPEC,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic names.",
            tier=Tier.EXPERT,
            nested_form_spec=OUTPUTS_CONFIG_FORM_SPEC,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic overrides. Fields default to the standard derived topic when unset.",
            tier=Tier.EXPERT,
            nested_form_spec=INPUTS_CONFIG_FORM_SPEC,
        ),
        "homeassistant": FieldSpec(
            label="Home Assistant",
            description="Home Assistant MQTT autodiscovery settings.",
            tier=Tier.EXPERT,
            nested_form_spec=CORE_HOME_ASSISTANT_CONFIG_FORM_SPEC,
        ),
        "debug": FieldSpec(
            label="Debug",
            description="Debug and diagnostic settings (verbose logging, dump files).",
            tier=Tier.EXPERT,
            nested_form_spec=DEBUG_CONFIG_FORM_SPEC,
        ),
        "control": FieldSpec(
            label="Control",
            description="Parameters for the mode-arbitration and enforcer-selection engine.",
            tier=Tier.EXPERT,
            nested_form_spec=CONTROL_CONFIG_FORM_SPEC,
        ),
        "reporting": FieldSpec(
            label="Reporting",
            description="Settings for the standalone mimirheim-reporter daemon's dump archive.",
            tier=Tier.EXPERT,
            nested_form_spec=REPORTING_CONFIG_FORM_SPEC,
        ),
    }
)
