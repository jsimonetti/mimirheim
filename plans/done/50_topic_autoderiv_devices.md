# Plan 50 — Auto-derive all device-level topics — the MQTT topic naming convention

## Motivation

Plan 49 made the six global system topics optional by deriving them from
`mqtt.topic_prefix`. This plan applies the same treatment to every per-device
topic in the schema.

A representative minimal `batteries:` entry currently requires:

```yaml
batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        topic: "homeassistant/sensor/battery_soc/state"
        unit: percent
    outputs:
      exchange_mode: null        # required when zero_exchange: true
```

After this plan, the same entry can be written as:

```yaml
batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        unit: percent           # topic derived to {prefix}/input/battery/battery_main/soc
```

The `outputs.exchange_mode` field is derived automatically when
`capabilities.zero_exchange: true` is set; the per-device validators that
previously rejected `capability=true, topic=null` are removed (the validator's
purpose is now served by the derivation itself).

This plan defines the complete device-level topic naming convention, documented
in IMPLEMENTATION_DETAILS §12 (extending what Plan 49 introduced). All helpers
and integrations (HA automations, PV ML helper, prices helper) must publish to
these derived paths so that a zero-topic-config deployment works end-to-end.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §12 (introduced in Plan 49) — extends the device-level table
- §3 Config schema design
- §6 Pydantic config models as device constructor arguments
- §9 Concurrency model (topic subscriptions)

---

## Prerequisites

Plan 49 must be complete. `MimirheimConfig._derive_global_topics` must exist.
This plan adds `MimirheimConfig._derive_device_topics`, which runs in the same
`mode="after"` chain.

---

## Critical design decisions

### Derivation is the only change; readiness and IO are unaffected

The readiness state (`ReadinessState`) and the MQTT client (`MqttClient`) use
topic strings as opaque keys. After this plan, they still use topic strings as
opaque keys — the only difference is that those strings are now guaranteed to be
non-None after schema validation. No changes are needed in `readiness.py` or in
`mqtt_client.py` beyond removing the existing capability-topic checks that are
now replaced by derivation.

### Per-device capability validators are removed

`BatteryConfig._validate_zero_exchange_output`, `EvConfig._validate_zero_exchange_output`,
`EvConfig._validate_loadbalance_output`, and `PvConfig._validate_on_off_requires_output_topic`
all exist to catch the "capability enabled, topic not set" case. After this plan,
the derivation fills in the topic when a capability is enabled, so these validators
are dead code. They must be removed.

A final post-derivation consistency check is added to `_derive_device_topics`:
if any required-when-enabled capability still has a `None` topic after the
derivation loop (impossible in practice, but defensive), raise a `ValueError` at
that point. This keeps the contract explicit.

### `SocTopicConfig.topic` becomes `str | None`

`SocTopicConfig` is shared by `BatteryInputsConfig`, `EvInputsConfig`, and
`HybridInverterConfig`. Changing `topic` from `str` to `str | None = None`
makes it optional in YAML while keeping `unit` as a required field (the device
name gives us the topic; the unit is application-specific and cannot be guessed).

### `DeferrableLoadConfig` required topic fields become optional

`topic_window_earliest` and `topic_window_latest` are currently required `str`.
An HA automation that publishes to `{prefix}/input/deferrable/{name}/window_earliest`
is a valid default. Both become `str | None = None`. The `topic_committed_start_time`
and `topic_recommended_start_time` fields are already optional; their defaults change
from `None` (no default) to derived paths.

### Why "owning" the input namespace matters

Forecast topics (`mimir/input/pv/...`) were already in the mimirheim namespace in the
existing example config. The state-reading topics (battery SOC, EV SOC, etc.)
were previously pointed at Home Assistant sensor entities in the
`homeassistant/sensor/...` namespace. This coupling forced operators to copy
the exact HA sensor entity ID into the mimirheim config — which breaks whenever the
HA entity is renamed, and prevents non-HA deployments from working out of the box.

After this plan, default input topics are in the mimirheim namespace. HA operators
add a short Node-RED flow or HA automation that republishes the sensor reading:

