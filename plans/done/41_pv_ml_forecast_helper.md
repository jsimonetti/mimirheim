# Plan 41 — ML-based PV forecast helper (pv_ml_learner)

## Overview

A new input_helper that replaces (or supplements) the forecast.solar tool with a
machine-learning forecast trained on the user's own historical solar production.
It produces an MQTT payload in the exact same format mimirheim already reads, so no
changes to mimirheim core are required.

---

## Critical concerns — read before implementing anything

### 1. Meteoserver API

The weather forecast source is **Meteoserver** ([meteoserver.nl](https://meteoserver.nl)).
A live API call has been made and the response structure is fully documented here.
No further API investigation is needed before implementing `meteoserver_fetcher.py`.

**Endpoint:**
```
GET https://data.meteoserver.nl/api/uurverwachting.php
    ?lat=<latitude>
    &long=<longitude>
    &key=<api_key>
```

Authentication is via query-string API key. No additional headers are required.

**Response structure:**
```json
{
  "plaatsnaam": [{"plaats": "Groenekan"}],
  "data": [
    {
      "tijd": "1775138400",
      "tijd_nl": "02-04-2026 16:00",
      "offset": "22",
      "temp": "8",
      "winds": "4",
      "windb": "3",
      "windknp": "8",
      "windkmh": "14.4",
      "windr": "250",
      "windrltr": "ZW",
      "vis": "30000",
      "neersl": "0.0",
      "luchtd": "1017.3",
      "rv": "72",
      "gr": "177",
      "gr_w": "491",
      "hw": "60",
      "mw": "40",
      "lw": "10",
      "tw": "70",
      "cond": "2",
      "ico": "d2",
      "samenv": "Halfbewolkt"
    }
  ]
}
```

**Field reference** (only the fields used by this tool):

| Field | Type | Meaning | Unit |
|-------|------|---------|------|
| `tijd` | int string | Unix timestamp, **UTC** | seconds |
| `tijd_nl` | string | Human-readable NL local time `DD-MM-YYYY HH:MM` | — |
| `offset` | int string | Hour offset from query time | hours |
| `temp` | int string | Temperature | °C |
| `winds` | int string | Wind speed | m/s |
| `rv` | int string | Relative humidity | % |
| `gr` | int string | Global horizontal irradiance | **W/m²** |
| `tw` | int string | Total cloud cover | % (0–100) |

Note: `gr` is already in W/m² — no conversion is needed. This is confirmed by
cross-checking `tijd` against `tijd_nl`: `1775138400` decodes to
`2026-04-02T14:00:00Z`, which corresponds to `16:00` Dutch local time (CEST = UTC+2),
so `tijd` is a standard UTC Unix timestamp.

The field `gr_w` (watts, approximately 2.77× `gr`) should not be used — its
exact meaning is unclear and `gr` in W/m² is the correct irradiance unit.

**Forecast horizon:** Approximately 54 hours ahead from the time of query.

**Update schedule:** The Meteoserver model updates at 05:30, 11:30, 17:30,
and 23:30 Dutch local time (4× daily). Fetching at :05 past any hour is safe;
no more than 4 fetches per day are meaningful.

### 2. KNMI data access via knmi-py

KNMI historical hourly observations are fetched using the
[**knmi-py**](https://pypi.org/project/knmi-py/) library (version 0.2+), which
wraps the public KNMI script API and returns data as a Pandas DataFrame. This
avoids implementing the raw HTTP + fixed-column text parsing by hand.

```python
import knmi

hourly_df = knmi.get_hour_data_dataframe(
    stations=[260],
    start="2024010101",   # format: YYYYMMDDHH
    end="2024123124",
    variables=["Q", "FH", "T", "RH"],
)
```

Four variables are fetched. Units and conversions:

| Column | Meaning | Raw unit | Converted unit | Conversion |
|--------|---------|----------|----------------|------------|
| `Q` | Global radiation | J/cm²/h | W/m² | `× 10_000 / 3_600` |
| `FH` | Mean hourly wind speed | 0.1 m/s | m/s | `× 0.1` |
| `T` | Temperature at 1.5m | 0.1 °C | °C | `× 0.1` |
| `RH` | Precipitation | 0.1 mm | mm | `× 0.1`; `-1` = trace (<0.05mm) → use `0.0` |

Missing value encoding varies by column:
- `Q == -1`: genuinely missing measurement — drop the entire row.
- `FH`, `T` missing: encoded as `-9999` in KNMI data — set to `None`.
- `RH == -1`: trace precipitation — convert to `0.0`, not `None`.

KNMI hour numbering runs 1–24 (hour 24 = 00:00 of the following calendar day).
knmi-py may or may not normalise this; test explicitly and adjust if needed.

The nearest KNMI station ID must be configured by the user. The station network
is dense enough across the Netherlands that the nearest station is rarely more
than 25 km away, which is acceptable for a diffuse-radiation estimate.

**knmi-py does not require an API key.** It uses the publicly accessible KNMI
script endpoint directly. Remove the `knmi.api_key` field from the config schema.

KNMI data is cheap to backfill and covers decades. The backfill window is not
fixed by the plan — the `knmi_fetcher` always fetches from its earliest missing
hour up to the current hour, bounded only by the HA actuals window (see Critical
Concern 3). There is no user-configurable `lookback_days` for KNMI.

**Publication delay:** KNMI typically publishes quality-checked hourly
observations with a 24–48 hour delay. Data for the most recent 48 hours may be
absent or incomplete via the public script endpoint. The KNMI fetcher must
therefore only request data up to `now - 48h` on each ingest run. This means
the last 48 hours of HA actuals will never have a corresponding KNMI row, and
those hours are automatically excluded from training (no KNMI radiation = no
training row). This is benign: training on data that is two days old is
indistinguishable from training on fully current data for a daily schedule.

### 3. Training window and Home Assistant database access

The training window is bounded entirely by the HA actuals data. KNMI is backfilled
to cover whatever window HA provides, so the effective training window is:

    [earliest HA actual hour, latest HA actual hour]

At first run, `ha_actuals.py` reads the full history from `long_term_statistics`
(no `lookback_days` config — all available data is read). On subsequent runs,
only rows after `get_latest_actuals_ts()` are fetched.

How far back HA data goes is entirely determined by the user's HA installation:
- **Minimum for any model:** 1 year (to see all four seasons at least once).
- **Recommended:** 3 years (to capture inter-year variation in solar generation).
- **Maximum:** No hard cap; more data is always better.

The model checks `count_distinct_months` against `config.training.min_months_required`
(default: 12) and refuses to train if the threshold is not met. The user should
lower this temporarily only during initial deployment when less data is available.

The HA recorder database (SQLite by default, MariaDB optionally) contains two
relevant tables in recent HA versions:

- `long_term_statistics_meta`: Maps statistic IDs to entity/display names.
- `long_term_statistics`: Hourly totals with columns `start_ts`, `statistic_id`,
  `sum` (cumulative kWh), `state` (instantaneous, not needed).

The `sum` column is **cumulative**. To get hourly production, subtract consecutive
`sum` values. Do not use the raw `states` table — it stores instantaneous power
readings at irregular intervals and is unsuitable for energy integration.

Connecting to the HA SQLite file while HA is running is safe for read-only
access. SQLite allows concurrent readers. **Never open with write access.**

The HA database path and the list of sensor entity IDs to sum must be
configurable. Multiple sensors are needed when the user has more than one inverter.

### 4. Cold start and minimum seasonal coverage

XGBoost cannot extrapolate to seasons it has not seen. A model trained on
three summer months will produce near-zero predictions in December. The tool
must:

- Count the number of distinct calendar months represented in the actuals database.
- Refuse to train (log a clear error, publish nothing) if fewer than
  `config.training.min_months_required` distinct months are available.
  The default is **12** (one full year of coverage).
- Log the current distribution of available months at each training run so the
  user can see which months remain missing.

The recommended floor for production use is **12 months** (four full seasons
seen at least once). A minimum of **3 years** of data is advised before the
model is considered reliable for all seasonal conditions; at that point, the
default minimum of 12 months is still the gate, but more data will naturally
be present. There is no maximum lookback enforced — all available HA history
is used.

Users deploying from day one should set `min_months_required: 6` or lower as a
temporary override while data accumulates, accepting reduced accuracy.

### 5. Night-hour detection via measured irradiance

Including thousands of night-hour rows (irradiance = 0, production = 0) will
dominate the training set and bias the model toward predicting zero. Night hours
must be excluded from training, and the inference path must short-circuit them
to 0.0 kW without invoking the model.

The exclusion criterion is based on **measured KNMI irradiance**, not on a
astronomical sunrise/sunset calculation. Using measured data is the correct
approach here: an astronomical calculation tells you when the sun is above the
horizon in theory; measured GHI tells you whether any meaningful radiation
actually reached the panel. These diverge in winter at low sun angles and under
heavy overcast. The KNMI `Q` column already encodes the physical reality at that
station for that hour.

**Threshold: `ghi_wm2 > 5`** (i.e., exclude hours where measured GHI ≤ 5 W/m²).

Rationale for 5 W/m²:
- True astronomical night: GHI = 0 by definition.
- Civil twilight at low sun angles: GHI is 1–5 W/m²; panels produce essentially
  nothing and the relationship to irradiance is dominated by measurement noise.
- A very overcast winter midday might read 8–20 W/m²; those hours are included.
  The model learns that low irradiance produces low output, which is correct.

Edge cases to handle explicitly:
- **KNMI Q = -1 (missing value):** Do not treat as night. Drop the entire row from
  training. Log how many rows are dropped at each ingest cycle.
- **KNMI Q = 0 (explicitly measured zero):** This is genuine night. Exclude.

At inference time, apply the same threshold to the Meteoserver `gr` field:
if `gr ≤ 5`, output `kw = 0.0` directly and skip model inference for that step.
The Meteoserver forecast is not a measurement and can never have Q = -1, so no
missing-value handling is needed at inference time.

### 6. Feature alignment between training and inference

At **training** time, weather features come from **KNMI measured observations**.
At **inference** time, weather features come from the **Meteoserver forecast**.

All four weather features must arrive in identical units at the feature matrix.
The conversions applied in each source:

| Feature   | KNMI raw       | KNMI conversion        | Meteoserver field | Already correct? |
|-----------|----------------|------------------------|-------------------|------------------|
| `ghi_wm2` | Q (J/cm²/h)   | `× 10_000 / 3_600`     | `gr` (W/m²)       | Yes |
| `wind_ms` | FH (0.1 m/s)   | `× 0.1`                | `winds` (m/s)     | Yes |
| `temp_c`  | T (0.1 °C)     | `× 0.1`                | `temp` (°C)        | Yes |
| `rain_mm` | RH (0.1 mm)    | `× 0.1`; `-1` → `0.0` | `neersl` (mm)     | Yes |

Mixing raw KNMI units with Meteoserver units in the same feature vector will
silently corrupt the model. The fetcher modules are responsible for applying
conversions at ingest time so that all stored values are already in final units.
The dataset builder and inference path must never apply any unit conversion
themselves — they read already-converted values from the database.

### 7. Inverter on/off and production-limiting state

Some PV installations include export-limiting, zero-export, or grid-protection
features that reduce actual output below what irradiance would support (e.g. when
the battery is full and grid export is forbidden). Training on those hours without
knowing about the throttling causes the model to learn a spuriously low
irradiance→production relationship.

**Do not include limiting state as a training feature.** The reason is the
inference problem: at prediction time, the limiting state depends on the
future battery SOC and grid export constraint — which is exactly what mimirheim is
trying to optimise. This creates a circular dependency that cannot be resolved.
A feature that requires future system state as its value cannot be used at
inference time.

**The correct solution is exclusion, not conditioning.** Hours where production
was actively limited should be dropped from training entirely, the same way
night hours are dropped. They cannot teach the model the irradiance→production
relationship and should not corrupt it.

To support this, each array in the config accepts an optional list of HA binary
or numeric sensors that indicate when limiting was active:

```yaml
arrays:
  - name: main
    sum_entity_ids:
      - sensor.solaredge_energy_today
    exclude_limiting_entity_ids:
      - binary_sensor.solaredge_export_limited
      - binary_sensor.inverter_grid_protection_active
```

Any training hour where any of these sensors reads `True` (binary) or `> 0`
(numeric) in `long_term_statistics` is excluded from the training set. This
field is optional; most users will not need it. Hours where none of these
sensors has a recorded value are included normally (absence of evidence is not
evidence of limiting).

For users whose inverter limits export for a significant fraction of summer
daylight hours (e.g. 10 % or more), configuring this exclusion is important.
Without it, the model will systematically under-predict summer peak production.

The trained model must survive daemon restarts. Store the model with `joblib.dump`
alongside a metadata JSON:

```json
{
  "trained_at_utc": "2026-04-01T06:00:00Z",
  "data_rows": 4320,
  "distinct_months": 9,
  "validation_mae_kwh": 0.082,
  "hyperparams": {...}
}
```

At startup, if a model file exists and its metadata is valid, skip retraining
until the next scheduled training time. If the model file is absent or corrupt,
trigger an immediate training run before publishing any forecast.

### 8. Model persistence and daemon restart safety

Use the same fixed-band approach as the existing pv_fetcher, but with bands
derived from the validation MAE rather than hard-coded constants. At inference
time, compute:

    relative_error = validation_mae_kwh / mean_actual_kwh_daylight

Then:

    0– 6 h ahead:  min(0.95, max(0.60, 1.0 - relative_error))
    6–24 h ahead:  above × 0.90
    24–48 h ahead: above × 0.80

Floor all confidence values at 0.30 regardless of model error.

If no model has been trained yet, publish confidence = 0.30 for all steps.

### 9. Confidence values

Use the same fixed-band approach

```
mimirheim_helpers/pv/
  forecast.solar/         (existing, unchanged)
  ml_learner/               (new)
    ml_learner/
      __init__.py
      __main__.py         # entry point / daemon class
      config.py           # Pydantic schemas
      storage.py          # SQLite schema + repository functions
      knmi_fetcher.py     # KNMI observations client (via knmi-py)
      meteoserver_fetcher.py   # Meteoserver forecast API client
      ha_actuals.py       # HA SQLAlchemy reader
      dataset_builder.py  # merge, normalise, fill
      features.py         # build feature matrix from dataset
      trainer.py          # XGBoost + TimeSeriesSplit hyperparam search
      predictor.py        # inference: load model, generate kW steps
      publisher.py        # format + publish mimirheim MQTT payload
      example.yaml
    pyproject.toml
    tests/
      unit/
        test_config.py
        test_storage.py
        test_knmi_fetcher.py
        test_meteoserver_fetcher.py
        test_ha_actuals.py
        test_dataset_builder.py
        test_features.py
        test_trainer.py
        test_predictor.py
        test_publisher.py
      __init__.py
      conftest.py
    uv.lock
    AGENTS.md
```

The `AGENTS.md` inside `mimirheim_helpers/pv/pv_ml_learner/` must reference this plan
and declare the same boundary rules as the main AGENTS.md.

---

## Dependencies

```toml
[project]
name = "mimirheim-pv-learner"
requires-python = ">=3.12"
dependencies = [
    "pydantic>=2.7",
    "pahomqtt>=2.0",       # MQTT publish
    "pyyaml>=6.0",
    "httpx>=0.27",         # Meteoserver HTTP
    "knmi-py>=0.2",        # KNMI hourly observations
    "sqlalchemy>=2.0",     # HA database read
    "xgboost>=2.0",
    "scikit-learn>=1.4",   # TimeSeriesSplit, metrics
    "joblib>=1.3",         # model serialisation
    "pandas>=2.2",         # dataset manipulation
    "helper_common",       # shared MqttConfig, HelperDaemon
]

[tool.uv.dev-dependencies]
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "freezegun>=1.4",
```

**Do not add numpy directly** — xgboost and pandas both pull it transitively.
Do not add matplotlib or seaborn — visualisation is out of scope.

---

## Implementation sequence

Each step must follow strict TDD: write the test, confirm it fails, implement,
confirm it passes.

---

### Step 1 — Scaffold

**Files to create:**
- `mimirheim_helpers/pv/pv_ml_learner/pyproject.toml`
- `mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/__init__.py`
- `mimirheim_helpers/pv/pv_ml_learner/tests/__init__.py`
- `mimirheim_helpers/pv/pv_ml_learner/tests/unit/__init__.py`
- `mimirheim_helpers/pv/pv_ml_learner/tests/conftest.py` (empty)
- `mimirheim_helpers/pv/pv_ml_learner/AGENTS.md` (copy + adapt from parent)

**Acceptance criteria:**
- `uv run pytest` inside `mimirheim_helpers/pv/pv_ml_learner/` collects with no errors.
- Zero tests collected is acceptable at this stage.

---

### Step 2 — Config schema (`config.py`)

All config fields are UTC-aware where times are involved.

**Schema:**

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: pv-learner
  username: ~
  password: ~

# When true, publish an empty non-retained message to mimir_trigger_topic
# after every complete inference cycle (all arrays published).
signal_mimir: false
# mimir_trigger_topic: mimir/input/trigger

knmi:
  station_id: 260          # De Bilt = 260; use nearest station to your location
  # No API key required; knmi-py uses the public KNMI script endpoint.
  # KNMI history is backfilled automatically to cover the full HA actuals window.

meteoserver:
  api_key: "your-meteoserver-api-key"
  latitude: 52.10
  longitude: 5.18
  forecast_horizon_hours: 48

homeassistant:
  db_path: /config/home-assistant_v2.db

# One entry per independently metered PV array. Each array is trained,
# predicted, and published independently. At least one array is required.
arrays:
  - name: main                         # unique identifier; used in logs and DB
    peak_power_kwp: 5.2                # installed peak power — used to clamp predictions
    output_topic: mimir/input/pv_forecast/main
    # Entity IDs whose hourly production is summed. All must be energy (kWh)
    # sensors with state_class: total_increasing, recorded in long_term_statistics.
    sum_entity_ids:
      - sensor.solaredge_energy_today
    model_path: /data/pv_ml_learner_main.joblib
    metadata_path: /data/pv_ml_learner_main_meta.json
    # Optional: binary or numeric sensors that indicate when the inverter was
    # actively limiting production (export cap, grid protection, etc.).
    # Any training hour where any of these reads True or > 0 is excluded.
    # See Critical Concern 7 for the full rationale.
    # exclude_limiting_entity_ids:
    #   - binary_sensor.solaredge_export_limited

storage:
  db_path: /data/pv_ml_learner.db   # shared SQLite database (KNMI + Meteoserver + all PV actuals)

training:
  # MQTT topic that triggers a full training cycle: ingest new KNMI and HA data,
  # retrain all arrays, then immediately run an inference cycle.
  # Publish any message here from an external scheduler (HA automation, cron,
  # Node-RED, etc.) to trigger training. Recommended: once daily at 03:00 UTC.
  train_trigger_topic: mimir/input/tools/pv_ml_learner/train
  # MQTT topic that triggers an inference-only cycle: fetch a fresh Meteoserver
  # forecast, run all arrays, publish. Recommended: fire after each Meteoserver
  # model update (approx 04:05, 10:05, 16:05 UTC).
  inference_trigger_topic: mimir/input/tools/pv_ml_learner/infer
  min_months_required: 12  # refuse to train with fewer distinct calendar months
                           # (default = 12; lower temporarily while data accumulates)
  hyperparams:
    # Ranges for TimeSeriesSplit grid search. Remove to use defaults.
    n_estimators: [200, 500]
    max_depth: [4, 6]
    learning_rate: [0.05, 0.1]
    subsample: [0.8, 1.0]
    min_child_weight: [1, 5]
  n_cv_splits: 5

ha_discovery:
  enabled: false
  # discovery_prefix: homeassistant
  # device_name: MIMIRHEIM PV Learner
```

**Pydantic rules:** All models use `ConfigDict(extra="forbid")`.

**Tests to write first** (in `test_config.py`):

- Valid full config parses without error.
- `min_months_required` below 1 is rejected.
- Missing `meteoserver.api_key` is rejected.
- An array with empty `sum_entity_ids` is rejected.
- Duplicate array names in `arrays` list are rejected.
- `signal_mimir = true` without `mimir_trigger_topic` is rejected.
- `exclude_limiting_entity_ids` absent on an array parses as an empty list.

---

### Step 3 — Persistent storage (`storage.py`)

SQLite database managed via SQLAlchemy Core (not ORM — we want explicit SQL
control for time-series queries). Use a single `ml_learner.db` file.

**Schema:**

```sql
CREATE TABLE knmi_radiation (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    hour_utc    INTEGER NOT NULL UNIQUE,  -- Unix timestamp, truncated to hour
    station_id  INTEGER NOT NULL,
    ghi_wm2     REAL NOT NULL,            -- W/m², converted from J/cm²/h (Q col)
    wind_ms     REAL,                     -- m/s (FH col ÷ 10); NULL if missing
    temp_c      REAL,                     -- °C (T col ÷ 10); NULL if missing
    rain_mm     REAL NOT NULL             -- mm (RH col ÷ 10; -1 trace → 0.0)
);

CREATE TABLE meteoserver_forecast (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_ts    INTEGER NOT NULL,         -- Unix timestamp of the fetch
    step_ts     INTEGER NOT NULL,         -- Unix timestamp of this forecast step (UTC)
    ghi_wm2     REAL NOT NULL,            -- W/m² (Meteoserver `gr`, already W/m²)
    temp_c      REAL NOT NULL,            -- °C (`temp`)
    wind_ms     REAL NOT NULL,            -- m/s (`winds`)
    rain_mm     REAL NOT NULL,            -- mm (`neersl`)
    cloud_pct   REAL NOT NULL             -- % 0–100 (`tw` total cloud cover)
);
CREATE INDEX ms_fetch ON meteoserver_forecast(fetch_ts);
CREATE INDEX ms_step  ON meteoserver_forecast(step_ts);

CREATE TABLE pv_actuals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    array_name TEXT    NOT NULL,          -- matches ArrayConfig.name
    hour_utc   INTEGER NOT NULL,          -- Unix timestamp, truncated to hour
    kwh        REAL    NOT NULL,
    UNIQUE (array_name, hour_utc)
);
```

**Repository functions** (all accept a SQLAlchemy `Connection`, never manage
their own transactions):

```python
def upsert_knmi_hours(conn, rows: list[KnmiRow]) -> int: ...
def get_knmi_range(conn, start_ts: int, end_ts: int) -> list[KnmiRow]: ...
def get_latest_knmi_ts(conn) -> int | None: ...

def insert_meteoserver_fetch(conn, fetch_ts: int, rows: list[McRow]) -> None: ...
def get_latest_meteoserver_fetch(conn) -> list[McRow] | None: ...
def prune_meteoserver(conn, keep_fetches: int = 10) -> None: ...

def upsert_pv_actuals(conn, rows: list[PvActualRow]) -> int: ...
def get_pv_actuals_range(conn, array_name: str, start_ts: int, end_ts: int) -> list[PvActualRow]: ...
def get_latest_actuals_ts(conn, array_name: str) -> int | None: ...
def count_distinct_months(conn, array_name: str) -> int: ...
```

`KnmiRow`, `McRow`, `PvActualRow` are lightweight dataclasses (not Pydantic — no
need for validation at the DB layer).

**Tests to write first** (in `test_storage.py`):

- `upsert_knmi_hours` inserts and can be called again without raising (upsert,
  not insert-only).
- `get_knmi_range` returns only rows within the requested window.
- `get_latest_knmi_ts` returns None on empty table.
- `upsert_pv_actuals` is idempotent on the same `hour_utc`.
- `count_distinct_months` returns correct count from a controlled fixture.
- `prune_meteoserver` removes old fetches, keeps the `keep_fetches` most recent.
- All tests use an in-memory SQLite connection (`sqlite:///:memory:`), never a file.

---

### Step 4 — KNMI fetcher (`knmi_fetcher.py`)

**Do:** Fetch hourly global radiation from KNMI via the `knmi-py` library for a
configurable station. On first run, backfill from the earliest HA actuals hour to
now. On subsequent runs, fetch only from `get_latest_knmi_ts()` to now.

KNMI data is never bounded by a user-configured lookback: the window is always
`[earliest_ha_actual, now]`. This guarantees KNMI coverage for every HA row.

**Library call:**

```python
import knmi

hourly_df = knmi.get_hour_data_dataframe(
    stations=[config.knmi.station_id],
    start=start_str,   # "YYYYMMDDHH"
    end=end_str,       # "YYYYMMDDHH"
    variables=["Q", "FH", "T", "RH"],
)
```

Conversions (see Critical Concern 2 for the full table):

    ghi_wm2  = Q  * 10_000 / 3_600   # J/cm²/h → W/m²
    wind_ms  = FH * 0.1               # 0.1 m/s → m/s
    temp_c   = T  * 0.1               # 0.1 °C → °C
    rain_mm  = RH * 0.1               # 0.1 mm → mm  (RH == -1 → 0.0)

KNMI hour numbering runs 1–24 (hour 24 = 00:00 of the following calendar day).
Test this boundary case explicitly with knmi-py — the library may or may not
normalise it. Adjust the `hour_utc` timestamp accordingly if it does not.

**Missing value handling:**
- `Q == -1`: genuine missing radiation — **drop the entire row**. A row without
  irradiance is useless for training and cannot be used for night detection.
  Log the count of dropped rows at INFO level each ingest cycle.
- `FH == -9999` or `T == -9999`: station did not report that variable for this
  hour — store as `None`. The dataset builder handles nullable columns.
- `RH == -1`: trace precipitation (< 0.05 mm) — store as `0.0`, which is
  physically correct (negligible rain, not missing data).

**Error handling:**
- If knmi-py raises a network or HTTP error, raise `FetchError` (retriable by
  caller). Do not retry inside this module.
- If the station ID is not recognised, raise `ConfigurationError`.

**Tests to write first** (in `test_knmi_fetcher.py`):

knmi-py makes real HTTP calls, so the tests must mock at the HTTP level using
`respx` or `unittest.mock.patch`. Supply a synthetic DataFrame that knmi-py
would return (bypassing its HTTP layer entirely is simpler — mock
`knmi.get_hour_data_dataframe` at the import boundary).

- Successful fetch: correct number of `KnmiRow` objects returned; all four
  variables converted with the correct multipliers.
- `Q = -1` rows are dropped entirely; count is logged.
- `FH = -9999` produces `wind_ms = None` (not 0.0 and not an error).
- `RH = -1` (trace precipitation) produces `rain_mm = 0.0`.
- KNMI hour 24 is converted to 00:00 of the following day.
- Network error raises `FetchError`.
- Unrecognised station raises `ConfigurationError`.

Do not call the real KNMI API in tests.

---

### Step 5 — HA actuals ingester (`ha_actuals.py`)

**Do:** Read hourly PV energy from HA's `long_term_statistics` table, sum the
configured sensor entities per hour, and store via `upsert_pv_actuals`. Only fetch
rows more recent than `get_latest_actuals_ts()`.

**Query pattern:**

```sql
SELECT lts.start_ts, SUM(lts.sum) AS total_kwh
FROM long_term_statistics lts
JOIN long_term_statistics_meta ltm ON lts.metadata_id = ltm.id
WHERE ltm.statistic_id IN :entity_ids
  AND lts.start_ts > :latest_known_ts
GROUP BY lts.start_ts
ORDER BY lts.start_ts
```

`sum` in `long_term_statistics` is a cumulative total. To get per-hour production,
compute the delta between consecutive rows **per entity** before summing:

```
entity_A_hour_kwh = entity_A_this_hour.sum - entity_A_prev_hour.sum
```

Then sum across entities per hour. This is more complex than a simple `SUM(sum)`.
Read the query carefully and unit-test it against a controlled dataset.

**Safety rules:**
- Open SQLite with `create_engine("sqlite:///...", connect_args={"check_same_thread": False})`
  and `execution_options(sqlite_readonly=True)` where supported, or use
  `URI=true&mode=ro` in the connection string.
- Never write to the HA database.
- If the HA database is locked (SQLite `SQLITE_BUSY`), log and skip — do not retry
  in a tight loop.

**Tests to write first** (in `test_ha_actuals.py`):

- Delta computation: given `sum` values [10, 12, 15, 14], the per-hour deltas
  are [2, 3, -1 → clamp to 0].
- Two sensors at the same hour are summed correctly.
- A gap in HA data (missing hour) produces no row for that hour (not a zero).
- The read-only connection string is constructed correctly.

Use an in-memory SQLite database pre-populated to match the HA schema. Do not
connect to a real HA database in tests.

---

### Step 6 — Meteoserver fetcher (`meteoserver_fetcher.py`)

**Do:** Fetch an hourly weather forecast from the Meteoserver API (see Critical
Concern 1 for the full API specification). Store via `insert_meteoserver_fetch`.
Extract `ghi_wm2`, `temp_c`, `wind_ms`, `rain_mm`, and `cloud_pct` from each
step. Always store with the current `fetch_ts` so the most recent forecast can
be retrieved by `get_latest_meteoserver_fetch`.

**Request:**

```python
response = httpx.get(
    "https://data.meteoserver.nl/api/uurverwachting.php",
    params={"lat": config.meteoserver.latitude,
            "long": config.meteoserver.longitude,
            "key": config.meteoserver.api_key},
    timeout=10.0,
)
```

**Parsing each step** (`step` is one element of `response.json()["data"]`):

```python
McRow(
    step_ts  = int(step["tijd"]),          # UTC Unix timestamp
    ghi_wm2  = float(step["gr"]),          # W/m² already, no conversion
    temp_c   = float(step["temp"]),        # °C
    wind_ms  = float(step["winds"]),       # m/s
    rain_mm  = float(step["neersl"]),      # mm precipitation
    cloud_pct= float(step["tw"]),          # % total cloud cover
)
```

Do not store `gr_w` — its exact meaning is unclear and `gr` in W/m² is the
correct irradiance field.

Truncate the stored steps to `config.meteoserver.forecast_horizon_hours` steps
(default 48). The API returns up to ~54 hours; discard the tail beyond the
configured horizon.

**Error handling:**
- HTTP 401/403: raise `ConfigurationError` (bad or missing API key — not
  retriable).
- HTTP 429: raise `RatelimitError`.
- HTTP 5xx or network error: raise `FetchError` (retriable by caller).
- Malformed JSON or missing `data` key: raise `FetchError`.

**Tests to write first** (in `test_meteoserver_fetcher.py`, using `respx` or
`httpx.MockTransport`):

- Successful response: correct number of `McRow` objects returned (capped at
  `forecast_horizon_hours`); `ghi_wm2`, `temp_c`, `wind_ms`, `rain_mm`,
  `cloud_pct` parsed correctly from the documented field names.
- `step_ts` is the integer value of the `tijd` field (UTC Unix timestamp).
- HTTP 401 raises `ConfigurationError`.
- HTTP 429 raises `RatelimitError`.
- HTTP 500 raises `FetchError`.
- Malformed JSON raises `FetchError`.

Use a fixture that reproduces the real response structure documented in Critical
Concern 1. No `pytest.mark.skip` markers — the API is fully documented.

---

### Step 7 — Dataset builder (`dataset_builder.py`)

**Do:** Join KNMI, Meteoserver, and PV actuals into a unified training dataset.

```python
@dataclass
class TrainingRow:
    hour_utc: int
    ghi_wm2: float         # from KNMI Q
    wind_ms: float | None  # from KNMI FH; None if station does not report it
    temp_c: float | None   # from KNMI T; None if station does not report it
    rain_mm: float         # from KNMI RH (0.0 for trace amounts)
    hour_of_day: int       # 0–23
    month: int             # 1–12
    week_nr: int           # ISO week number 1–53
    quarter: int           # 1–4
    kwh_actual: float      # from pv_actuals
```

**Join rules:**
- Only hours where both KNMI radiation **and** pv_actuals exist are included.
- Hours where KNMI Q ≤ 5 W/m² are excluded (night — see Critical Concern 5).
- Hours where pv_actuals is negative (clipped cumulative rollover) are excluded.
- Hours where any `exclude_limiting_entity_ids` sensor reads `True` or `> 0`
  are excluded (inverter throttling — see Critical Concern 7). Hours with no
  recorded value for those sensors are included normally.
- Meteoserver data is not used in training rows; only KNMI is used as the weather
  source at training time.
- `wind_ms` and `temp_c` are `None` where the KNMI station did not report them
  for that hour. Do not impute with zeros — this would corrupt the model; pass
  `None` through and let `features.py` handle availability.
- `rain_mm` is never `None`; the KNMI `-1` trace case is already resolved to
  `0.0` in the fetcher.

Most full-measurement stations (including De Bilt, 260) report all four variables.
Check station capabilities at first run and log clearly if wind or temperature
is consistently `None` across the first 100 rows.

**Tests to write first** (in `test_dataset_builder.py`):

- Build from controlled fixture: correct row count (night hours excluded).
- Row with negative actual is excluded.
- Row where a limiting sensor reads `True` is excluded.
- Row where the limiting sensor has no recorded value is included.
- `count_distinct_months` after building reflects correct distribution.
- Missing KNMI temperature produces `temp_c = None`, not `temp_c = 0.0`.
- `rain_mm = 0.0` for rows where KNMI `RH` was `-1` (trace).

---

### Step 8 — Feature engineering (`features.py`)

Two public functions:

```python
def build_training_matrix(rows: list[TrainingRow]) -> tuple[pd.DataFrame, pd.Series]:
    """Return (X, y) for XGBoost training."""

def build_inference_row(step_ts: datetime, mc_row: McRow) -> pd.DataFrame:
    """Return a single-row feature DataFrame for one forecast step."""
```

**Feature columns (must be identical in both functions):**

| Column        | Source (train)  | Source (inference)     | Always present? |
|---------------|-----------------|------------------------|-----------------|
| `ghi_wm2`     | KNMI Q          | Meteoserver `gr`       | Yes |
| `wind_ms`     | KNMI FH         | Meteoserver `winds`    | If station reports it |
| `temp_c`      | KNMI T          | Meteoserver `temp`     | If station reports it |
| `rain_mm`     | KNMI RH         | Meteoserver `neersl`   | Yes |
| `hour`        | `datetime.hour` | `step_ts.hour`         | Yes |
| `month`       | `datetime.month`| `step_ts.month`        | Yes |
| `week_nr`     | ISO week        | ISO week               | Yes |
| `quarter`     | `(month-1)//3+1`| same                   | Yes |

`rain_mm` is always present because KNMI `-1` (trace) is resolved to `0.0` in
the fetcher, and Meteoserver always provides `neersl`.

If `wind_ms` or `temp_c` are unavailable (station consistently reports `None`),
exclude those columns from **both** training and inference. The feature list is
determined once at training time and serialised in the model metadata. Inference
must use the exact same feature list; `build_inference_row` receives the saved
feature list and must raise `ValueError` if any required column is absent from
the Meteoserver row.

**Tests to write first** (in `test_features.py`):

- `build_training_matrix` returns the expected column names including `rain_mm`.
- Column names are identical between `build_training_matrix` and `build_inference_row`.
- `quarter` is computed correctly for month 1 (→ 1), 4 (→ 2), 7 (→ 3), 10 (→ 4).
- `week_nr` handles the year boundary (ISO week 52/53 → 1).
- When `wind_ms` and `temp_c` are `None` on all training rows, those columns are
  absent from the training matrix and from the inference row.

---

### Step 9 — Model trainer (`trainer.py`)

**Do:** Accept a `list[TrainingRow]`, perform grid-search hyperparameter tuning
using `TimeSeriesSplit(n_splits=config.training.n_cv_splits)`, train a final
XGBoost `XGBRegressor` on all data with the best hyperparameters, and persist
via `joblib.dump`.

**Objective:** `reg:squarederror`. Target: `kwh_actual` per hour.

**Grid search:**
- Use `sklearn.model_selection.GridSearchCV` with `scoring="neg_mean_absolute_error"`.
- The grid is provided by `config.training.hyperparams`. If not configured, use
  a minimal default grid (n_estimators=[200], max_depth=[5], learning_rate=[0.08],
  subsample=[0.9], min_child_weight=[1]).
- Log the best parameters and best CV MAE at INFO level.

**Minimum data check** (critical concern 4):
- Before fitting, call `count_distinct_months`. If below `config.training.min_months_required`,
  raise `InsufficientDataError` (a custom exception). The caller must not publish
  a forecast if this is raised.

**Output:**
- `joblib.dump(best_estimator, model_path)` — path passed explicitly.
- Write metadata JSON to `metadata_path` — path passed explicitly.

**Tests to write first** (in `test_trainer.py`):

- `InsufficientDataError` is raised when distinct months < threshold.
- After a successful training run on synthetic data, model file exists.
- Metadata JSON contains `trained_at_utc`, `validation_mae_kwh`, and
  `distinct_months`.
- A second training run overwrites the model file without raising.
- The feature list in metadata matches the columns in `build_training_matrix`.

All tests use synthetic in-memory data; XGBoost's fit is allowed to run (it is
fast enough on 500 rows).

---

### Step 10 — Predictor (`predictor.py`)

**Do:** Load the trained model, fetch the latest Meteoserver forecast from the
database, build inference rows for each step, predict, apply the clamp, and
return a list of `ForecastStep` objects.

```python
@dataclass
class ForecastStep:
    ts: datetime           # UTC-aware
    kw: float              # predicted AC output (non-negative)
    confidence: float      # from confidence formula in critical concern 9
```

**Rules:**
- Any step with Meteoserver GHI ≤ 5 W/m² → `kw = 0.0`, `confidence` from
  confidence formula anyway (same formula; user can see these are near-zero).
- Clamp all predictions to `[0.0, peak_power_kwp * 1.1]`.
- If no trained model exists, raise `ModelNotReadyError`. The daemon catches this
  and logs without publishing.

**Tests to write first** (in `test_predictor.py`):

- Night steps (GHI ≤ 5) return `kw = 0.0`.
- Predictions are clamped above zero and below `peak_power_kwp * 1.1`.
- `ModelNotReadyError` is raised when model file is absent.
- Confidence values are within `[0.30, 0.95]`.
- Output step count matches the number of Meteoserver forecast rows.

---

### Step 11 — Publisher (`publisher.py`)

Identical structure to the existing `pv_fetcher/publisher.py`. Converts
`list[ForecastStep]` to the mimirheim MQTT payload format and publishes retained.

```python
def publish_forecast(
    client: mqtt.Client,
    output_topic: str,
    steps: list[ForecastStep],
    *,
    signal_mimir: bool,
    mimir_trigger_topic: str | None = None,
) -> None: ...
```

Output JSON format (must match mimirheim's `PowerForecastStep` schema):

```json
[
  {"ts": "2026-04-02T10:00:00+00:00", "kw": 1.23, "confidence": 0.87},
  ...
]
```

**Tests to write first** (in `test_publisher.py`):

- Output JSON is valid and contains `ts`, `kw`, `confidence` keys only.
- `ts` strings are UTC ISO 8601 with timezone offset.
- `signal_mimir = True` without `mimir_trigger_topic` raises `ValueError` before
  any MQTT call.
- Payload is published retained with QoS 1.

---

### Step 12 — Main daemon (`__main__.py`)

The daemon has two **independent** schedules, each with a distinct responsibility.
They are driven by `apscheduler >= 3.10, < 4.0` (same library as the mimirheim
scheduler).

**Training cycle** (triggered by message on `config.training.train_trigger_topic`):

1. Fetch KNMI observations from last known timestamp up to `now - 48h`
   (respecting the KNMI publication delay; see Critical Concern 2).
   This step is shared across all arrays.
2. For each array in `config.arrays`:
   a. Read HA data since the last stored actuals timestamp for that array.
   b. Build the training dataset (KNMI + per-array PV actuals).
   c. If `count_distinct_months >= min_months_required`: train model, write
      metadata to `array.model_path` and `array.metadata_path`.
      Otherwise: log which months are missing and skip this array.
3. After processing all arrays, immediately run the inference cycle to publish
   fresh forecasts using the newly trained (or unchanged) models.

The training cycle does **not** fetch Meteoserver. Weather forecast data is
not needed for training.

**Inference cycle** (triggered by message on `config.training.inference_trigger_topic`):

1. Fetch a fresh Meteoserver forecast (shared across all arrays).
2. For each array in `config.arrays`:
   a. Run predictor against the Meteoserver rows using the array's model.
   b. Publish the forecast payload to `array.output_topic`.
   c. If `ModelNotReadyError`: log and skip this array; do not crash.
3. If `config.signal_mimir` is True and all arrays have published successfully,
   publish an empty message to `config.mimir_trigger_topic`.

Scheduling (cron timing, HA automation, Node-RED, etc.) is the responsibility
of the caller. The daemon itself provides no internal scheduler.

**Startup sequence:**
1. Initialise SQLite database (create tables if not exist).
2. Check if any array model file is missing.
3. If any array lacks a model: attempt a training run immediately (subject to
   minimum data check). Log clearly if insufficient data.
4. Start MQTT client, subscribe to both trigger topics, and block.

**Tests to write first** (integration, in `tests/integration/test_daemon.py`):

- `run_training_cycle` writes model files for all configured arrays.
- `run_training_cycle` does not raise when data is insufficient; model file
  is absent after the call.
- `run_training_cycle` with two arrays writes two separate model files.
- `run_inference_cycle` calls `publish_forecast` once per array.
- `run_inference_cycle` skips one array when its model is missing; other
  arrays still publish.
- A Meteoserver fetch failure causes `run_inference_cycle` to return without
  publishing anything.

---

## Acceptance criteria (full tool)

- All unit tests pass: `uv run pytest tests/unit/ -q`.
- Integration tests pass against a mock MQTT broker.
- `uv run python -m pv_ml_learner --config dev_config.yaml` starts, ingests KNMI
  and HA data, trains a model (if data is sufficient), and publishes a 48-step
  MQTT payload in mimirheim format.
- The published payload is accepted by mimirheim without config changes (verify by
  inspecting the `pv_forecast` in the next solve dump).
- With at least 6 months of HA solar data, the model's validation MAE in the
  metadata file is less than 0.3 kWh/h (a sanity check; not a hard requirement).

---

## What this plan explicitly does not cover

- Any GUI, web dashboard, or visualisation of training data.
- Automatic station-proximity lookup (the KNMI station ID is user-configured).
- Support for non-Netherlands locations (KNMI and Meteoserver are NL-specific).
- Retraining when HA data is corrected or KNMI data is revised.
- Ensemble models or model comparison across training runs.
- Migration of data from forecast.solar to this tool.

These are future concerns. Do not implement them during this plan.
