# homeassistant_db — Base load forecast from the Home Assistant recorder database

**homeassistant_db** is a standalone daemon that derives a base load forecast by querying the Home Assistant recorder database directly (without using the REST API) and publishes it to the mimirheim static load forecast topic.

It supports any database backend that HA supports — SQLite (the default), PostgreSQL, and MariaDB — via a standard [SQLAlchemy](https://www.sqlalchemy.org/) connection URL. No HA API token is required; only read access to the database is needed.

Use this tool instead of `mimirheim_helpers/baseload/homeassistant/` when:

- The HA REST API is unavailable or you prefer not to generate a Long-Lived Access Token.
- The HA instance and this tool run on the same host (or share a network-accessible database).
- You use PostgreSQL or MariaDB as the HA recorder backend and want direct database access.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)
8. [Database prerequisites](#8-database-prerequisites)

---

## 1. Purpose

mimirheim requires a per-step base load forecast for each configured `static_loads` entry before each solve. The base load is the household's uncontrollable power consumption that mimirheim must account for but cannot influence — fridge, lighting, standby, cooking, etc.

This tool generates that forecast by reading the HA recorder's long-term statistics tables directly. It computes a "typical" load profile by averaging the same time-of-day readings across several recent days, then publishes the result in the mimirheim timestamped-steps format.

---

## 2. How it works

### Trigger model

The tool subscribes to a single MQTT trigger topic and acts on every message. It queries the database on demand — it does not poll continuously. Because base load patterns are stable on daily timescales, triggering once per day (e.g. at midnight) is usually sufficient to keep the forecast current.

### Sensor type detection

Each configured entity is classified as a **power sensor** or an **energy sensor** by looking at its unit of measurement in `statistics_meta.unit_of_measurement`:

| Unit | Sensor type | HA column | Conversion |
|------|------------|-----------|------------|
| `mW`, `W`, `kW`, `MW`, `GW` | power | `mean` | multiply by power-unit multiplier |
| `Wh`, `kWh`, `MWh` | energy | `sum` delta | multiply by energy-unit multiplier |

**Power sensors** use the `mean` column — the HA recorder writes the average value over the hour. A reading of 500 W becomes 0.5 kWh/h.

**Energy sensors** use the `sum` column — the HA recorder writes the cumulative count of energy absorbed. The per-hour energy is the consecutive difference between adjacent rows. To ensure the first bucket in the lookback window can be computed, one extra row before the window start is fetched. Negative deltas (which can only result from recorder data corruption, since HA normalises counter resets internally) are discarded.

When a `unit` override is set in the entity config (see below), that value takes precedence over the database unit for type detection.

An entity whose unit is not in either table raises an error that names the unsupported unit.

### Outlier detection

After extracting values, each entity's readings are passed through a **P99-based outlier filter** before being averaged:

1. All extracted values for the entity are collected across the full lookback window.
2. If the entity has **fewer than 24 valid samples**, detection is skipped and all values are passed through unchanged. A warning is logged. With fewer than one full day of data the statistical threshold would be unreliable.
3. The **99th percentile (P99)** is computed. P99 is used rather than P95 because sensors with infrequent high-consumption cycles (e.g. a washing machine idle at 0.5 Wh/h, active at 1 kWh/h) can have P95 fall deep in the idle range, causing legitimate wash cycle readings to be flagged as outliers. P99 is still robust against genuinely corrupt values: with 1344 hourly readings (56 days), two corrupt values represent just 0.15% of the data and do not influence P99.
    4. **Zero-inflation guard**: if P99 == 0.0 — device idle ≥99% of the time — the P99 of only the non-zero values is used instead. This prevents a threshold of 0 from flagging all non-zero readings as outliers.
    5. `threshold = P99_effective × outlier_factor` (default `outlier_factor` is **10.0**). At this factor, a Marstek battery with P99 ≈ 2 kWh/h gives threshold = 20 kWh/h, catching the 250 kWh/h and 119 kWh/h corrupt readings with an order-of-magnitude margin.
6. Any reading exceeding the threshold is **dropped** (not clamped). Dropping makes the decision explicit; clamping would silently corrupt the hour-of-day average.
7. Every dropped reading is logged at WARNING level with entity ID, timestamp, actual value, and threshold.

### Forecasting method

After outlier filtering, all readings are in kWh/h regardless of the original sensor type. The tool computes a **same-hour-of-day average over recent days** as the base load forecast:

1. For each configured entity, group its clean hourly kWh/h readings by hour-of-day (0–23).
2. Compute the weighted mean kWh/h at each hour across the lookback window. When `lookback_decay > 1.0`, more recent days contribute more weight.
3. For each hour compute: `net_kw[h] = sum(sum_entities[h]) − sum(subtract_entities[h])`, clamped to zero.
4. Tile the 24-hour profile to fill the full `horizon_hours` window starting from the current wall-clock hour.

This is a simple but robust approach. It produces reasonable forecasts without requiring machine learning and degrades gracefully when some historical data is missing.

### Database access

The tool opens a read-only connection via SQLAlchemy and executes `SELECT` queries only. It queries two tables:

- `statistics_meta` — maps entity IDs to integer primary keys and stores `unit_of_measurement`.
- `statistics` — one row per entity per hour, with a Unix timestamp (`start_ts`), a `mean` column (power sensors), and a `sum` column (energy sensors).

Both tables are present in all HA recorder backends (SQLite, PostgreSQL, MariaDB) from HA 2023.3 onwards. The SQL used is standard and runs without modification across all three backends.

---

## 3. Configuration

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: ha-baseload-db
  # username: user
  # password: secret

trigger_topic: mimir/input/tools/baseload/trigger
output_topic: mimir/input/base    # must match static_loads.*.topic_forecast in mimirheim config

homeassistant:
  # SQLAlchemy connection URL for the HA recorder database.
  # See "Database URL formats" below for examples.
  db_url: sqlite:////config/home-assistant_v2.db

  sum_entities:                           # entities whose readings are summed
    - entity_id: sensor.energy_consumption_tarif_1   # kWh entity — auto-detected
    - entity_id: sensor.energy_consumption_tarif_2
    - entity_id: sensor.keuken_vaatwasser_energy
  subtract_entities:                      # entities subtracted from the sum
    - entity_id: sensor.marstek_total_discharging_energy
      outlier_factor: 5.0               # tighter threshold for a 2.5 kW inverter
  lookback_days: 56                       # number of days of history to average over
  horizon_hours: 48                       # how many hours of forecast to publish

signal_mimir: false
mimir_trigger_topic: mimir/input/trigger   # required only when signal_mimir: true
```

Each entry in `sum_entities` and `subtract_entities` requires only `entity_id`. The tool auto-detects the unit from `statistics_meta.unit_of_measurement`. Two optional fields are available per entity:

```yaml
sum_entities:
  - entity_id: sensor.power_phase_l1_w
    unit: W                    # override the detected unit (use when DB unit is wrong)
    outlier_factor: 5.0        # tighten the P99 threshold (default 10.0)
```

### Database URL formats

The `db_url` field is a standard SQLAlchemy connection URL.

| Backend | URL format | Notes |
|---------|-----------|-------|
| SQLite | `sqlite:////absolute/path/to/home-assistant_v2.db` | Four slashes: three for the URL scheme, one to start the absolute path. The typical path inside HA OS is `/config/home-assistant_v2.db` |
| PostgreSQL | `postgresql+psycopg2://user:pass@host/dbname` | Requires `psycopg2-binary`. Install with `uv pip install mimirheim-baseload-homeassistant-db[postgres]` |
| MariaDB / MySQL | `mysql+pymysql://user:pass@host/dbname` | Requires `pymysql`. Install with `uv pip install mimirheim-baseload-homeassistant-db[mysql]` |

SQLite is the HA default and requires no extra driver.

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
| `homeassistant.db_url` | string | SQLAlchemy database URL. See table above for format by backend |
| `homeassistant.sum_entities` | list of entity objects | Entities whose hourly kWh/h values are summed to form the gross load. At least one entity is required |
| `homeassistant.subtract_entities` | list of entity objects | Entities whose hourly kWh/h values are subtracted from the sum. Use this to remove steered loads (battery, PV, deferred loads) that mimirheim already schedules. May be empty |
| `entity.entity_id` | string | Home Assistant entity ID, e.g. `sensor.energy_consumption` |
| `entity.unit` | string, optional | Unit override. When set, used instead of the database-stored unit. Accepted power units: `mW`, `W`, `kW`, `MW`, `GW`. Accepted energy units: `Wh`, `kWh`, `MWh`. Omit to auto-detect |
| `entity.outlier_factor` | float > 0, optional | P99 threshold multiplier for outlier detection. Default: `10.0`. Lower to tighten the threshold for sensors with a known physical maximum |
| `homeassistant.lookback_days` | integer 1–112 | Number of recent days to average. Default: `7` |
| `homeassistant.lookback_decay` | float ≥ 1.0 | Recency weighting. `1.0` is a plain average; `2.0` weights the most recent day twice as heavily as the oldest. Default: `1.0` |
| `homeassistant.horizon_hours` | integer 1–168 | Number of hours of forecast to publish. The 24-hour day profile is repeated to fill the horizon. Default: `48` |
| `signal_mimir` | boolean | Publish to `mimir_trigger_topic` after publishing the base load payload. Default: `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

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

Install dependencies and run from the tool directory:

```bash
cd mimirheim_helpers/baseload/homeassistant_db
uv sync --group dev
uv run python -m baseload_ha --config config.yaml
```

For PostgreSQL or MariaDB, install the matching driver extra first:

```bash
uv pip install mimirheim-baseload-homeassistant-db[postgres]   # PostgreSQL
uv pip install mimirheim-baseload-homeassistant-db[mysql]      # MariaDB/MySQL
```

---

## 6. Fault tolerance

- **Database unreachable**: If the database file does not exist, the connection is refused, or a SQL error occurs, the cycle is aborted and the error is logged at `ERROR` level with full traceback. The last retained payload on `output_topic` remains unchanged.
- **Insufficient history**: If fewer than `lookback_days` days of data are available (e.g. on first run), the tool computes the average over however many days are available. If no history exists at all for a given hour, it falls back to the mean across all available readings.
- **Missing hours**: HA statistics can have gaps. Individual missing hours within the lookback window are simply absent from the average; they do not prevent the forecast from being built.
- **MQTT disconnect**: Reconnects automatically.

---

## 7. Scheduling

Once daily at midnight is the standard cadence. If your household load is highly variable, a longer `lookback_days` is a better approach than updating more frequently.

Example scheduler entry:

```yaml
schedules:
  baseload:
    cron: "0 0 * * *"
    trigger_topic: mimir/input/tools/baseload/trigger
```

---

## 8. Database prerequisites

### HA recorder must be enabled

The recorder integration is enabled by default in HA. It writes long-term statistics to the database automatically for sensors with `state_class: measurement` (the correct class for power sensors).

### HA version

The `statistics` and `statistics_meta` tables with the `start_ts` (Unix float) column schema were introduced in **HA 2023.3**. Earlier versions stored timestamps differently. This tool requires HA 2023.3 or later.

### SQLite access

When HA runs on the same host, mount or copy `/config/home-assistant_v2.db` to a path this tool can read. For Home Assistant OS running on a separate device, you can:

- Mount the HA `/config` volume via NFS or SSHFS.
- Copy the database periodically with `scp` or `rsync` (a stale copy is still useful for the daily baseload average).
- Run this tool inside the same container as HA, with the database path mounted.

### Database user permissions (PostgreSQL / MariaDB)

The database user in `db_url` requires only `SELECT` on the `statistics` and `statistics_meta` tables. No write access is needed.

```sql
-- PostgreSQL example
GRANT SELECT ON statistics, statistics_meta TO ha_reader;

-- MariaDB example
GRANT SELECT ON homeassistant.statistics TO 'ha_reader'@'%';
GRANT SELECT ON homeassistant.statistics_meta TO 'ha_reader'@'%';
```

| `homeassistant.unit` | string | Unit reported by all entities: `W` or `kW`. The tool converts to kW before publishing. All entities must use the same unit |
| `homeassistant.lookback_days` | integer 1–112 | Number of recent days to average. Higher values smooth out anomalies but respond more slowly to lifestyle changes. Default recommended: `7` |
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