```yaml
# HA automation example (state_changed trigger)
- trigger:
    platform: state
    entity_id: sensor.battery_soc
  action:
    service: mqtt.publish
    data:
      topic: "mimir/input/battery/battery_main/soc"
      payload: "{{ trigger.to_state.state }}"
      retain: true
```

mimirheim helper tools (`mimirheim_helpers/`) are updated to publish to the derived topics
by default so they work without additional configuration.

---

## Complete device-level topic naming convention

This table extends IMPLEMENTATION_DETAILS §12. All topics are relative to
`{prefix}` which defaults to `"mimirheim"`.

### Input topics

| Config field | Derived topic |
|---|---|
| `batteries.{name}.inputs.soc.topic` | `{p}/input/battery/{name}/soc` |
| `ev_chargers.{name}.inputs.soc.topic` | `{p}/input/ev/{name}/soc` |
| `ev_chargers.{name}.inputs.plugged_in_topic` | `{p}/input/ev/{name}/plugged_in` |
| `hybrid_inverters.{name}.inputs.soc.topic` | `{p}/input/hybrid/{name}/soc` |
| `hybrid_inverters.{name}.topic_pv_forecast` | `{p}/input/hybrid/{name}/pv_forecast` |
| `pv_arrays.{name}.topic_forecast` | `{p}/input/pv/{name}/forecast` |
| `static_loads.{name}.topic_forecast` | `{p}/input/baseload/{name}/forecast` |
| `deferrable_loads.{name}.topic_window_earliest` | `{p}/input/deferrable/{name}/window_earliest` |
| `deferrable_loads.{name}.topic_window_latest` | `{p}/input/deferrable/{name}/window_latest` |
| `deferrable_loads.{name}.topic_committed_start_time` | `{p}/input/deferrable/{name}/committed_start` |
| `thermal_boilers.{name}.inputs.topic_current_temp` | `{p}/input/thermal_boiler/{name}/temp_c` |
| `space_heating_hps.{name}.inputs.topic_heat_needed_kwh` | `{p}/input/space_heating/{name}/heat_needed_kwh` |
| `space_heating_hps.{name}.building_thermal.inputs.topic_current_indoor_temp_c` | `{p}/input/space_heating/{name}/btm/indoor_temp_c` |
| `space_heating_hps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c` | `{p}/input/space_heating/{name}/btm/outdoor_forecast_c` |
| `combi_heat_pumps.{name}.inputs.topic_current_temp` | `{p}/input/combi_hp/{name}/temp_c` |
| `combi_heat_pumps.{name}.inputs.topic_heat_needed_kwh` | `{p}/input/combi_hp/{name}/sh_heat_needed_kwh` |
| `combi_heat_pumps.{name}.building_thermal.inputs.topic_current_indoor_temp_c` | `{p}/input/combi_hp/{name}/btm/indoor_temp_c` |
| `combi_heat_pumps.{name}.building_thermal.inputs.topic_outdoor_temp_forecast_c` | `{p}/input/combi_hp/{name}/btm/outdoor_forecast_c` |

### Output topics

| Config field | Derived topic |
|---|---|
| `batteries.{name}.outputs.exchange_mode` | `{p}/output/battery/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.exchange_mode` | `{p}/output/ev/{name}/exchange_mode` |
| `ev_chargers.{name}.outputs.loadbalance_cmd` | `{p}/output/ev/{name}/loadbalance` |
| `pv_arrays.{name}.outputs.power_limit_kw` | `{p}/output/pv/{name}/power_limit_kw` |
| `pv_arrays.{name}.outputs.zero_export_mode` | `{p}/output/pv/{name}/zero_export_mode` |
| `pv_arrays.{name}.outputs.on_off_mode` | `{p}/output/pv/{name}/on_off_mode` |
| `deferrable_loads.{name}.topic_recommended_start_time` | `{p}/output/deferrable/{name}/recommended_start` |

Note: device control output topics (`exchange_mode`, `loadbalance_cmd`, `on_off_mode`,
etc.) are derived for all devices, regardless of whether the corresponding
capability is enabled. When a capability is disabled, the derived topic is present
in the config but never published. This is intentional: the topic exists and is
stable so an operator can subscribe to it before enabling the capability without
reconfiguring the broker subscription.

