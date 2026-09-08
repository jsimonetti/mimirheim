# open-meteo — PV power forecast fetcher

**pv_openmeteo** is a standalone daemon that turns the [Open-Meteo](https://open-meteo.com) weather forecast into a solar power forecast and publishes it to the mimirheim PV forecast input topics in the format mimirheim expects.

It is an alternative to the [forecast.solar](../forecast.solar/README.md) helper. Where forecast.solar returns a finished power estimate, Open-Meteo returns irradiance and temperature and the modelling happens locally, in the [`open-meteo-solar-forecast`](https://github.com/rany2/open-meteo-solar-forecast) library. That is what makes the difference between the two helpers:

| | forecast.solar | Open-Meteo |
|---|---|---|
| Resolution | hourly | 15 minutes, matching the mimirheim solver grid |
| Rate limit | 60 requests/hour on the free tier | several thousand requests/day, free |
| Modelling | opaque, server-side | local, and every parameter is configurable |
| Multi-plane arrays | one call and one topic per plane | planes are summed into one array before publishing |
| Weather model choice | none | any Open-Meteo model, or automatic selection |

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Arrays and planes](#3-arrays-and-planes)
4. [Configuration](#4-configuration)
5. [Output format](#5-output-format)
6. [Running](#6-running)
7. [Fault tolerance](#7-fault-tolerance)
8. [Scheduling](#8-scheduling)

---

## 1. Purpose

mimirheim requires a fresh PV forecast payload on each configured array's `topic_forecast` before each solve cycle. This tool fills that input by:

1. Waiting for a message on its trigger topic.
2. Calling the Open-Meteo forecast API once per plane.
3. Converting irradiance and temperature into AC power, plane by plane, and summing the planes of each array.
4. Publishing each array's forecast — retained — to its configured output topic.
5. Optionally publishing to mimirheim's trigger topic so that mimirheim runs a new solve immediately.

## 2. How it works

### Trigger model

The tool subscribes to a single MQTT trigger topic and acts on every message received. It does not poll on a timer. The [scheduler helper](../../scheduler/) should trigger it every 30 minutes or so; see [Scheduling](#8-scheduling).

### From irradiance to power

For each plane the library requests the 15-minute global tilted irradiance, ambient temperature and snow depth at the plane's coordinates, tilt and orientation, then computes power as

```
P = peak_power_kwp * (GTI / 1000) * (1 + alpha * (T_cell - 25)) * efficiency_factor
```

where the cell temperature `T_cell` comes from the ambient temperature and the irradiance via the Ross model. In plain terms: output scales with the light actually falling on the panel, and warm panels produce slightly less than cold ones at the same irradiance. Optional per-plane corrections for horizon shading, snow cover and morning or evening damping are applied on top.

The result is clamped to the inverter's AC rating: per plane when each plane has its own inverter, or once on the summed output when the planes share one. The distinction matters. An east-west roof on a single 5 kW inverter never has both halves at peak simultaneously, so the shared clamp binds much less often than the sum of the module ratings would suggest.

### Average power, not instantaneous power

mimirheim schedules average power over each step, so this tool publishes the average across each quarter hour rather than the instantaneous value at its start. On the steep parts of the morning and evening curve the two differ noticeably, and the instantaneous series overstates production.

### Confidence assignment

Steps further ahead are less reliable. The tool applies a configurable decay schedule based on how far ahead each step is at the time of the fetch:

| Hours ahead | Default confidence |
|-------------|--------------------|
| 0-6 | 0.90 |
| 6-24 | 0.75 |
| 24-48 | 0.55 |
| 48+ | 0.35 |

What mimirheim does with them today is narrower than the payload suggests. The objective weights each step by `SolveBundle.horizon_confidence`, which the readiness layer sources from the price steps alone; `resample_power` reduces a PV forecast to plain power values. These confidences therefore go no further than the retained payload: not into the solve bundle, the debug dumps, or the schedule. Setting them correctly still matters: it is what the input contract asks for, and they become live the moment PV confidence is folded into the weighting.

## 3. Arrays and planes

A **plane** is a coplanar group of modules: one tilt, one orientation, one location. An east-west roof is two planes.

An **array** is what mimirheim can control: one entry under `pv_arrays` in the mimirheim config, one forecast topic, one curtailment target. Group planes into an array when they are metered and controlled together, which in practice means when they hang off the same inverter.

```
arrays:
  solaredge:          -> mimir/input/pv/solaredge/forecast
    planes: east 25 deg, west 25 deg, east 45 deg, west 45 deg
  enphase:            -> mimir/input/pv/enphase/forecast
    planes: east 45 deg, west 45 deg
```

One array is one library call regardless of how many planes it holds; the library issues one API request per plane and sums the results.

## 4. Configuration

See [`../../examples/pv-openmeteo.yaml`](../../examples/pv-openmeteo.yaml) for a fully annotated example. The shape:

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: mimir-pv-openmeteo

trigger_topic: mimir/input/tools/pv/trigger

open_meteo:
  api_key: null
  base_url: https://api.open-meteo.com
  weather_model: null
  forecast_days: 3
  past_hours: 1.0

site:
  latitude: 52.37
  longitude: 4.89

arrays:
  roof:
    inverter_kwp: 5.0
    planes:
      - {declination: 35, azimuth: -90, peak_power_kwp: 3.1, efficiency_factor: 0.9}
      - {declination: 35, azimuth:  90, peak_power_kwp: 3.1, efficiency_factor: 0.9}
```

### Top level

| Field | Type | Description |
|-------|------|-------------|
| `mqtt.host` | string | MQTT broker hostname or IP address |
| `mqtt.port` | integer | MQTT broker port. Default `1883` |
| `mqtt.client_id` | string | MQTT client identifier. Default `mimir-pv-openmeteo` |
| `mqtt.username` | string | Optional broker username |
| `mqtt.password` | string | Optional broker password |
| `trigger_topic` | string | MQTT topic that triggers a fetch-and-publish cycle |
| `mimir_topic_prefix` | string | mimirheim's `mqtt.topic_prefix`. Default `mimir`. Used to derive the array output topics and the trigger topic |
| `signal_mimir` | boolean | Publish to `mimir_trigger_topic` once per cycle in which at least one array was published. A cycle where every array failed sends nothing. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Defaults to `{mimir_topic_prefix}/input/trigger` |
| `ha_discovery` | map | Home Assistant MQTT discovery. When enabled, publishes a button entity that fires the trigger topic, and (unless `forecast_sensor: false`) one forecast sensor per array, all grouped under a single HA device |
| `stats_topic` | string | When set, per-cycle run statistics are published here |

### `open_meteo`

| Field | Type | Description |
|-------|------|-------------|
| `api_key` | string or null | API key for a commercial subscription. `null` uses the free public endpoint |
| `base_url` | string | API base URL. `https://customer-api.open-meteo.com` for a commercial subscription, or a self-hosted instance |
| `weather_model` | string or null | Open-Meteo model name, for example `icon_seamless`. `null` lets Open-Meteo choose per location |
| `forecast_days` | integer 1-16 | Days of forecast to request, including today. Default `3` |
| `past_hours` | float 0-48 | Hours of already-elapsed forecast kept in the payload, so mimirheim has a step at or before the start of its horizon. Default `1.0` |

### `site`

| Field | Type | Description |
|-------|------|-------------|
| `latitude` | float | Site latitude in decimal degrees (positive = north) |
| `longitude` | float | Site longitude in decimal degrees (positive = east) |

### `arrays.<name>`

The map key is used as the mimirheim `pv_arrays` device name when deriving the output topic.

| Field | Type | Description |
|-------|------|-------------|
| `output_topic` | string | MQTT topic for this array's forecast, published retained. Defaults to `{mimir_topic_prefix}/input/pv/{key}/forecast` |
| `inverter_kwp` | float or null | AC limit of the inverter shared by every plane in the array, in kW. Mutually exclusive with the per-plane `inverter_kwp` |
| `planes` | list | At least one plane |

### `arrays.<name>.planes[]`

| Field | Type | Description |
|-------|------|-------------|
| `label` | string | Optional name, used in log messages only |
| `declination` | float 0-90 | Panel tilt in degrees from horizontal. 0 = flat, 90 = vertical |
| `azimuth` | float -180-180 | Panel orientation in degrees from south. 0 = south, -90 = east, 90 = west. Subtract 180 from a compass bearing |
| `peak_power_kwp` | float > 0 | Nameplate DC power of this plane in kWp |
| `inverter_kwp` | float or null | AC limit of this plane's own inverter in kW. Use for micro-inverters or one inverter per string |
| `efficiency_factor` | float 0-1 | Everything that scales linearly between nameplate and AC terminals: inverter efficiency, cabling, soiling, degradation. Default `1.0` |
| `tracking` | enum | `none`, `azimuth`, `tilt` or `dual`. A tracked axis makes the corresponding fixed angle irrelevant. Default `none` |
| `damping_morning` | float 0-1 | Fraction of production removed at sunrise, tapering linearly to none at solar noon. Default `0.0` |
| `damping_evening` | float 0-1 | The same for the afternoon, from none at solar noon to the full fraction at sunset. Default `0.0` |
| `max_snowcover_depth_cm` | float >= 0 | Snow depth at which the plane produces nothing, with linear derating up to it. `0.0` disables the correction |
| `horizon` | list or null | Skyline profile: at least two `{azimuth, elevation}` points in ascending order. Setting it enables horizon shading; omitting it disables it |
| `partial_shading` | boolean | While the horizon blocks the direct beam, still credit diffuse light rather than dropping to zero. No effect without `horizon`. Default `false` |
| `latitude`, `longitude` | float | Override the site coordinates for this plane. All planes must resolve to the same UTC offset |

Note the two azimuth conventions. Panel azimuth is measured from **south** (the Open-Meteo convention: 0 = south, -90 = east, 90 = west), while horizon points use **compass bearings** (0 = north, 90 = east, 180 = south), because that is how a horizon survey is recorded. A panel azimuth of 90 in Home Assistant's east-facing sense is `-90` here.

### `confidence_decay`

| Field | Type | Description |
|-------|------|-------------|
| `hours_0_to_6` | float 0-1 | Confidence for steps 0-6 hours ahead. Default `0.90` |
| `hours_6_to_24` | float 0-1 | Confidence for steps 6-24 hours ahead. Default `0.75` |
| `hours_24_to_48` | float 0-1 | Confidence for steps 24-48 hours ahead. Default `0.55` |
| `hours_48_plus` | float 0-1 | Confidence for steps beyond 48 hours. Default `0.35` |

## 5. Output format

One payload per array, published retained at QoS 1 to that array's `output_topic`. Steps are at the API's native 15-minute resolution, which is also the mimirheim solver grid.

```json
[
  {"ts": "2026-03-30T06:00:00+00:00", "kw": 0.0,  "confidence": 0.9},
  {"ts": "2026-03-30T06:15:00+00:00", "kw": 0.12, "confidence": 0.9},
  {"ts": "2026-03-30T06:30:00+00:00", "kw": 0.41, "confidence": 0.9},
  {"ts": "2026-03-31T00:00:00+00:00", "kw": 0.0,  "confidence": 0.55}
]
```

- `ts` is UTC ISO 8601 with a `+00:00` offset, marking the start of the quarter hour.
- `kw` is the average AC power over that quarter hour, never negative.
- `confidence` comes from the decay schedule, applied per step.

Night-time steps are present with `kw: 0.0`. Open-Meteo returns a dense series, so there are no gaps for mimirheim to bridge.

## 6. Running

```bash
uv run python -m pv_openmeteo --config mimirheim_helpers/examples/pv-openmeteo.yaml
```

### Systemd unit example

```ini
[Unit]
Description=mimirheim Open-Meteo PV fetcher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m pv_openmeteo --config /etc/mimirheim/pv-openmeteo.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## 7. Fault tolerance

- **HTTP failure**: the array is skipped and the error logged with a full traceback. The last retained payload stays on the topic.
- **Partial failure**: arrays are independent. A failure on one still lets the others publish, and the log records which succeeded.
- **Empty response**: an array whose forecast contains no steps within the horizon is skipped rather than published. An empty payload would overwrite a usable retained forecast with something mimirheim rejects.
- **Rate limiting**: on an HTTP 429 the cycle aborts immediately and further triggers are ignored for 15 minutes. Open-Meteo does not report when the limit resets, so the back-off is fixed.
- **MQTT disconnect**: the client reconnects automatically. Triggers missed during a disconnect are not replayed, since the trigger topic is not retained.
- **Renamed or removed array**: the retained forecast on the old topic, and its HA discovery sensor, both stay on the broker. Neither name is known at the next connect, so nothing can clean them up automatically. Delete the retained messages by hand (`mosquitto_pub -r -n -t <topic>`) after renaming an array. This is the same accepted limitation the pv_ml_learner helper has.

## 8. Scheduling

Every 30 minutes is a reasonable cadence: often enough to track changing cloud cover, and nowhere near the free tier's limits at a handful of planes per cycle. Night-time fetches are worth keeping; they publish the zeros that tell the solver not to expect production, and they keep the readiness state current.

```yaml
schedules:
  - "*/30 * * * *": mimir/input/tools/pv/trigger
```
