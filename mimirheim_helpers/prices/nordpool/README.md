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
  vat_multiplier: 1.0       # Apply VAT/markup; 1.25 adds 25 %. Default 1.0 (no markup).
  grid_tariff_import_eur_per_kwh: 0.0   # Fixed network tariff added to every import step
  grid_tariff_export_eur_per_kwh: 0.0   # Fixed network tariff subtracted from every export step

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
| `nordpool.vat_multiplier` | float ≥ 1.0 | Multiplier applied to every price step. Use this to embed VAT or a fixed markup. Default `1.0` |
| `nordpool.grid_tariff_import_eur_per_kwh` | float ≥ 0 | Flat import network tariff in EUR/kWh added to every step's `import_eur_per_kwh`. Default `0.0` |
| `nordpool.grid_tariff_export_eur_per_kwh` | float ≥ 0 | Flat export network tariff in EUR/kWh subtracted from every step's `export_eur_per_kwh`. Default `0.0` |
| `signal_mimir` | boolean | If `true`, publish an empty message to `mimir_trigger_topic` after publishing the price payload. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

---

## 4. Output format

The tool publishes a JSON array retained to `output_topic`. Each element is one hour:

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
- Import and export prices are equal (Nordpool day-ahead is a single spot price). The `grid_tariff_*` fields allow them to diverge if your network tariff structure requires it.
- `confidence` is always `1.0` — day-ahead prices are confirmed, not estimated.
- mimirheim resamples this hourly array to its 15-minute solver grid using a step function.

---

## 5. Running

```bash
# From the mimirheim repo root:
uv run python -m mimirheim_helpers.prices.nordpool --config mimirheim_helpers/prices/nordpool/config.yaml
```

The process logs to stdout and does not daemonise. Use a process supervisor (systemd, Docker, s6) to run it persistently.

### Systemd unit example

```ini
[Unit]
Description=mimirheim Nordpool price fetcher
After=network.target mosquitto.service

[Service]
WorkingDirectory=/opt/mimirheim
ExecStart=/opt/mimirheim/.venv/bin/python -m mimirheim_helpers.prices.nordpool --config /etc/mimirheim/nordpool.yaml
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