---

## Relevant source locations

```
mimirheim/config/schema.py            — change topic fields to str | None;
                                   remove per-device capability validators;
                                   add MimirheimConfig._derive_device_topics
tests/unit/test_config_schema.py — add derivation and override tests per device type
mimirheim/io/mqtt_client.py           — remove any remaining capability-topic guards
mimirheim/config/example.yaml         — rewrite to show zero-topic minimal config plus
                                   annotated override examples
README.md                        — update device topic table; add convention section
IMPLEMENTATION_DETAILS.md        — extend §12 with full device-level convention table
```

---

## Tests first

All new tests go in `tests/unit/test_config_schema.py`. Run
`uv run pytest tests/unit/test_config_schema.py` — all new tests must fail before
implementation begins.

```python
# ---------------------------------------------------------------------------
# Battery device topics
# ---------------------------------------------------------------------------

def test_battery_soc_topic_derived_from_prefix_and_name() -> None:
    """Battery SOC topic is derived when inputs.soc.topic is not set.
    config.batteries["battery_main"].inputs.soc.topic
        == "mimir/input/battery/battery_main/soc"
    Inputs section provided with unit but no topic.
    """

def test_battery_soc_explicit_topic_preserved() -> None:
    """Explicit inputs.soc.topic value is kept unchanged."""

def test_battery_exchange_mode_derived_when_zero_exchange_enabled() -> None:
    """outputs.exchange_mode is derived when capabilities.zero_exchange=True
    and exchange_mode is None.
    config.batteries["b"].outputs.exchange_mode
        == "mimir/output/battery/b/exchange_mode"
    """

def test_battery_exchange_mode_also_derived_when_capability_disabled() -> None:
    """exchange_mode is derived even when zero_exchange=False.
    The topic is present; it is simply never published.
    """

def test_battery_exchange_mode_explicit_topic_preserved() -> None:
    """Explicit outputs.exchange_mode is kept even when zero_exchange=True."""

def test_battery_zero_exchange_no_longer_requires_explicit_topic() -> None:
    """capabilities.zero_exchange=True with outputs.exchange_mode=None
    does not raise a ValidationError; the topic is auto-derived.
    """

# ---------------------------------------------------------------------------
# EV charger device topics
# ---------------------------------------------------------------------------

def test_ev_soc_topic_derived() -> None:
    """inputs.soc.topic derived to {prefix}/input/ev/{name}/soc."""

def test_ev_plugged_in_topic_derived() -> None:
    """inputs.plugged_in_topic derived to {prefix}/input/ev/{name}/plugged_in."""

def test_ev_exchange_mode_derived() -> None:
    """outputs.exchange_mode derived to {prefix}/output/ev/{name}/exchange_mode."""

def test_ev_loadbalance_cmd_derived() -> None:
    """outputs.loadbalance_cmd derived to {prefix}/output/ev/{name}/loadbalance."""

def test_ev_zero_exchange_no_longer_requires_explicit_topic() -> None:
    """capabilities.zero_exchange=True without exchange_mode does not raise."""

def test_ev_loadbalance_no_longer_requires_explicit_topic() -> None:
    """capabilities.loadbalance=True without loadbalance_cmd does not raise."""

# ---------------------------------------------------------------------------
# PV array device topics
# ---------------------------------------------------------------------------

def test_pv_forecast_topic_derived() -> None:
    """topic_forecast derived to {prefix}/input/pv/{name}/forecast."""

def test_pv_power_limit_kw_topic_derived() -> None:
    """outputs.power_limit_kw derived to {prefix}/output/pv/{name}/power_limit_kw."""

def test_pv_zero_export_mode_topic_derived() -> None:
    """outputs.zero_export_mode derived to {prefix}/output/pv/{name}/zero_export_mode."""

def test_pv_on_off_mode_topic_derived() -> None:
    """outputs.on_off_mode derived to {prefix}/output/pv/{name}/on_off_mode."""

def test_pv_on_off_no_longer_requires_explicit_topic() -> None:
    """capabilities.on_off=True without on_off_mode does not raise."""

# ---------------------------------------------------------------------------
# Static load and hybrid inverter topics
# ---------------------------------------------------------------------------

def test_static_load_forecast_topic_derived() -> None:
    """topic_forecast derived to {prefix}/input/baseload/{name}/forecast."""

def test_hybrid_soc_topic_derived() -> None:
    """inputs.soc.topic derived to {prefix}/input/hybrid/{name}/soc."""

def test_hybrid_pv_forecast_topic_derived() -> None:
    """topic_pv_forecast derived to {prefix}/input/hybrid/{name}/pv_forecast."""

# ---------------------------------------------------------------------------
# Deferrable load topics
# ---------------------------------------------------------------------------

def test_deferrable_window_earliest_topic_derived() -> None:
    """topic_window_earliest derived to {prefix}/input/deferrable/{name}/window_earliest."""

def test_deferrable_window_latest_topic_derived() -> None:
    """topic_window_latest derived to {prefix}/input/deferrable/{name}/window_latest."""

def test_deferrable_committed_start_topic_derived() -> None:
    """topic_committed_start_time derived to
    {prefix}/input/deferrable/{name}/committed_start."""

def test_deferrable_recommended_start_topic_derived() -> None:
    """topic_recommended_start_time derived to
    {prefix}/output/deferrable/{name}/recommended_start."""

def test_deferrable_explicit_topic_preserves_override() -> None:
    """An explicit topic_window_earliest is not overwritten by derivation."""

# ---------------------------------------------------------------------------
# Thermal boiler topics
# ---------------------------------------------------------------------------

def test_thermal_boiler_temp_topic_derived() -> None:
    """inputs.topic_current_temp derived to
    {prefix}/input/thermal_boiler/{name}/temp_c."""

# ---------------------------------------------------------------------------
# Space heating heat pump topics
# ---------------------------------------------------------------------------

def test_space_heating_heat_needed_topic_derived() -> None:
    """inputs.topic_heat_needed_kwh derived to
    {prefix}/input/space_heating/{name}/heat_needed_kwh."""

def test_space_heating_btm_indoor_temp_topic_derived() -> None:
    """building_thermal.inputs.topic_current_indoor_temp_c derived to
    {prefix}/input/space_heating/{name}/btm/indoor_temp_c."""

def test_space_heating_btm_outdoor_forecast_topic_derived() -> None:
    """building_thermal.inputs.topic_outdoor_temp_forecast_c derived to
    {prefix}/input/space_heating/{name}/btm/outdoor_forecast_c."""

# ---------------------------------------------------------------------------
# Combi heat pump topics
# ---------------------------------------------------------------------------

def test_combi_hp_temp_topic_derived() -> None:
    """inputs.topic_current_temp derived to
    {prefix}/input/combi_hp/{name}/temp_c."""

def test_combi_hp_sh_heat_needed_topic_derived() -> None:
    """inputs.topic_heat_needed_kwh derived to
    {prefix}/input/combi_hp/{name}/sh_heat_needed_kwh."""

def test_combi_hp_btm_topics_derived() -> None:
    """building_thermal.inputs topics derived under combi_hp/{name}/btm/."""

# ---------------------------------------------------------------------------
# Custom prefix propagates to all device topics
# ---------------------------------------------------------------------------

def test_custom_prefix_propagates_to_device_topics() -> None:
    """When mqtt.topic_prefix is 'home/v2', all derived device topics use
    that prefix.
    config with prefix='home/v2', battery named 'bat1':
    config.batteries["bat1"].inputs.soc.topic == "home/v2/input/battery/bat1/soc"
    """
```

