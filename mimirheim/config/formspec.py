"""FormSpec authored for MimirheimConfig, mimirheim core's Config Owner presentation layer.

Kept as a separate, colocated artifact from ``mimirheim.config.schema`` per
ADR-0002 (``mimirheim_shared/docs/adr``): ``MimirheimConfig`` itself carries no
FormSpec-level presentation metadata. This module is what mimirheim core's
``describe()`` step (``mimirheim.io.config_service``) combines with
``MimirheimConfig`` to build its Config Service Descriptor.

Only MimirheimConfig's own top-level fields are covered here.
``mimirheim_shared.alignment.assert_form_spec_complete`` only checks a
model's immediate fields, not nested models recursively; FormSpecs for
individual device config models (BatteryConfig, PvConfig, and so on) are
authored separately, as later tickets need them.
"""

from __future__ import annotations

from mimirheim_shared.formspec import FieldSpec, FormSpec, Tier

MIMIRHEIM_CONFIG_FORM_SPEC = FormSpec(
    fields={
        "batteries": FieldSpec(
            label="Batteries", description="Named battery devices.", tier=Tier.BASIC
        ),
        "pv_arrays": FieldSpec(
            label="PV arrays", description="Named PV array devices.", tier=Tier.BASIC
        ),
        "ev_chargers": FieldSpec(
            label="EV chargers", description="Named EV charger devices.", tier=Tier.BASIC
        ),
        "deferrable_loads": FieldSpec(
            label="Deferrable loads",
            description="Named deferrable load devices (e.g. a dishwasher or washing machine with a scheduling window).",
            tier=Tier.EXPERT,
        ),
        "static_loads": FieldSpec(
            label="Static loads", description="Named static (forecast-only) load devices.", tier=Tier.BASIC
        ),
        "hybrid_inverters": FieldSpec(
            label="Hybrid inverters",
            description="Named hybrid inverter devices (combined PV and battery behind one inverter).",
            tier=Tier.BASIC,
        ),
        "thermal_boilers": FieldSpec(
            label="Thermal boilers", description="Named domestic hot water thermal boiler devices.", tier=Tier.EXPERT
        ),
        "space_heating_hps": FieldSpec(
            label="Space heating heat pumps",
            description="Named space heating heat pump devices.",
            tier=Tier.EXPERT,
        ),
        "combi_heat_pumps": FieldSpec(
            label="Combi heat pumps",
            description="Named combi heat pump devices (domestic hot water and space heating combined).",
            tier=Tier.EXPERT,
        ),
        "grid": FieldSpec(
            label="Grid connection",
            description="Grid connection parameters (import/export limits). Exactly one per mimirheim instance.",
            tier=Tier.BASIC,
        ),
        "objectives": FieldSpec(
            label="Objectives",
            description="Objective function parameters, such as the weights used by the balanced strategy.",
            tier=Tier.EXPERT,
        ),
        "constraints": FieldSpec(
            label="Constraints",
            description="Hard constraints on grid import/export power, enforced independently of strategy.",
            tier=Tier.EXPERT,
        ),
        "solver": FieldSpec(
            label="Solver",
            description="Solver tuning parameters for the CBC MILP backend (horizon cap, thread count, time limit).",
            tier=Tier.EXPERT,
        ),
        "readiness": FieldSpec(
            label="Readiness",
            description="Forecast coverage thresholds controlling when mimirheim is willing to solve.",
            tier=Tier.EXPERT,
        ),
        "mqtt": FieldSpec(
            label="MQTT", description="MQTT broker connection parameters.", tier=Tier.BASIC
        ),
        "outputs": FieldSpec(
            label="Output topics", description="MQTT output topic names.", tier=Tier.EXPERT
        ),
        "inputs": FieldSpec(
            label="Input topics",
            description="MQTT input topic overrides. Fields default to the standard derived topic when unset.",
            tier=Tier.EXPERT,
        ),
        "homeassistant": FieldSpec(
            label="Home Assistant",
            description="Home Assistant MQTT autodiscovery settings.",
            tier=Tier.EXPERT,
        ),
        "debug": FieldSpec(
            label="Debug", description="Debug and diagnostic settings (verbose logging, dump files).", tier=Tier.EXPERT
        ),
        "control": FieldSpec(
            label="Control",
            description="Parameters for the mode-arbitration and enforcer-selection engine.",
            tier=Tier.EXPERT,
        ),
        "reporting": FieldSpec(
            label="Reporting",
            description="Settings for the standalone mimirheim-reporter daemon's dump archive.",
            tier=Tier.EXPERT,
        ),
    }
)
