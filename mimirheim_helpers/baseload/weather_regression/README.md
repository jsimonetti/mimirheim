# baseload_weather: weather-dependent base load forecast

**baseload_weather** forecasts the house load for a mimirheim `static_loads` entry from Home Assistant recorder history and an Open-Meteo temperature forecast. It is meant for houses where electric heating, such as a heat pump, makes consumption depend on the weather, so that a same-hour-of-day average misses cold and mild spells.

Use it instead of, or next to, `baseload_ha_db` when:

- the house heats electrically and the load rises noticeably on cold days, and
- an outdoor temperature sensor with long-term statistics exists in Home Assistant.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)
7. [Scheduling](#7-scheduling)
8. [Prerequisites](#8-prerequisites)

---

## 1. Purpose

mimirheim needs a per-step forecast of the household's uncontrollable consumption for each `static_loads` entry. For a house with electric heating, most of the variation in that consumption from day to day is heating, and heating follows the weather. This helper models that relation explicitly and applies it to the temperature forecast, instead of assuming tomorrow looks like the average of the last days.

### Next to baseload_ha_db

A common setup uses this helper only for the load that depends on the weather, the heat pump, and `baseload_ha_db` for the rest of the house. The model then trains on the heat pump's own energy counter alone, without the weather-independent household load, and a heat pump meter often has a longer history than the whole-house balance.

- Configure two `static_loads` entries in mimirheim, for example `base_load` and `heating`.
- In this helper, set `mimir_static_load_name: heating`, list only the heat pump's energy counter in `sum_entities`, and set `subtract_entities: []`. The example configuration subtracts grid export and EV charging, which must not come off the heat pump's consumption.
- In `baseload_ha_db`, keep the house balance as before and **add the same heat pump counter to its `subtract_entities`**. Otherwise the heat pump is counted in both forecasts. Nothing checks for that.

This needs a mimirheim version that gives each static load its own forecast; in older versions every static load receives the sum of all load forecasts, so two static loads count the household load twice.

---

## 2. How it works

### Model

For every **cell**, a combination of local hour of day (0-23) and day type (weekday or weekend), the hourly load in kWh is modelled as

```
kWh = a + b * max(0, T_base - T_s)
```

- `T_s` is the outdoor temperature smoothed by an exponential moving average with time constant `smoothing_hours`. A building responds to the weather of the last hours, not of this hour; the smoothing stands in for its thermal mass and that of its heat emitters.
- `T_base` is the balance temperature above which no heating is needed. It is chosen from a 0.5-degree grid between `t_base_min_c` and `t_base_max_c`.
- `a` is the weather-independent load of the cell: cooking, lighting, standby, hot water.
- `b >= 0` is the heating slope of the cell in kWh per hour per degree below `T_base`.

Hour of day and day type are evaluated in the configured `timezone`, so morning and evening routines stay aligned across daylight saving changes.

### Fitting

Each run refits the model on the last `lookback_days` days; nothing is stored between runs. Days are weighted by recency with `lookback_decay`: the newest day weighs `lookback_decay` times the oldest, with days in between interpolated exponentially.

Every cell is fitted at the most specific level its data supports:

| Level | Fit | Used when |
|---|---|---|
| 1 | Own cell | Enough effective samples and enough temperature variation within the cell |
| 2 | The hour, pooled over weekday and weekend | The cell alone falls short |
| 3 | One slope shared by all cells, with an intercept per cell | Pooling per hour still falls short |
| 4 | The weighted mean, no slope | No balance-temperature candidate meets the level-3 eligibility requirements |

"Enough" means an effective sample size (Kish) of at least `min_samples` and a weighted variance of the heating-degree term of at least `min_hdd_variance`. The shared slope at level 3 is estimated within cells, so differences between hours of the day cannot pass for a heating effect. A slope that comes out negative is set to zero, because less load in colder weather is not heating.

`T_base` is chosen first, as the grid candidate with the lowest weighted error of the level-3 fit; candidates that fail the level-3 eligibility test over all training hours (too few effective samples, or too little within-cell variation) are skipped, and near-equal errors go to the lowest candidate. When no candidate qualifies, there is no slope at all and every cell uses level 4.

Where a cell has no training hours of its own, its level-3 intercept and its level-4 mean fall back to the cell's hour pooled over day types, and then to all training hours.

### Training data

- **Load**: the hourly energy of every `sum_entities` counter minus every `subtract_entities` counter. An hour is used only when every listed counter has a valid reading for it. A missing reading is never counted as zero, because a missing PV or export reading would otherwise turn into artificial consumption.
- **Counter readings**: hourly deltas of the recorder's cumulative `sum`. A delta is trusted only between two rows exactly one hour apart and when it is not negative. Deltas above `P99 * outlier_factor` per entity are dropped. With fewer than 24 deltas the filter is skipped, because the percentile would be unreliable; when P99 is zero (a counter that is idle almost always), the P99 of the non-zero deltas is used instead.
- **Temperature**: the recorder's hourly mean, converted to degrees Celsius. Gaps of up to six hours are interpolated linearly. After a longer gap the moving average restarts, and the next `ceil(3 * smoothing_hours)` hours are not used for training while it settles.
- **exclude_dates**: drop the load readings of those local dates; their temperatures stay in use.

### Forecast

The Open-Meteo forecast covers from 24 hours before the current hour to the end of the horizon, so the moving average runs without a break from history into the forecast; recorder values win where both exist. Each forecast hour gets `max(0, a + b * max(0, T_base - T_s))` from its cell.

---

## 3. Configuration

`mimirheim_helpers/examples/baseload-weather.yaml` is the annotated reference configuration and validates as shipped. The generated field reference is `wiki/Reference/Config-Baseload-Weather.md`.

Top-level fields:

| Field | Default | Meaning |
|---|---|---|
| `mqtt` | required | Broker settings. `client_id` defaults to `mimir-baseload-weather`. |
| `mimir_topic_prefix` | `mimir` | mimirheim's `mqtt.topic_prefix`, used to derive topics. |
| `mimir_static_load_name` | `base_load` | The `static_loads` entry this forecast is for. |
| `trigger_topic` | `{prefix}/input/tools/baseload/trigger` | Shared with the other baseload helpers. |
| `output_topic` | `{prefix}/input/baseload/{name}/forecast` | Retained forecast topic. |
| `horizon_hours` | `96` | Hours after the current hour, 1-168. |
| `signal_mimir` / `mimir_trigger_topic` | `false` / derived | Ask mimirheim to solve after publishing. |
| `stats_topic` | none | Per-cycle run statistics from the shared helper daemon. |
| `model_stats_topic` | none | Fit statistics after each successful cycle. |
| `ha_discovery` | none | Home Assistant MQTT discovery (a trigger button). |
| `homeassistant` | required | `db_url`, `sum_entities`, `subtract_entities`, `temperature_entity`. |
| `weather_forecast` | required | `latitude`, `longitude`, optional `base_url`, `timeout_s`. |
| `model` | required | `timezone` (required), `lookback_days` (56, 7-400), `lookback_decay` (1.0), `smoothing_hours` (12, 1-72), `t_base_min_c` / `t_base_max_c` (10 / 20), `min_samples` (10), `min_hdd_variance` (1.0), `exclude_dates`. |

Energy entities accept the units Wh, kWh and MWh only; the temperature entity accepts °C and °F.

Every listed energy entity must have a reading for an hour to be used, for the whole lookback window. This differs from `baseload_ha_db`, which counts a missing reading as zero:

- **Sensor renamed or replaced**: list only the current entity. Listing the old and the new one together leaves no hour where both have a reading. History from before the change is then not used for training.
- **Entity added later**: an hour needs two consecutive hourly rows of the entity for its delta, so history starts with the hour of its second statistics row; earlier hours are not used. Adding a newly installed device's counter (a battery, an EV charger) therefore restarts the training history from the moment its statistics begin, which keeps that device's past consumption out of the model.

---

## 4. Output format

Published retained, QoS 1, to `output_topic`:

```json
[
  {"ts": "2026-01-20T12:00:00+00:00", "kw": 1.82},
  {"ts": "2026-01-20T13:00:00+00:00", "kw": 1.64}
]
```

- One step per hour from the current hour through `horizon_hours` later, inclusive: `horizon_hours + 1` steps. mimirheim's forecast coverage ends at the last timestamp, so the final step marks the end of the horizon.
- `kw` is the average power over the hour (equal to the hour's kWh), never negative. mimirheim holds each value until the next step.
- No `confidence` field; mimirheim treats it as 1.0.

When `model_stats_topic` is set, a retained JSON object follows each successful forecast: `ts`, `t_base_c` (null when no slope could be fitted), `levels` (cells per fit level), `r2` (null for a constant load), `mae_kwh`, `training_hours`, `training_coverage_pct` and `forecast_points`. It is best effort: if it cannot be published, the cycle still counts as successful.

---

## 5. Running

From the repository root:

```bash
uv sync --all-extras
uv run python -m baseload_weather --config config.yaml
```

For PostgreSQL or MariaDB recorders, install the matching driver extra:

```bash
uv pip install "mimirheim[baseload-weather-postgres]"
uv pip install "mimirheim[baseload-weather-mysql]"
```

In the container the service is `baseload-weather`, enabled with `ENABLE_BASELOAD_WEATHER=true` and configured by `/config/baseload-weather.yaml`.

---

## 6. Fault tolerance

A failure before the forecast is published leaves the retained forecast unchanged and is recorded as a failed cycle:

- **Recorder**: unreachable database, SQL error, an entity without long-term statistics, or an unsupported unit. Errors never contain database credentials.
- **Weather forecast**: on HTTP 502, 503, 504 and network errors the request is attempted up to four times, with exponential back-off and jitter between attempts. HTTP 429 suppresses triggers for 15 minutes. A response that does not cover every hour of the horizon, or holds a non-finite value, fails the cycle. Nothing is cached.
- **Model**: no usable training hour at all.

Short history is not a failure: cells fall back to coarser fit levels, down to the plain mean.

---

## 7. Scheduling

The helper listens on the trigger shared by all baseload helpers, so an existing baseload scheduler entry triggers it. Because the weather forecast changes during the day, triggering every few hours keeps the forecast current, for example:

```yaml
schedules:
  - "5 */3 * * *": mimir/input/tools/baseload/trigger
```

---

## 8. Prerequisites

- Home Assistant 2023.3 or later (the `statistics.start_ts` schema).
- Long-term statistics for every listed entity: energy counters with `state_class: total` or `total_increasing`, and the outdoor temperature sensor with `state_class: measurement`. A sensor without a state class has no long-term statistics; add one through `customize` if needed. Statistics start when the state class is set; earlier history is not back-filled.
- Read access to the recorder database. SQLite files are always opened read-only, which also prevents a missing file from being created. For PostgreSQL or MariaDB, `SELECT` on `statistics` and `statistics_meta` is enough.
- Network access to the Open-Meteo API, or a self-hosted instance via `weather_forecast.base_url`.
