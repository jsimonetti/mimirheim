# epexpredictor_prices — EPEX day-ahead price prediction fetcher

**epexpredictor_prices** is a standalone daemon that fetches EPEX day-ahead spot price predictions from the [EpexPredictor API](https://epexpredictor.batzill.com) and publishes them to the mimirheim prices input topic, in the same format as the Nordpool helper.

Unlike Nordpool, EpexPredictor is explicitly a modeled forecast, not a guaranteed source: the operator's own documentation states "There are no guarantees on availability or correctness of the data." Its response includes a `knownUntil` timestamp marking the boundary between real EPEX auction results (relayed from energy-charts.info / ENTSO-E) and the model's own predicted tail, which currently extends about a week ahead.

This helper is meant to **augment** a true day-ahead source (Nordpool, a supplier feed), extending the mimirheim price horizon further into the future, at progressively lower confidence, using mimirheim's multi-source price merge (`inputs.prices` as a list of topics, merged per step by highest confidence).

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [Output format](#4-output-format)
5. [Running](#5-running)
6. [Fault tolerance](#6-fault-tolerance)

---

## 1. Purpose

mimirheim's `inputs.prices` accepts a list of topics, merged per step by whichever source reports the highest confidence for that step. epexpredictor_prices fills one such topic by:

1. Waiting for a message on its trigger topic.
2. Fetching the current EPEX day-ahead prediction from the EpexPredictor API via HTTP.
3. Applying the `import_formula` and `export_formula` to convert raw predicted prices to all-in prices.
4. Assigning a confidence value to every step based on how far ahead of the fetch it lies.
5. Publishing the payload — retained — to the configured output topic.
6. Optionally publishing to mimirheim's trigger topic so that mimirheim runs a new solve immediately.

**Run this helper alongside a true day-ahead source, not instead of one.** Its default `output_topic` is the same canonical `{prefix}/input/prices` topic Nordpool defaults to. If you run both helpers together, set one of the two `output_topic` values explicitly so they land on different entries of the `inputs.prices` list — this is exactly the scenario mimirheim's multi-source price merge was built to support.

---

## 2. How it works

### Trigger model

epexpredictor_prices runs as a persistent daemon and subscribes to a single MQTT trigger topic. It does not poll on a timer internally. Pair it with the scheduler tool or an external cron job.

### Confidence decay is anchored to fetch time

Every step's confidence is a function of how many hours ahead of the fetch it lies, exactly like the open-meteo PV helper's confidence bands:

| Horizon | Default confidence |
|---|---|
| 0-6 h ahead | 0.90 |
| 6-24 h ahead | 0.75 |
| 24-48 h ahead | 0.55 |
| 48+ h ahead | 0.35 |

This applies regardless of whether a step falls within the API's `knownUntil` "known" (real) window or its predicted tail. One consequence: within `knownUntil`, EpexPredictor is relaying real auction data, so a step there otherwise gets the same confidence penalty as a step in the predicted tail at the same horizon distance, even though it is factually as reliable as Nordpool's own output at that step.

### `known_until_confidence`: treating the real portion as confirmed

`confidence_decay.known_until_confidence` is an independent knob that addresses this without abandoning the fetch-time anchor. When set, it overrides the decay-band result for any step **at or before** the API's `knownUntil` timestamp with a single fixed confidence value. Steps after `knownUntil` are never affected and keep using the bands above.

Left at its default of `null` (the default), behaviour is exactly the fetch-time-only design described above: the bands apply uniformly to every step regardless of `knownUntil`.

Set it to `1.0` to match Nordpool's treatment of confirmed day-ahead prices, so this helper's real steps compete on equal footing with Nordpool in the multi-source merge instead of being structurally outranked on every overlapping step.

### Price interval

Same behaviour and rationale as the Nordpool helper: `epexpredictor.price_interval` is `quarter_hourly` (default, matches EPEX's 15-minute market time unit since October 2025) or `hourly` (averages the API's own hourly aggregation via the `hourly=true` request parameter).

### Area codes

EpexPredictor supports a fixed set of bidding zones: `DE`, `AT`, `BE`, `NL`, `SE1`, `SE2`, `SE3`, `SE4`, `DK1`, `DK2`, `ES`, `PT`. An unsupported area is rejected at config load time.

---

## 3. Configuration

Create a `config.yaml` alongside the tool (or pass any path with `--config`):

```yaml
mqtt:
  host: localhost
  port: 1883
  client_id: epexpredictor-prices

trigger_topic: mimir/input/tools/epexpredictor/trigger
output_topic: mimir/input/prices/epexpredictor   # must differ from Nordpool's if both run

epexpredictor:
  area: NL
  horizon_hours: 72
  import_formula: "price"
  export_formula: "price"
  price_interval: quarter_hourly

confidence_decay:
  hours_0_to_6: 0.90
  hours_6_to_24: 0.75
  hours_24_to_48: 0.55
  hours_48_plus: 0.35
  known_until_confidence: null   # set to 1.0 to trust the real portion like Nordpool

signal_mimir: false
mimir_trigger_topic: mimir/input/trigger   # required only when signal_mimir: true
```

All fields are required unless a default is shown. The tool rejects unknown fields.

### Field reference

| Field | Type | Description |
|-------|------|-------------|
| `mqtt.host` | string | MQTT broker hostname or IP address |
| `mqtt.port` | integer | MQTT broker port. Default: `1883` |
| `mqtt.client_id` | string | MQTT client identifier. Defaults to `mimir-epexpredictor` |
| `trigger_topic` | string | MQTT topic that triggers a fetch-and-publish cycle |
| `output_topic` | string | MQTT topic to publish the price payload to (retained). Defaults to `{mimir_topic_prefix}/input/prices` — **set explicitly when running alongside another price source** |
| `epexpredictor.area` | enum | EPEX bidding zone: `DE`, `AT`, `BE`, `NL`, `SE1`, `SE2`, `SE3`, `SE4`, `DK1`, `DK2`, `ES`, `PT` |
| `epexpredictor.base_url` | string | API base URL. Default: `https://epexpredictor.batzill.com`. Override for a self-hosted instance |
| `epexpredictor.horizon_hours` | integer | Hours ahead to request. Default `72` |
| `epexpredictor.import_formula` | string | Python expression for the all-in import price in EUR/kWh. Variables: `price` (predicted spot, EUR/kWh), `ts` (UTC-aware `datetime`). Default `"price"` |
| `epexpredictor.export_formula` | string | Python expression for the net export price in EUR/kWh. Same variables. Default `"price"` |
| `epexpredictor.price_interval` | `quarter_hourly` or `hourly` | Length of one published price step. Default `quarter_hourly` |
| `confidence_decay.hours_0_to_6` | float | Confidence for steps 0-6 h ahead of fetch time. Default `0.90` |
| `confidence_decay.hours_6_to_24` | float | Confidence for steps 6-24 h ahead. Default `0.75` |
| `confidence_decay.hours_24_to_48` | float | Confidence for steps 24-48 h ahead. Default `0.55` |
| `confidence_decay.hours_48_plus` | float | Confidence for steps 48+ h ahead. Default `0.35` |
| `confidence_decay.known_until_confidence` | float or null | Overrides confidence for steps at or before `knownUntil`. Default `null` (no override) |
| `signal_mimir` | boolean | If `true`, publish an empty message to `mimir_trigger_topic` after publishing. Default `false` |
| `mimir_trigger_topic` | string | mimirheim's trigger topic. Required when `signal_mimir: true` |

The API never receives a `surcharge` or `taxPercent` value from this helper's configuration — those parameters are always sent as `0.0`. Markup and tax belong exclusively in `import_formula` / `export_formula`.

---

## 4. Output format

The tool publishes a JSON array retained to `output_topic`:

```json
[
  {
    "ts": "2026-09-10T09:00:00+00:00",
    "import_eur_per_kwh": 0.213450,
    "export_eur_per_kwh": 0.213450,
    "confidence": 0.90
  }
]
```

Identical shape to the Nordpool helper's output. `ts` is UTC ISO 8601 with a `+00:00` offset. `confidence` varies per step according to the configured decay bands (and the `known_until_confidence` override, if set) rather than always being `1.0`.

---

## 5. Running

```bash
# From the mimirheim repo root:
uv run python -m epexpredictor_prices --config mimirheim_helpers/prices/epexpredictor/config.yaml
```

The process logs to stdout and does not daemonise. Use a process supervisor (systemd, Docker, s6) to run it persistently.

---

## 6. Fault tolerance

- **HTTP failure**: If the EpexPredictor API returns an error or the request times out, the tool logs the error at `ERROR` level and does not publish. The existing retained payload on `output_topic` (if any) remains unchanged.
- **MQTT disconnect**: The tool reconnects automatically using paho-mqtt's built-in reconnect loop. Trigger messages that arrive during a disconnect are not replayed (the trigger topic is not retained).
- **Invalid response**: If the API response cannot be parsed (missing `prices` or `knownUntil`, or an unparsable timestamp), the cycle is aborted and the error is logged. No partial payload is published.
