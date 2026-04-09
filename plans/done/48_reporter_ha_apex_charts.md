# Plan 48 — Reporter MQTT topics for HA ApexCharts visualisation

## Motivation

The `mimirheim-reporter` daemon already has all solve-cycle data in memory when it
processes a dump-available notification: prices, energy flows, SOC curves, and
summary statistics. Currently it writes this to an HTML file and stops. Home
Assistant operators who want to embed the latest schedule charts in an HA
dashboard have no machine-readable path: the HTML report is for browsers, not
for HA cards.

This plan adds an optional MQTT publishing step to the reporter daemon. After
rendering a report, the daemon publishes two retained MQTT messages containing
the underlying chart data in a format suitable for consumption by
`apexcharts-card` (the popular community HA custom card):

1. **Schedule series** — all time-series data tracks (import price, export
   price, battery SOC, grid import/export, PV forecast, baseload) as
   apex-compatible `[ISO-timestamp, value]` arrays, serialised as a single JSON
   object published to a configurable `chart_topic`.

2. **Summary figures** — scalar economic and exchange statistics from the last
   solve, published to a configurable `summary_topic`.

HA MQTT discovery payloads are published so both topics appear automatically as
HA sensors without manual HA YAML configuration.

---

## Critical design decisions

### Why MQTT and not direct HA WebSocket/REST API

The reporter is already an MQTT daemon. All mimirheim ecosystem tooling communicates
via MQTT. Introducing a dependency on HA's WebSocket or REST API would create a
hard coupling to the specific HA instance's URL, authentication tokens, and
availability. An HA restart would require the reporter to re-authenticate.
MQTT decouples them completely.

### One payload per chart topic, replaced after every solve

Both `chart_topic` and `summary_topic` are retained. Each successful solve
overwrites the previous value. Home Assistant sensors subscribed to these topics
update immediately. Old data is never left stale: if the reporter daemon is
stopped, the broker holds the last known values, which is correct behaviour for
a schedule that changes infrequently.

### Data format: JSON object with named series arrays

The payload for `chart_topic` is a flat JSON object:

```json
{
  "solve_time_utc": "2026-04-02T14:00:00Z",
  "import_price": [["2026-04-02T14:00:00Z", 0.24], ["2026-04-02T14:15:00Z", 0.25], ...],
  "export_price": [["2026-04-02T14:00:00Z", 0.05], ...],
  "grid_import_kw": [...],
  "grid_export_kw": [...],
  "pv_kw": [...],
  "baseload_kw": [...],
  "battery__home_battery__soc_kwh": [...],
  "battery__home_battery__charge_kw": [...]
}
```

Per-device series use the pattern `{device_type}__{device_name}__{metric}`.
Double underscore separates type, name, and metric so device names that contain
a single underscore remain unambiguous.

Timestamps in each array entry are ISO-8601 UTC strings. `apexcharts-card`'s
`data_generator` function receives these as strings and can convert them to
milliseconds with `new Date(ts).getTime()`.

### Only device types present in the dump are included

If a solve has no EV chargers, no EV series are published. The HA sensor payload
size stays proportional to the actual system, not a theoretical maximum.

### The summary payload mirrors the render helpers' economic summary

```json
{
  "solve_time_utc": "2026-04-02T14:00:00Z",
  "strategy": "minimize_cost",
  "solve_status": "ok",
  "naive_cost_eur": 3.12,
  "optimised_cost_eur": 1.87,
  "soc_credit_eur": 0.23,
  "effective_cost_eur": 1.64,
  "saving_eur": 1.48,
  "saving_pct": 47.4,
  "self_sufficiency_pct": 62.1,
  "grid_import_kwh": 4.2,
  "grid_export_kwh": 0.8
}
```

### HA discovery is optional, controlled by `ha_discovery.enabled` in reporter config

The reporter already has `helper_common.config.MqttConfig` but no HA discovery
config. Add a new optional `ha_discovery: ReporterDiscoveryConfig | None = None`
section to `ReporterConfig`. When present and enabled, discovery payloads are
published for the chart and summary sensors on startup and on HA birth message.

