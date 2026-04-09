# homeassistant — Base load forecast from Home Assistant statistics

**homeassistant** is a standalone daemon that derives a base load forecast from the historical statistics of a power sensor in Home Assistant and publishes it to the mimirheim static load forecast topic.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)
8. [HA prerequisites](#8-ha-prerequisites)

---

## 1. Purpose

mimirheim requires a per-step base load forecast for each configured `static_loads` entry before each solve. The base load is the household's uncontrollable power consumption that mimirheim must account for but cannot influence — fridge, lighting, standby, cooking, etc.

This tool generates that forecast by querying Home Assistant's long-term statistics API for a power sensor. It computes a "typical" load profile by averaging the same time-of-day readings across several recent days, then publishes the result in the mimirheim timestamped-steps format.

---

## 2. How it works

### Trigger model

The tool subscribes to a single MQTT trigger topic and acts on every message. It calls the HA REST API on demand — it does not poll continuously. Because base load patterns are stable on daily timescales, triggering once per day (e.g. at midnight) is usually sufficient to keep the forecast current.

### Forecasting method

The tool uses a **same-hour average over recent days** as the base load forecast for each future hour:

1. It queries HA's statistics API for hourly mean values of all configured entities over the last `lookback_days` days.
2. For each configured entity in `sum_entities`, it computes the mean power across all available same-hour readings in the lookback window. It does the same for each entity in `subtract_entities`.
3. For each hour of the day (0–23) it computes:
   `net_kw[h] = sum(mean(sum_entities[h])) - sum(mean(subtract_entities[h]))`
   The result is clamped to zero — baseload is never negative.
4. This 24-hour profile is then repeated to fill the full `horizon_hours` window, starting from the current wall-clock hour.

This is a simple but robust approach. It produces reasonable forecasts without requiring machine learning, handles seasonal variation only coarsely (a longer `lookback_days` averages it out), and degrades gracefully when some historical data is missing.

### Power sensor units

HA sensors commonly report power in watts (`W`) or kilowatts (`kW`). The tool converts to kilowatts before publishing, using the `unit` field in config.

---

## 3. Configuration

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: ha-baseload
  # username: user
  # password: secret

trigger_topic: mimir/input/tools/baseload/trigger
output_topic: mimir/input/base    # must match static_loads.*.topic_forecast in mimirheim config

homeassistant:
  url: http://homeassistant.local:8123
  token: your-long-lived-access-token
  sum_entities:                           # entities whose readings are summed
    - sensor.power_phase_l1_w            # e.g. three-phase clamp sensors
    - sensor.power_phase_l2_w
    - sensor.power_phase_l3_w
  subtract_entities:                      # entities subtracted from the sum
    - sensor.battery_active_power_w      # subtract steered loads so they are not
    - sensor.pv_active_power_w           # double-counted by mimirheim
  unit: W                                 # W or kW — applies to all entities
  lookback_days: 7                        # number of days of history to average over
  horizon_hours: 48                        # how many hours of forecast to publish

signal_mimir: false
mimir_trigger_topic: mimir/input/trigger   # required only when signal_mimir: true
```

All fields are required unless a default is shown. The tool rejects unknown fields.

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `mqtt.host` | string | MQTT broker hostname or IP address |
| `mqtt.port` | integer | MQTT broker port. Default: `1883` |
| `mqtt.client_id` | string | MQTT client identifier. Must be unique on the broker |
| `mqtt.username` | string | Optional broker username |
| `mqtt.password` | string | Optional broker password |
| `trigger_topic` | string | MQTT topic that triggers a fetch-and-publish cycle |
| `output_topic` | string | MQTT topic to publish the base load forecast to (retained). Must match the corresponding `static_loads.*.topic_forecast` in mimirheim config |
| `homeassistant.url` | string | Base URL of your HA instance, including scheme and port |
| `homeassistant.token` | string | A Long-Lived Access Token generated in HA (Profile → Long-Lived Access Tokens) |
| `homeassistant.sum_entities` | list of strings | Entity IDs whose hourly mean power is summed to form the gross load. At least one entity is required |
| `homeassistant.subtract_entities` | list of strings | Entity IDs whose hourly mean power is subtracted from the sum. Use this to remove steered loads (battery, PV, deferred loads) that mimirheim already schedules. May be empty |
| `homeassistant.unit` | string | Unit reported by all entities: `W` or `kW`. The tool converts to kW before publishing. All entities must use the same unit |
| `homeassistant.lookback_days` | integer 1–30 | Number of recent days to average. Higher values smooth out anomalies but respond more slowly to lifestyle changes. Default recommended: `7` |
| `homeassistant.horizon_hours` | integer 1–168 | Number of hours of forecast to publish. The 24-hour day profile is repeated to fill the horizon. Default: `24` |
| `signal_mimir` | boolean | Publish to `mimir_trigger_topic` after publishing the base load payload. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

### Getting a Long-Lived Access Token

1. Open Home Assistant.
2. Click your username in the lower-left corner → **Profile**.
3. Scroll down to **Long-Lived Access Tokens** and click **Create Token**.
4. Copy the token and paste it into config. The token is only shown once.

---

## 4. Output format

Published retained to `output_topic`. Steps are hourly. mimirheim resamples to its 15-minute solver grid using linear interpolation.

```json
[
  {"ts": "2026-03-30T14:00:00+00:00", "kw": 0.42},
  {"ts": "2026-03-30T15:00:00+00:00", "kw": 0.38},
  {"ts": "2026-03-30T16:00:00+00:00", "kw": 0.51},
  {"ts": "2026-03-30T17:00:00+00:00", "kw": 0.89},
  {"ts": "2026-03-30T18:00:00+00:00", "kw": 1.23}
]
```

- `ts` is UTC ISO 8601 with `+00:00` offset, marking the start of each hour.
- `kw` is the net forecast power in kilowatts: sum of `sum_entities` minus sum of `subtract_entities`, clamped to zero.
- No `confidence` field is included. mimirheim treats absent confidence as 1.0.
- The forecast covers `horizon_hours` hours from the current wall-clock hour. If `horizon_hours` exceeds 24, the 24-hour day profile is tiled to fill the full window.

---

## 5. Running

```bash
uv run python -m mimirheim_helpers.baseload.homeassistant --config mimirheim_helpers/baseload/homeassistant/config.yaml
```

### Systemd unit example

```ini
[Unit]
Description=mimirheim HA base load fetcher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m mimirheim_helpers.baseload.homeassistant --config /etc/mimirheim/baseload_ha.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 6. Fault tolerance

- **HTTP failure**: If the HA REST API returns an error (network failure, 401, 500), the cycle is aborted and the error is logged. The last retained payload on `output_topic` remains unchanged.
- **Insufficient history**: If fewer than `lookback_days` days of data are available (e.g. on first run), the tool computes the average over however many days are available. If no history exists at all for a given hour, the tool falls back to the mean across all available readings. This fallback is logged at `WARNING` level.
- **Missing hours**: HA statistics can have gaps. The tool fills missing hours using linear interpolation from the nearest available readings. If an entire day has no data, that day is excluded from the average.
- **MQTT disconnect**: Reconnects automatically.

---

## 7. Scheduling

Once daily at midnight is the standard cadence, refreshing the forecast for the coming day. If your household load is highly variable (e.g. you work from home some days and not others), updating more frequently does not help — the historical average is a fixed profile. A longer `lookback_days` is a better approach for absorbing variability.

Example scheduler entry:

```yaml
schedules:
  baseload:
    cron: "0 0 * * *"
    trigger_topic: mimir/input/tools/baseload/trigger
```

---

## 8. HA prerequisites

The tool calls the HA statistics API endpoint:

```
GET {url}/api/recorder/statistics_during_period
```

This endpoint requires:

- **HA version 2022.11 or later** — the statistics API was stabilised in this release.
- **The recorder integration enabled** (it is enabled by default in HA). Statistics are stored in the HA SQLite or MariaDB database.
- **Long-term statistics for the entity** — HA automatically stores hourly mean statistics for entities with `state_class: measurement` (which is the correct class for power sensors). If your power sensor does not have this class, HA will not have hourly statistics for it; check the entity in Settings → Entities.
- **The Long-Lived Access Token** must have read access to the statistics API (all HA tokens do by default).

### Verifying your sensor has statistics

In Home Assistant: **Developer Tools → Statistics**. Search for your entity. If it appears, statistics are available and this tool will work. If it does not appear, check the entity's `state_class` attribute.
