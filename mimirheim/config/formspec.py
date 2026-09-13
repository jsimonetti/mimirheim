"""FormSpec authored for MimirheimConfig, mimirheim core's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``mimirheim.config.schema`` per
ADR-0002 (``mimirheim_shared/docs/adr``): ``MimirheimConfig`` itself carries no
FormSpec-level presentation metadata. This module is what mimirheim core's
``describe()`` step (``mimirheim.io.config_service``) combines with
``MimirheimConfig`` to build its Config Service Descriptor.

Per ADR-0006, ``mimirheim_shared.alignment.assert_form_spec_complete`` now
checks a FormSpec recursively against its model, at every depth. This module
proves out each of the six Field Shapes (``mimirheim_shared.field_shape`` /
CONTEXT.md) for real at least once: ``grid`` (nested object), ``batteries``
(Named Collection), ``BatteryConfig.charge_segments`` (Ordered Collection),
and ``ObjectivesConfig.balanced_weights`` (optional object). Every other
structural field is given an explicit ``shape_override=FieldShape.SCALAR``:
rendering it as opaque for now is the current, unchanged behaviour, and the
override records that plainly rather than by omission. Covering every field
of every nested model is not required for this to be true, and is left to
whichever later ticket gives that model's own FormSpec real structure.
"""

from __future__ import annotations

from mimirheim_shared.field_shape import FieldShape
from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier
from mimirheim_shared.visibility import Comparison, ComparisonOperator

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
            shape_override=FieldShape.SCALAR,
            visible_if=Comparison(
                field="discharge_efficiency_curve", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "charge_efficiency_curve": FieldSpec(
            label="Charge efficiency curve",
            description="SOS2 piecewise-linear efficiency curve for charging. Mutually exclusive with charge_segments.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
            visible_if=Comparison(
                field="charge_segments", operator=ComparisonOperator.EQ, value=None
            ),
        ),
        "discharge_efficiency_curve": FieldSpec(
            label="Discharge efficiency curve",
            description="SOS2 piecewise-linear efficiency curve for discharging. Mutually exclusive with discharge_segments.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
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
            shape_override=FieldShape.SCALAR,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic configuration for battery state readings.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic configuration for battery control signals.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "soc_ratchet": FieldSpec(
            label="SOC ratchet",
            description="Periodic full-charge policy. Disabled by default.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
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
            shape_override=FieldShape.SCALAR,
        ),
        "ev_chargers": FieldSpec(
            label="EV chargers",
            description="Named EV charger devices.",
            tier=Tier.BASIC,
            shape_override=FieldShape.SCALAR,
        ),
        "deferrable_loads": FieldSpec(
            label="Deferrable loads",
            description="Named deferrable load devices (e.g. a dishwasher or washing machine with a scheduling window).",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "static_loads": FieldSpec(
            label="Static loads",
            description="Named static (forecast-only) load devices.",
            tier=Tier.BASIC,
            shape_override=FieldShape.SCALAR,
        ),
        "hybrid_inverters": FieldSpec(
            label="Hybrid inverters",
            description="Named hybrid inverter devices (combined PV and battery behind one inverter).",
            tier=Tier.BASIC,
            shape_override=FieldShape.SCALAR,
        ),
        "thermal_boilers": FieldSpec(
            label="Thermal boilers",
            description="Named domestic hot water thermal boiler devices.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "space_heating_hps": FieldSpec(
            label="Space heating heat pumps",
            description="Named space heating heat pump devices.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "combi_heat_pumps": FieldSpec(
            label="Combi heat pumps",
            description="Named combi heat pump devices (domestic hot water and space heating combined).",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
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
            shape_override=FieldShape.SCALAR,
        ),
        "solver": FieldSpec(
            label="Solver",
            description="Solver tuning parameters for the CBC MILP backend (horizon cap, thread count, time limit).",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "readiness": FieldSpec(
            label="Readiness",
            description="Forecast coverage thresholds controlling when mimirheim is willing to solve.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "mqtt": FieldSpec(
            label="MQTT",
            description="MQTT broker connection parameters.",
            tier=Tier.BASIC,
            shape_override=FieldShape.SCALAR,
        ),
        "outputs": FieldSpec(
            label="Output topics",
            description="MQTT output topic names.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic overrides. Fields default to the standard derived topic when unset.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "homeassistant": FieldSpec(
            label="Home Assistant",
            description="Home Assistant MQTT autodiscovery settings.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "debug": FieldSpec(
            label="Debug",
            description="Debug and diagnostic settings (verbose logging, dump files).",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "control": FieldSpec(
            label="Control",
            description="Parameters for the mode-arbitration and enforcer-selection engine.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
        "reporting": FieldSpec(
            label="Reporting",
            description="Settings for the standalone mimirheim-reporter daemon's dump archive.",
            tier=Tier.EXPERT,
            shape_override=FieldShape.SCALAR,
        ),
    }
)