Use the post-plan-47 device JSON format (`homeassistant/device/{device_id}/config`)
for the reporter's HA discovery payload, so cleanup is a single topic publish.

### The reporter does NOT need `HomeAssistantConfig` from `helper_common`

The reporter is an `MqttDaemon` (not `HelperDaemon`), so it does not get the
trigger/HA-birth machinery for free. Add a minimal `ReporterDiscoveryConfig`
Pydantic model to `reporter/config.py` and subscribe to `homeassistant/status`
manually inside `ReporterDaemon._on_connect()` if discovery is enabled.

### Chart and summary topics are independent of `reporting.enabled`

The chart and summary pub are driven by the same `dump_available` notification
already driving the report render. These topics are added to the existing
`_on_notification` flow in `ReporterDaemon`. No new subscription is needed.

When `chart_topic` is None (the default), no chart publication step runs. Same
for `summary_topic`. Existing deployments are unaffected.

---

## Relevant source locations

```
mimirheim_helpers/reporter/reporter/
    config.py       — add ChartPublishingConfig, ReporterDiscoveryConfig;
                      add chart_publishing and ha_discovery sections to ReporterConfig
    chart_publisher.py  — new: build_chart_payload() and build_summary_payload()
    daemon.py       — modify: add chart publish step in _on_notification(),
                      subscribe to homeassistant/status if discovery enabled,
                      publish discovery on connect and on HA birth message

mimirheim_helpers/reporter/tests/unit/
    test_config_schema.py   — add ChartPublishingConfig and ReporterDiscovery
                              Config schema tests
    test_chart_publisher.py — new: unit tests for build_chart_payload() and
                              build_summary_payload()
    test_daemon_chart.py    — new: unit tests for chart publish step in daemon

examples/homeassistant/
    hioo_reporter_dashboard.yaml  — new: example apex-charts-card HA dashboard YAML
```

---

## Tests first

### `tests/unit/test_config_schema.py` — additions

Add tests for the new config sections to the existing file:

```python
# --- ChartPublishingConfig ---

def test_chart_publishing_config_defaults() -> None:
    """All fields default correctly: chart_topic and summary_topic are None,
    max_payload_bytes is 65536."""

def test_chart_publishing_config_extra_field_rejected() -> None:
    """Unknown fields raise ValidationError (extra='forbid')."""

def test_chart_publishing_config_max_payload_bytes_ge_zero() -> None:
    """max_payload_bytes must be >= 0; negative values raise ValidationError."""

# --- ReporterDiscoveryConfig ---

def test_reporter_discovery_config_defaults() -> None:
    """enabled=False, discovery_prefix='homeassistant', device_id and
    device_name default to None/empty."""

def test_reporter_ha_discovery_in_root_config() -> None:
    """ReporterConfig with ha_discovery section validates correctly."""
```

### `tests/unit/test_chart_publisher.py` — new file

Tests use small synthetic dump dicts constructed inline (no file I/O).

```python
# test_build_chart_payload_structure
def test_build_chart_payload_contains_required_keys() -> None:
    """build_chart_payload(inp, out) returns a dict with keys:
    solve_time_utc, import_price, export_price, grid_import_kw,
    grid_export_kw. PV and baseload series are present when the inp dict
    contains them."""

def test_series_entries_are_two_element_lists() -> None:
    """Every entry in each series array is a 2-element list [iso_str, float].
    The ISO string must parseable by datetime.fromisoformat()."""

def test_series_length_matches_horizon_prices() -> None:
    """All series arrays have the same length as inp['horizon_prices']."""

def test_device_series_included_for_batteries() -> None:
    """When inp contains battery keys, battery soc and charge/discharge
    series appear in the payload under the {type}__{name}__{metric}
    naming convention."""

def test_device_series_absent_when_no_batteries() -> None:
    """When inp has no battery keys, no battery__ series appear."""

# test_build_summary_payload_structure
def test_build_summary_payload_contains_required_keys() -> None:
    """build_summary_payload(inp, out) returns a dict with all expected
    scalar fields: solve_time_utc, strategy, solve_status, naive_cost_eur,
    optimised_cost_eur, soc_credit_eur, effective_cost_eur, saving_eur,
    saving_pct, self_sufficiency_pct, grid_import_kwh, grid_export_kwh."""

def test_build_summary_saving_pct_correct() -> None:
    """saving_pct = (naive - effective) / naive * 100. Verified with a
    known naive_cost_eur and effective_cost_eur in the inp/out dicts."""

def test_build_summary_saving_pct_zero_naive_handled() -> None:
    """When naive_cost_eur is 0, saving_pct is 0.0 (no ZeroDivisionError)."""
```