---

## Implementation

### `mimirheim/config/schema.py` — field changes

#### `SocTopicConfig`

Change `topic: str` to `str | None = None`. The `unit` field remains required.

```python
class SocTopicConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str | None = Field(
        default=None,
        description=(
            "MQTT topic publishing the SOC value. "
            "Defaults to the device-specific path derived from mqtt.topic_prefix."
        ),
    )
    unit: Literal["kwh", "percent"] = Field(...)
```

#### `EvInputsConfig`

Change `plugged_in_topic: str` to `str | None = None`.

#### `BatteryOutputsConfig`

`exchange_mode: str | None` — already `None` by default. No field change needed.
The validator that rejects it when capability is enabled is removed (see below).

#### `EvOutputsConfig`

Both `exchange_mode` and `loadbalance_cmd` already default to `None`. No field
changes. Remove the per-device validators.

#### `PvConfig`

Change `topic_forecast: str = Field(...)` to `str | None = Field(default=None, ...)`.

Update `PvOutputsConfig`: all three output topic fields already default to `None`.
No field changes. Remove the `on_off` validator.

#### `StaticLoadConfig`

Change `topic_forecast: str = Field(...)` to `str | None = Field(default=None, ...)`.

#### `HybridInverterConfig`

Change `topic_pv_forecast: str = Field(...)` to `str | None = Field(default=None, ...)`.

