# 14. MQTT topic naming convention and auto-derivation

## Motivation

All MQTT topics used by mimirheim follow a predictable naming convention derived
from `mqtt.topic_prefix`. Before Plans 49 and 50, operators had to transcribe
these topics explicitly into every device entry in the YAML. Changing the
prefix therefore required editing dozens of topic strings by hand and risked
missing one.

After Plans 49 and 50, all topic fields default to `None` in the schema.
`MimirheimConfig._derive_global_topics` and `MimirheimConfig._derive_device_topics` run
as `model_validator(mode="after")` validators during config load and fill in
`None` fields using the convention in the tables below. Explicit values supplied
in the YAML are preserved; only `None` fields are filled in.

This means the `outputs:` section of the YAML, previously required, is now
entirely optional. An operator who uses only the defaults can omit every MQTT
topic string from their config file.

## Derivation mechanics

The two derivation validators run in definition order on `MimirheimConfig`:

1. `_derive_global_topics` — fills the six system-level topics.
2. `_derive_device_topics` — fills all per-device topics by iterating each
   named device map.

Both validators mutate the model in place. This is safe because mimirheim's Pydantic
models are not frozen (`frozen=True` is not set in any `model_config`). Downstream
code (the MQTT client, publisher, and readiness tracker) reads topic strings from
the validated config and always receives a resolved non-`None` value — no `or`
fallback is needed at any read site.

Device output topics (e.g. `exchange_mode`, `loadbalance_cmd`) are derived for
all devices regardless of whether the corresponding capability is enabled. A
disabled capability means the topic is present in the config but never published.
This is intentional: the topic path is stable so an operator can subscribe to it
before enabling the capability without reconfiguring broker subscriptions.

## Global topic naming convention

The six global topics are derived from `mqtt.topic_prefix` (`p`):

| Config field | Derived topic (prefix = `mimirheim`) |
|---|---|
| `outputs.schedule` | `mimir/strategy/schedule` |
| `outputs.current` | `mimir/strategy/current` |
| `outputs.last_solve` | `mimir/status/last_solve` |
| `outputs.availability` | `mimir/status/availability` |
| `inputs.prices` | `[mimir/input/prices]` (one-element list) |
| `reporting.notify_topic` | `mimir/status/dump_available` |

`inputs.prices` is the one field in this table that is list-valued rather than
scalar (see plan 69, "Multi-source price merge"). A bare string in YAML
coerces to a one-element list. The default-fill validator
(`_derive_global_topics`) only substitutes the single derived topic when the
list is empty (`not self.inputs.prices`), so an explicitly configured list —
including a one-element list — is left untouched. Downstream consumers
(`ReadinessState`, `MqttClient`) always iterate the list; there is no
single-topic fallback path left in the code.

Two further topics are not configurable and are always constructed directly from
the prefix in the IO layer:

| Purpose | Always derived as |
|---|---|
| Strategy selection input | `{prefix}/input/strategy` |
| Solve trigger input | `{prefix}/input/trigger` |

## Device-level topic naming convention

All device topics follow `{p}/{direction}/{device-type}/{name}/{field}`. Input
topics use `{p}/input/...`; output topics use `{p}/output/...`.

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
| `space_heating_hps.{name}.inputs.topic_heat_produced_today_kwh` | `{p}/input/space_heating/{name}/heat_produced_today_kwh` |
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

## Overriding a derived topic

Set the field explicitly in the YAML to override the derived value. The
derivation validator only fills in `None` fields; any non-`None` value is left
unchanged.

```yaml
# Override the prices topic when sharing a broker across multiple mimirheim instances:
inputs:
  prices: "shared/input/prices"

# Override a battery SOC topic to read from a Home Assistant sensor directly:
batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        topic: "homeassistant/sensor/battery_soc/state"
        unit: percent
```

## Minimal configuration pattern

With all topics derived, a device entry only needs its physical parameters. No
MQTT topic needs to appear in the YAML for a standard single-broker deployment:

```yaml
mqtt:
  host: localhost
  topic_prefix: mimir

grid:
  import_limit_kw: 17.0
  export_limit_kw: 17.0

batteries:
  battery_main:
    capacity_kwh: 5.4
    inputs:
      soc:
        unit: percent            # topic derived to mimir/input/battery/battery_main/soc

pv_arrays:
  roof_pv:
    max_power_kw: 4.5           # topic derived to mimir/input/pv/roof_pv/forecast

static_loads:
  base_load: {}                 # topic derived to mimir/input/baseload/base_load/forecast
```