### `tests/unit/test_daemon_chart.py` — new file

```python
# test_chart_published_after_notification
def test_chart_payload_published_when_chart_topic_configured() -> None:
    """When reporter config has chart_publishing.chart_topic set, processing
    a dump-available notification calls client.publish(chart_topic, ...) with
    a non-empty JSON payload."""

def test_summary_payload_published_when_summary_topic_configured() -> None:
    """When reporter config has chart_publishing.summary_topic set,
    client.publish(summary_topic, ...) is called with summary JSON."""

def test_chart_not_published_when_chart_topic_is_none() -> None:
    """When chart_topic is None (default), no publish call is made for chart
    data."""

def test_chart_publish_uses_retain_true_qos1() -> None:
    """Chart and summary payloads are published retained, QoS 1."""

def test_chart_payload_truncated_when_exceeds_max_bytes() -> None:
    """When the serialised chart payload exceeds max_payload_bytes, the
    daemon logs a warning and does NOT publish it (rather than publishing
    oversized data that may exceed broker limits)."""

def test_discovery_published_on_connect_when_enabled() -> None:
    """When ha_discovery.enabled is True, _on_connect publishes a device
    JSON discovery payload to homeassistant/device/{device_id}/config."""

def test_discovery_republished_on_ha_birth_message() -> None:
    """When homeassistant/status 'online' arrives, discovery is re-published
    after a short delay."""

def test_discovery_not_published_when_disabled() -> None:
    """When ha_discovery is None or enabled=False, no discovery publish occurs
    on connect."""
```

Run `cd mimirheim_helpers/reporter && uv run pytest` — all new tests must fail before
implementation begins.

---

## Implementation

### Step 1 — `reporter/config.py` additions

```python
class ChartPublishingConfig(BaseModel):
    """Configuration for MQTT chart data publishing.

    Controls whether the reporter publishes apex-charts-compatible series
    data and summary statistics after each report render. Both topics are
    independent; either or both may be set.

    Attributes:
        chart_topic: MQTT topic for the time-series chart data payload.
            When None, no chart data is published.
        summary_topic: MQTT topic for the scalar economic summary payload.
            When None, no summary is published.
        max_payload_bytes: Maximum allowed serialised payload size in bytes.
            Payloads that exceed this limit are dropped with a warning rather
            than published, protecting brokers with low message-size settings.
            0 means unlimited.
    """
    model_config = ConfigDict(extra="forbid")

    chart_topic: str | None = Field(default=None)
    summary_topic: str | None = Field(default=None)
    max_payload_bytes: int = Field(default=65536, ge=0)


class ReporterDiscoveryConfig(BaseModel):
    """HA MQTT discovery settings for the reporter daemon.

    When enabled, discovery payloads are published using the HA device
    JSON format (homeassistant/device/{device_id}/config). Requires HA
    2024.2 or later.

    Attributes:
        enabled: Enable HA MQTT discovery for reporter sensors.
        discovery_prefix: HA MQTT discovery topic prefix.
        device_id: HA device identifier. Defaults to mqtt.client_id.
        device_name: Human-readable HA device display name.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False)
    discovery_prefix: str = Field(default="homeassistant")
    device_id: str | None = Field(default=None)
    device_name: str = Field(default="mimirheim Reporter")
```

Add to `ReporterConfig`:

```python
chart_publishing: ChartPublishingConfig = Field(
    default_factory=ChartPublishingConfig,
    description="MQTT publishing of apex-charts compatible chart and summary data.",
)
ha_discovery: ReporterDiscoveryConfig | None = Field(
    default=None,
    description="HA MQTT discovery for chart and summary sensors.",
)
```

### Step 2 — `reporter/chart_publisher.py` (new file)