#### `DeferrableLoadConfig`

Change `topic_window_earliest: str` and `topic_window_latest: str` to
`str | None = None`. The `topic_committed_start_time` and
`topic_recommended_start_time` fields are already `str | None`; no field change
needed (their derived defaults are filled in by the new validator, not by a
`Field(default=...)` value).

#### `ThermalBoilerInputsConfig`

Change `topic_current_temp: str` to `str | None = None`.

#### `SpaceHeatingInputsConfig`

Change `topic_heat_needed_kwh: str` to `str | None = None`.
The `topic_heat_produced_today_kwh` field is already `str | None`.

#### `CombiHeatPumpInputsConfig`

Change `topic_current_temp: str` and `topic_heat_needed_kwh: str` to
`str | None = None`.

#### `BuildingThermalInputsConfig`

Change `topic_current_indoor_temp_c: str` and `topic_outdoor_temp_forecast_c: str`
to `str | None = None`.

### `mimirheim/config/schema.py` — remove per-device validators

Remove the following `model_validator` methods completely:

- `BatteryConfig._validate_zero_exchange_output`
- `EvConfig._validate_zero_exchange_output`
- `EvConfig._validate_loadbalance_output`
- `PvConfig._validate_on_off_requires_output_topic`

### `mimirheim/config/schema.py` — new `_derive_device_topics` validator

Add a second `mode="after"` validator on `MimirheimConfig`, defined after
`_derive_global_topics`. Run order is definition order in Pydantic v2.

The validator iterates all device sections and fills in any `None` topic fields:

```python
@model_validator(mode="after")
def _derive_device_topics(self) -> "MimirheimConfig":
    """Fill in per-device topic fields that were not explicitly set.

    Topic strings are derived from mqtt.topic_prefix and the device name
    using the convention documented in IMPLEMENTATION_DETAILS §12.

    After this validator, no topic field on any configured device is None.
    Output topics are derived regardless of whether the related capability is
    enabled; they are simply never published for disabled capabilities.

    Args (implicit: self): The MimirheimConfig instance after field validation.

    Returns:
        The MimirheimConfig instance with all device topic fields resolved.
    """
    p = self.mqtt.topic_prefix

    for name, cfg in self.batteries.items():
        if cfg.inputs is not None and cfg.inputs.soc.topic is None:
            cfg.inputs.soc.topic = f"{p}/input/battery/{name}/soc"
        if cfg.outputs.exchange_mode is None:
            cfg.outputs.exchange_mode = f"{p}/output/battery/{name}/exchange_mode"

    for name, cfg in self.ev_chargers.items():
        if cfg.inputs is not None:
            if cfg.inputs.soc.topic is None:
                cfg.inputs.soc.topic = f"{p}/input/ev/{name}/soc"
            if cfg.inputs.plugged_in_topic is None:
                cfg.inputs.plugged_in_topic = f"{p}/input/ev/{name}/plugged_in"
        if cfg.outputs.exchange_mode is None:
            cfg.outputs.exchange_mode = f"{p}/output/ev/{name}/exchange_mode"
        if cfg.outputs.loadbalance_cmd is None:
            cfg.outputs.loadbalance_cmd = f"{p}/output/ev/{name}/loadbalance"

    for name, cfg in self.pv_arrays.items():
        if cfg.topic_forecast is None:
            cfg.topic_forecast = f"{p}/input/pv/{name}/forecast"
        if cfg.outputs.power_limit_kw is None:
            cfg.outputs.power_limit_kw = f"{p}/output/pv/{name}/power_limit_kw"
        if cfg.outputs.zero_export_mode is None:
            cfg.outputs.zero_export_mode = f"{p}/output/pv/{name}/zero_export_mode"
        if cfg.outputs.on_off_mode is None:
            cfg.outputs.on_off_mode = f"{p}/output/pv/{name}/on_off_mode"

    for name, cfg in self.static_loads.items():
        if cfg.topic_forecast is None:
            cfg.topic_forecast = f"{p}/input/baseload/{name}/forecast"

    for name, cfg in self.hybrid_inverters.items():
        if cfg.inputs is not None and cfg.inputs.soc.topic is None:
            cfg.inputs.soc.topic = f"{p}/input/hybrid/{name}/soc"
        if cfg.topic_pv_forecast is None:
            cfg.topic_pv_forecast = f"{p}/input/hybrid/{name}/pv_forecast"

    for name, cfg in self.deferrable_loads.items():
        if cfg.topic_window_earliest is None:
            cfg.topic_window_earliest = f"{p}/input/deferrable/{name}/window_earliest"
        if cfg.topic_window_latest is None:
            cfg.topic_window_latest = f"{p}/input/deferrable/{name}/window_latest"
        if cfg.topic_committed_start_time is None:
            cfg.topic_committed_start_time = (
                f"{p}/input/deferrable/{name}/committed_start"
            )
        if cfg.topic_recommended_start_time is None:
            cfg.topic_recommended_start_time = (
                f"{p}/output/deferrable/{name}/recommended_start"
            )

    for name, cfg in self.thermal_boilers.items():
        if cfg.inputs is not None and cfg.inputs.topic_current_temp is None:
            cfg.inputs.topic_current_temp = (
                f"{p}/input/thermal_boiler/{name}/temp_c"
            )

    for name, cfg in self.space_heating_hps.items():
        if cfg.inputs is not None and cfg.inputs.topic_heat_needed_kwh is None:
            cfg.inputs.topic_heat_needed_kwh = (
                f"{p}/input/space_heating/{name}/heat_needed_kwh"
            )
        if cfg.building_thermal is not None and cfg.building_thermal.inputs is not None:
            btm = cfg.building_thermal.inputs
            if btm.topic_current_indoor_temp_c is None:
                btm.topic_current_indoor_temp_c = (
                    f"{p}/input/space_heating/{name}/btm/indoor_temp_c"
                )
            if btm.topic_outdoor_temp_forecast_c is None:
                btm.topic_outdoor_temp_forecast_c = (
                    f"{p}/input/space_heating/{name}/btm/outdoor_forecast_c"
                )

    for name, cfg in self.combi_heat_pumps.items():
        if cfg.inputs is not None:
            if cfg.inputs.topic_current_temp is None:
                cfg.inputs.topic_current_temp = (
                    f"{p}/input/combi_hp/{name}/temp_c"
                )
            if cfg.inputs.topic_heat_needed_kwh is None:
                cfg.inputs.topic_heat_needed_kwh = (
                    f"{p}/input/combi_hp/{name}/sh_heat_needed_kwh"
                )
        if cfg.building_thermal is not None and cfg.building_thermal.inputs is not None:
            btm = cfg.building_thermal.inputs
            if btm.topic_current_indoor_temp_c is None:
                btm.topic_current_indoor_temp_c = (
                    f"{p}/input/combi_hp/{name}/btm/indoor_temp_c"
                )
            if btm.topic_outdoor_temp_forecast_c is None:
                btm.topic_outdoor_temp_forecast_c = (
                    f"{p}/input/combi_hp/{name}/btm/outdoor_forecast_c"
                )

    return self
```

### `mimirheim/config/example.yaml` — rewrite device sections

