# nordpool — Day-ahead electricity price fetcher

**nordpool** is a standalone daemon that fetches day-ahead electricity prices from the Nordpool data portal and publishes them to the mimirheim prices input topic in the exact format mimirheim expects.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)

---

## 1. Purpose

mimirheim requires a fresh prices payload on `{prefix}/input/prices` before each solve cycle. The nordpool tool fills that input by:

1. Waiting for a message on its trigger topic.
2. Fetching today's confirmed day-ahead prices from the Nordpool data portal via HTTP.
3. Fetching tomorrow's prices if they are already published (typically available from ~13:00 CET on weekdays).
4. Publishing the combined payload — retained — to the configured output topic.
5. Optionally publishing to mimirheim's trigger topic so that mimirheim runs a new solve immediately.

---

## 2. How it works

### Trigger model

nordpool runs as a persistent daemon and subscribes to a single MQTT trigger topic. It does not poll on a timer internally. A separate scheduler (see `mimirheim_helpers/scheduler/`) publishes to the trigger topic on whatever schedule is appropriate for your setup — typically once in the early afternoon after day-ahead prices are published, and again at midnight to roll the horizon.

### Price retrieval

The tool uses the `pynordpool` library, which wraps the Nordpool data portal REST API.

- A **single API call** requests today's and tomorrow's prices together.
- If tomorrow's prices are not yet published (typically before ~12:42 CET on weekdays), the call silently returns today's prices only. No special handling is needed in configuration or scheduling.
- Prices are in EUR/MWh from the API and are divided by 1000 to produce EUR/kWh for mimirheim.
- Only steps at or after the current UTC hour are included in the published payload.
- Day-ahead prices are confirmed prices: all steps are published with `confidence: 1.0`.

### Price interval

Nordpool has quoted day-ahead prices on a **15-minute** market time unit for most areas since October 2025, and that is what the API returns. `nordpool.price_interval` controls what the tool publishes.

| Value | Behaviour |
|-------|-----------|
| `quarter_hourly` (default) | Publish the raw market time unit unchanged, one step per quarter hour. |
| `hourly` | Average each clock hour into one step. |

Set `price_interval: hourly` when your supplier bills **one dynamic price per whole hour**. Those suppliers derive the hourly rate as the arithmetic mean of the four quarter-hour spot prices, so aggregating here gives mimirheim the price the meter is actually settled on. Leaving the default `quarter_hourly` on an hourly contract makes the optimiser charge and discharge to chase intra-hour price swings that never reach the bill.

Aggregation details:

- Buckets are aligned to the UTC clock. Every CET and CEST offset is a whole number of hours, so a UTC-aligned bucket is also a local-clock-aligned bucket.
- The mean is taken on the **raw spot price, before** `import_formula` and `export_formula` run. The supplier prices the hourly average, so the formula must be applied to that average — this matters for any formula that is not a straight affine function of `price`.
- A partially covered bucket at either end of the horizon is averaged over the periods present rather than dropped, so the payload has no gaps.
- mimirheim resamples the payload onto its own 15-minute solver grid with a hold-previous step function, so each hourly price covers the four solver steps that follow it.

One consequence is worth knowing before switching. mimirheim treats the **last timestamp in the payload as the end of its horizon** (`compute_horizon_steps` in `mimirheim/core/forecast.py`) — it does not extrapolate a step duration past the final entry. An hourly payload whose last step starts at 23:00 therefore ends the price horizon at 23:00, 45 minutes earlier than a quarter-hourly payload whose last step starts at 23:45 — three solver steps. Prices normally reach further ahead than the PV and baseload forecasts, so the joint horizon is rarely bound by this, but those 45 minutes are lost when prices are the shortest series — check `readiness.min_horizon_hours` if a solve starts being skipped after the switch.

### Area codes

Nordpool area codes follow the standard two- or four-character format used by the data portal:

| Country | Example areas |
|---------|--------------|
| Norway | `NO1` `NO2` `NO3` `NO4` `NO5` |
| Sweden | `SE1` `SE2` `SE3` `SE4` |
| Denmark | `DK1` `DK2` |
| Finland | `FI` |
| Netherlands | `NL` |
| Germany | `DE-LU` |
| Belgium | `BE` |

---

## 3. Configuration

Create a `config.yaml` alongside the tool (or pass any path with `--config`):

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: nordpool-prices
  # username and password are optional
  # username: user
  # password: secret

