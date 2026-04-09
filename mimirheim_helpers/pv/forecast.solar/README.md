# forecast.solar — PV power forecast fetcher

**forecast.solar** is a standalone daemon that fetches solar power generation forecasts from the [forecast.solar](https://forecast.solar) public API and publishes them to the mimirheim PV forecast input topic in the exact format mimirheim expects.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)
8. [API tiers](#8-api-tiers)

---

## 1. Purpose

mimirheim requires a fresh PV forecast payload on each configured array's `topic_forecast` before each solve cycle. This tool fills that input by:

1. Waiting for a message on its trigger topic.
2. Calling the forecast.solar estimate API for each configured array.
3. Converting the response to the mimirheim timestamped-steps format.
4. Publishing each array's forecast — retained — to its configured output topic.
5. Optionally publishing to mimirheim's trigger topic so that mimirheim runs a new solve immediately.

---

## 2. How it works

### Trigger model

The tool subscribes to a single MQTT trigger topic and acts on every message received. It does not poll on a timer. The scheduler tool (see `mimirheim_helpers/scheduler/`) should trigger it every 1–3 hours during daylight and once before midnight to capture the overnight zero-production period.

### API call

For each configured array the tool calls:

```
GET https://api.forecast.solar/estimate/{lat}/{lon}/{dec}/{az}/{kwp}
```

| Parameter | Description |
|-----------|-------------|
| `lat` | Site latitude in decimal degrees (positive = north) |
| `lon` | Site longitude in decimal degrees (positive = east) |
| `dec` | Panel declination: angle from horizontal in degrees (0 = flat, 90 = vertical) |
| `az` | Panel azimuth: deviation from south in degrees (0 = south, −90 = east, 90 = west) |
| `kwp` | Array peak power in kWp |

With a paid API key, the URL gains a key prefix: `https://api.forecast.solar/{api_key}/estimate/...`. The key is optional for the free tier.

### Confidence assignment

forecast.solar provides estimates up to several days ahead. Confidence in near-term forecasts is higher than in those further out. The tool applies a configurable decay schedule:

| Hours ahead | Default confidence |
|-------------|--------------------|
| 0–6 | 0.90 |
| 6–24 | 0.75 |
| 24–48 | 0.55 |
| 48+ | 0.35 |

These defaults can be overridden in config. They are applied per-step based on how far ahead that step is relative to the time of the fetch.

### Multiple arrays

Each entry under `arrays` is an independent array with its own geometry and output topic. The tool publishes a separate payload for each and makes one API call per array.

---

## 3. Configuration

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: forecast-solar-pv
  # username: user
  # password: secret

trigger_topic: mimir/input/tools/pv/trigger

forecast_solar:
  api_key: null   # null = free tier (60 requests/hour, 1-day horizon)
                  # paid key = extended horizon and higher rate limits

arrays:
  roof_pv:                           # name is a label for logging only — it can be anything
    output_topic: mimir/input/pv      # must match pv_arrays.<name>.topic_forecast in mimirheim config
    latitude: 52.37
    longitude: 4.89
    declination: 35                  # degrees from horizontal
    azimuth: 0                       # 0 = south, -90 = east, 90 = west
    peak_power_kwp: 5.0

  garage_pv:
    output_topic: mimir/input/garage-pv
    latitude: 52.37
    longitude: 4.89
    declination: 15
    azimuth: -45
    peak_power_kwp: 2.0

confidence_decay:                    # optional — override default confidence by horizon band
  hours_0_to_6: 0.90
  hours_6_to_24: 0.75
  hours_24_to_48: 0.55
  hours_48_plus: 0.35

signal_mimir: false
mimir_trigger_topic: mimir/input/trigger   # required only when signal_mimir: true
```

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `mqtt.host` | string | MQTT broker hostname or IP address |
| `mqtt.port` | integer | MQTT broker port. Default: `1883` |
| `mqtt.client_id` | string | MQTT client identifier. Must be unique on the broker |
| `mqtt.username` | string | Optional broker username |
| `mqtt.password` | string | Optional broker password |
| `trigger_topic` | string | MQTT topic that triggers a fetch-and-publish cycle |
| `forecast_solar.api_key` | string or null | API key for paid tiers. `null` uses the free anonymous endpoint |
| `arrays` | map | Named map of array configurations. Each key is a descriptive name (does not need to match anything in mimirheim config; the `output_topic` does the matching) |
| `arrays.<name>.output_topic` | string | MQTT topic to publish this array's forecast to (retained). Must match the corresponding `pv_arrays.*.topic_forecast` in mimirheim config. The array name itself is a label used only in log messages |
| `arrays.<name>.latitude` | float | Site latitude in decimal degrees |
| `arrays.<name>.longitude` | float | Site longitude in decimal degrees |
| `arrays.<name>.declination` | integer 0–90 | Panel tilt angle in degrees from horizontal |
| `arrays.<name>.azimuth` | integer −180–180 | Panel azimuth deviation from south in degrees |
| `arrays.<name>.peak_power_kwp` | float > 0 | Array peak power in kWp |
| `confidence_decay.hours_0_to_6` | float 0–1 | Confidence assigned to steps 0–6 hours ahead. Default `0.90` |
| `confidence_decay.hours_6_to_24` | float 0–1 | Confidence for 6–24 h ahead. Default `0.75` |
| `confidence_decay.hours_24_to_48` | float 0–1 | Confidence for 24–48 h ahead. Default `0.55` |
| `confidence_decay.hours_48_plus` | float 0–1 | Confidence for 48+ h ahead. Default `0.35` |
| `signal_mimir` | boolean | Publish to `mimir_trigger_topic` after all array payloads are published. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

---

## 4. Output format

One payload per array, published retained to each array's `output_topic`. Steps are at the native resolution of the API response (typically hourly). mimirheim resamples to its 15-minute solver grid using linear interpolation.

```json
[
  {"ts": "2026-03-30T06:00:00+00:00", "kw": 0.0,  "confidence": 0.90},
  {"ts": "2026-03-30T07:00:00+00:00", "kw": 0.42, "confidence": 0.90},
  {"ts": "2026-03-30T08:00:00+00:00", "kw": 1.85, "confidence": 0.90},
  {"ts": "2026-03-30T09:00:00+00:00", "kw": 3.12, "confidence": 0.75},
  {"ts": "2026-03-30T22:00:00+00:00", "kw": 0.0,  "confidence": 0.75}
]
```

- `ts` is UTC ISO 8601 with `+00:00` offset.
- `kw` is the forecast power output in kilowatts. Always non-negative.
- `confidence` is from the decay schedule, applied per-step.

---

## 5. Running

```bash
uv run python -m mimirheim_helpers.pv.forecast_solar --config mimirheim_helpers/pv/forecast.solar/config.yaml
```

### Systemd unit example

```ini
[Unit]
Description=mimirheim forecast.solar PV fetcher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m mimirheim_helpers.pv.forecast_solar --config /etc/mimirheim/forecast_solar.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 6. Fault tolerance

- **HTTP failure**: On any network error or non-200 response, the cycle is aborted. The last retained payload on the MQTT topic remains in place. The error is logged at `ERROR` level.
- **Partial failure**: If the tool is configured with multiple arrays and one API call fails, the successful calls are still published. The failed array retains its previous payload. The log records which arrays succeeded and which failed.
- **Rate limiting**: The free tier allows 60 requests per hour. With multiple arrays and frequent triggers, you may hit this limit. The tool logs a warning when it receives a 429 response and skips publishing for the affected array that cycle.
- **MQTT disconnect**: The tool reconnects automatically. Trigger messages during a disconnect are not replayed (the trigger topic is not retained).

---

## 7. Scheduling

A reasonable forecast cadence for PV:

- **Every 1–3 hours during daylight** (e.g. 06:00, 09:00, 12:00, 15:00, 18:00 UTC in summer) to keep near-term confidence values current.
- **Once at night** (e.g. 03:00 UTC) to pre-populate the next morning's forecast before any morning solve.

Example scheduler entries:

```yaml
schedules:
  pv_morning:
    cron: "0 3 * * *"
    trigger_topic: mimir/input/tools/pv/trigger
  pv_daytime:
    cron: "0 6,9,12,15,18 * * *"
    trigger_topic: mimir/input/tools/pv/trigger
```

---

## 8. API tiers

| Tier | Rate limit | Horizon | API key |
|------|-----------|---------|---------|
| Free | 60 req/h | Today + tomorrow | Not required |
| Personal | 2000 req/day | Up to 4 days | Required |
| Professional | Unlimited | Up to 4 days | Required |

For residential use the free tier is generally sufficient. Set `api_key: null` in config.

See [forecast.solar/pricing](https://forecast.solar/pricing) for current tier details.