The example config has two goals after this plan:

1. **Minimal section**: shows the smallest valid config for each device type,
   with comments explaining which topics are derived.
2. **Override section** (commented): shows how operators set explicit topics
   when the derived path does not match their setup (e.g. an existing HA sensor
   entity ID they want to subscribe to directly).

For the `batteries:` section, provide a minimal entry:

```yaml
batteries:
  battery_main:
    capacity_kwh: 5.4
    min_soc_kwh: 0.5
    charge_segments:
      - { power_max_kw: 2.5, efficiency: 0.95 }
    discharge_segments:
      - { power_max_kw: 2.5, efficiency: 0.95 }
    wear_cost_eur_per_kwh: 0.02

    capabilities:
      zero_exchange: false

    # MQTT input for live state readings.
    # topic is derived to {prefix}/input/battery/battery_main/soc
    # when not set. unit must always be specified.
    inputs:
      soc:
        unit: percent

    # Optional: override the derived topics explicitly.
    # Uncomment to subscribe to a specific HA sensor topic instead:
    # inputs:
    #   soc:
    #     topic: "homeassistant/sensor/battery_soc/state"
    #     unit: percent
```

Apply this pattern to every device section: show the minimal zero-topic form as
the primary example, and show the full explicit form as commented override.

For forecast topics (PV, static load), add the comment pattern where the topice
is clearly described: e.g., `# topic derived to {prefix}/input/pv/pv_roof/forecast`.

### `IMPLEMENTATION_DETAILS.md` — extend §12

Replace the placeholder note "Per-device topics are documented in Plan 50" with
the complete device-level topic convention table reproduced from the "Complete
device-level topic naming convention" section of this plan.

Add a subsection "Minimal configuration pattern" that shows: with all topics
derived, the only required fields per device are the physical parameters
(capacity, power limits, efficiency curves). No MQTT topic needs to be
explicitly specified for a standard single-broker deployment where all helpers
publish to the mimirheim namespace.

### `README.md`

1. Update the configuration reference section for each device type to note which
   topic fields are optional (with derived defaults shown in the table).
2. Add a "Topic naming convention" section under the MQTT documentation that
   contains the full global + device table (same content as IMPLEMENTATION_DETAILS §12).
3. Show a minimal end-to-end YAML example: a one-battery, one-PV, one-static-load
   system with no explicit topics set, demonstrating that the config is essentially
   physics-only.

---

## Golden files and scenario configs

The scenario configs in `tests/scenarios/*/config.yaml` currently specify explicit
topic strings for all device inputs. These explicit values are preserved by the
derivation (the validator only fills in `None` values). No golden file regeneration
is needed. Verify this by running `uv run pytest` and confirming all scenario tests
pass without modification.

---

## Acceptance criteria

- All topic fields listed in the "Complete device-level topic naming convention"
  table are `str | None` in the schema.
- `MimirheimConfig._derive_device_topics` fills in all `None` device topic fields from
  prefix and device name.
- A config with only physical device parameters (no topic fields) produces a fully
  resolved config with all topics set to the derived paths.
- A config that specifies one or more explicit topics preserves those values
  and derives the rest.
- `PvConfig.topic_forecast`, `StaticLoadConfig.topic_forecast`, and
  `HybridInverterConfig.topic_pv_forecast` are all `str | None`.
- `SocTopicConfig.topic`, `EvInputsConfig.plugged_in_topic`,
  `DeferrableLoadConfig.topic_window_earliest`, and
  `DeferrableLoadConfig.topic_window_latest` are all `str | None`.
- The validators `BatteryConfig._validate_zero_exchange_output`,
  `EvConfig._validate_zero_exchange_output`, `EvConfig._validate_loadbalance_output`,
  and `PvConfig._validate_on_off_requires_output_topic` are removed.
- All new unit tests pass.
- All existing unit tests and golden scenario tests remain green (`uv run pytest`).
- `example.yaml` shows minimal zero-topic device configs with derivation comments.
- `IMPLEMENTATION_DETAILS.md` §12 contains the complete global + device topic table.
- `README.md` contains a topic naming convention section and a minimal config example.