```python
"""Chart and summary MQTT payload builders for mimirheim-reporter.

This module is a pure-function library: it takes parsed dump dicts and
returns plain Python dicts suitable for JSON serialisation and MQTT
publication. It does not open files, connect to MQTT, or import from mimirheim.

The outputs are consumed by ReporterDaemon after each report render and
published to the configured chart and summary topics.

What this module does not do:
- It does not render HTML.
- It does not publish MQTT messages.
- It does not read configuration.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

STEP_MINUTES = 15


def build_chart_payload(inp: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Build an apex-charts-compatible time-series payload from a dump pair.

    Each series is a list of [ISO-timestamp-string, float] pairs, one entry
    per 15-minute solver time step. The ISO timestamps are UTC and can be
    converted to milliseconds in JavaScript with new Date(ts).getTime().

    Per-device series use the naming pattern:
        {device_type}__{device_name}__{metric}
    where double underscores separate the three components. Device types are
    'battery', 'ev', 'pv', 'load'. Metrics are 'soc_kwh', 'charge_kw',
    'discharge_kw', 'power_kw' (where applicable).

    Args:
        inp: Parsed SolveBundle JSON (the ``*_input.json`` dump).
        out: Parsed SolveResult JSON (the ``*_output.json`` dump).

    Returns:
        Dict with 'solve_time_utc' string and named series arrays.
    """
    ...


def build_summary_payload(inp: dict[str, Any], out: dict[str, Any]) -> dict[str, Any]:
    """Build a scalar economic summary payload from a dump pair.

    Args:
        inp: Parsed SolveBundle JSON.
        out: Parsed SolveResult JSON.

    Returns:
        Dict with scalar fields suitable for JSON serialisation.
    """
    ...
```

### Step 3 — `reporter/daemon.py` modifications

In `_on_notification()`, after the existing report render and `gc.collect()`:

```python
# Publish chart data if configured.
cfg_chart = self._reporter_config_chart  # ChartPublishingConfig reference
if cfg_chart.chart_topic is not None or cfg_chart.summary_topic is not None:
    self._publish_chart_data(inp_data, out_data, cfg_chart)
```

Add `_publish_chart_data()`:

```python
def _publish_chart_data(
    self,
    inp: dict[str, Any],
    out: dict[str, Any],
    cfg: ChartPublishingConfig,
) -> None:
    """Publish chart and summary payloads to configured MQTT topics.

    Skips publication if the serialised payload exceeds max_payload_bytes.

    Args:
        inp: Parsed SolveBundle JSON.
        out: Parsed SolveResult JSON.
        cfg: Chart publishing config section.
    """
    if cfg.chart_topic is not None:
        payload = json.dumps(build_chart_payload(inp, out))
        if cfg.max_payload_bytes > 0 and len(payload.encode()) > cfg.max_payload_bytes:
            logger.warning(
                "Chart payload (%d bytes) exceeds max_payload_bytes=%d; skipping.",
                len(payload.encode()),
                cfg.max_payload_bytes,
            )
        else:
            self._client.publish(cfg.chart_topic, payload, qos=1, retain=True)

    if cfg.summary_topic is not None:
        payload = json.dumps(build_summary_payload(inp, out))
        self._client.publish(cfg.summary_topic, payload, qos=1, retain=True)
```

For HA discovery: override `_on_connect()` to subscribe to
`homeassistant/status` and call `_publish_chart_discovery()` when discovery
is enabled. Override `_on_message()` to handle the HA birth message.

`_publish_chart_discovery()` publishes a single device JSON discovery payload
(using the post-plan-47 format) with one sensor entity per configured topic:

- `{device_id}_chart_series` — `state_topic: chart_topic`,
  `json_attributes_topic: chart_topic`, state is `solve_time_utc` value.
- `{device_id}_summary` — `state_topic: summary_topic`, state is
  `solve_time_utc`; `json_attributes_topic: summary_topic` provides all
  scalar attributes.

---

## Example HA dashboard

Create `examples/homeassistant/hioo_reporter_dashboard.yaml` with a complete
working example for the `apexcharts-card` custom component. The card uses
`data_generator` to read series from sensor attributes.