trigger_topic: mimir/input/tools/prices/trigger
output_topic: mimir/input/prices

nordpool:
  area: NO2                 # Nordpool price area code
  import_formula: "price"   # Python expression for the all-in import price in EUR/kWh
  export_formula: "price"   # Python expression for the net export price in EUR/kWh
  price_interval: quarter_hourly   # or "hourly" for an hourly dynamic tariff

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
| `output_topic` | string | MQTT topic to publish the price payload to (retained) |
| `nordpool.area` | string | Nordpool price area code |
| `nordpool.import_formula` | string | Python expression for the all-in import price in EUR/kWh. Variables: `price` (raw spot, EUR/kWh), `ts` (UTC-aware `datetime`). Default `"price"` |
| `nordpool.export_formula` | string | Python expression for the net export price in EUR/kWh. Same variables as `import_formula`. Default `"price"` |
| `nordpool.price_interval` | `quarter_hourly` or `hourly` | Length of one published price step. `quarter_hourly` publishes the raw Nordpool market time unit; `hourly` averages each clock hour into one step for suppliers that bill an hourly dynamic price. Default `quarter_hourly` |
| `signal_mimir` | boolean | If `true`, publish an empty message to `mimir_trigger_topic` after publishing the price payload. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

---

## 4. Output format

The tool publishes a JSON array retained to `output_topic`. Each element is one price step of `nordpool.price_interval` (the example below uses `hourly`):

```json
[
  {
    "ts": "2026-03-30T13:00:00+00:00",
    "import_eur_per_kwh": 0.2234,
    "export_eur_per_kwh": 0.2234,
    "confidence": 1.0
  },
  {
    "ts": "2026-03-30T14:00:00+00:00",
    "import_eur_per_kwh": 0.2187,
    "export_eur_per_kwh": 0.2187,
    "confidence": 1.0
  }
]
```

- `ts` is the start of the price period in UTC (ISO 8601 with offset `+00:00`).
- Import and export prices come from the same raw spot price; `import_formula` and `export_formula` are what make them diverge.
- `confidence` is always `1.0` — day-ahead prices are confirmed, not estimated.
- mimirheim resamples this array onto its 15-minute solver grid using a hold-previous step function, so a coarser payload simply repeats each price across the steps it covers.

---

## 5. Running

```bash
# From the mimirheim repo root:
uv run python -m nordpool --config mimirheim_helpers/prices/nordpool/config.yaml
```

The process logs to stdout and does not daemonise. Use a process supervisor (systemd, Docker, s6) to run it persistently.

### Systemd unit example

```ini
[Unit]
Description=mimirheim Nordpool price fetcher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m nordpool --config /etc/mimirheim/nordpool.yaml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

---

## 6. Fault tolerance

- **HTTP failure**: If the Nordpool API returns an error or the request times out, the tool logs the error at `ERROR` level and does not publish. The existing retained payload on `output_topic` (if any) remains unchanged — mimirheim continues using the last known prices.
- **Tomorrow not yet published**: The API silently returns today's prices only when tomorrow's prices have not been published yet. The tool publishes what it has; mimirheim solves over a shorter horizon until tomorrow's prices arrive.
- **MQTT disconnect**: The tool reconnects automatically using paho-mqtt's built-in reconnect loop. Trigger messages that arrive during a disconnect are not replayed (the trigger topic is not retained). The scheduler will send the next trigger on schedule.
- **Invalid price data**: If the API response cannot be parsed or contains negative prices, the cycle is aborted and the error is logged. No partial payload is published.

---

## 7. Scheduling

The nordpool tool does not contain an internal timer. Pair it with the scheduler tool or an external cron job.

### Recommended schedule

Nordpool publishes day-ahead prices for the next calendar day at approximately 12:42 CET (11:42 UTC) on weekdays. A robust schedule triggers twice:

1. **14:00 UTC daily** — fetches today + tomorrow. This slightly compensates for occasional late publication.
2. **00:05 UTC daily** — midnight rollover. Refreshes the payload so the horizon covers a full day ahead from midnight.

Example scheduler entry (see `mimirheim_helpers/scheduler/config.yaml`):

```yaml
schedules:
  prices_afternoon:
    cron: "0 14 * * *"
    trigger_topic: mimir/input/tools/prices/trigger
  prices_midnight:
    cron: "5 0 * * *"
    trigger_topic: mimir/input/tools/prices/trigger
```