```yaml
# mimirheim reporter — ApexCharts dashboard example
#
# Requirements:
#   - apexcharts-card custom component (https://github.com/RomRider/apexcharts-card)
#   - mimirheim-reporter configured with chart_publishing.chart_topic and
#     ha_discovery.enabled: true
#   - The sensor entity_id below must match the unique_id in reporter config.

type: custom:apexcharts-card
graph_span: 12h
header:
  title: mimirheim Schedule — Next 12 hours
  show: true
now:
  show: true
  label: now
series:
  - entity: sensor.hioo_reporter_chart_series
    name: Import price (ct/kWh)
    color: '#ff6600'
    type: line
    extend_to: false
    data_generator: |
      return entity.attributes.import_price
        ? entity.attributes.import_price.map(([ts, v]) => [new Date(ts).getTime(), +(v*100).toFixed(1)])
        : [];
  - entity: sensor.hioo_reporter_chart_series
    name: Export price (ct/kWh)
    color: '#009900'
    type: line
    extend_to: false
    data_generator: |
      return entity.attributes.export_price
        ? entity.attributes.export_price.map(([ts, v]) => [new Date(ts).getTime(), +(v*100).toFixed(1)])
        : [];
  - entity: sensor.hioo_reporter_chart_series
    name: Battery SOC (kWh)
    color: '#0066cc'
    type: area
    opacity: 0.3
    extend_to: false
    data_generator: |
      // Replace 'home_battery' with your battery name as configured in mimirheim
      const key = 'battery__home_battery__soc_kwh';
      return entity.attributes[key]
        ? entity.attributes[key].map(([ts, v]) => [new Date(ts).getTime(), +v.toFixed(2)])
        : [];
  - entity: sensor.hioo_reporter_chart_series
    name: Grid import (kW)
    color: '#cc0000'
    type: bar
    extend_to: false
    data_generator: |
      return entity.attributes.grid_import_kw
        ? entity.attributes.grid_import_kw.map(([ts, v]) => [new Date(ts).getTime(), +v.toFixed(2)])
        : [];
  - entity: sensor.hioo_reporter_chart_series
    name: Grid export (kW)
    color: '#00aa00'
    type: bar
    extend_to: false
    data_generator: |
      return entity.attributes.grid_export_kw
        ? entity.attributes.grid_export_kw.map(([ts, v]) => [new Date(ts).getTime(), +v.toFixed(2)])
        : [];
  - entity: sensor.hioo_reporter_chart_series
    name: PV forecast (kW)
    color: '#ffcc00'
    type: area
    opacity: 0.4
    extend_to: false
    data_generator: |
      return entity.attributes.pv_kw
        ? entity.attributes.pv_kw.map(([ts, v]) => [new Date(ts).getTime(), +v.toFixed(2)])
        : [];
```

Include a second card example using the summary sensor to display a single-stat
Markdown or Entity card showing `saving_eur` and `self_sufficiency_pct`.

---

## Acceptance criteria

```bash
cd mimirheim_helpers/reporter && uv run pytest           # all new tests pass
cd mimirheim_helpers/reporter && uv run pytest --reuse-db # no regressions
```

Behavioural checks:

1. Reporter configured with `chart_publishing.chart_topic: mimir/reporter/chart` and
   `chart_publishing.summary_topic: mimir/reporter/summary`; dump-available
   notification delivered; both topics receive retained JSON payloads within 1
   second of the notification.

2. `chart_topic` payload is valid JSON; contains `import_price` and
   `export_price` arrays; each array entry is `[iso_string, number]`; the
   ISO strings parse as valid datetimes.

3. `summary_topic` payload contains `saving_eur` and `effective_cost_eur`;
   `saving_pct` is correctly derived.

4. When the rendered payload exceeds `max_payload_bytes`, no publish is made to
   `chart_topic` and a WARNING log line is emitted.

5. HA discovery enabled: `homeassistant/device/{device_id}/config` contains
   sensor entries for `chart_series` and `summary`; HA shows both sensors on
   the reporter device card; `json_attributes_topic` provides all series data
   as sensor attributes.

6. Example dashboard YAML (`examples/homeassistant/hioo_reporter_dashboard.yaml`)
   is valid YAML, parseable without errors.

---
