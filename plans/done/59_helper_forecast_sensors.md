# Plan 59 — HA forecast sensors for input helpers

## Purpose

When a helper's `ha_discovery.enabled` is True, publish an additional HA MQTT
discovery payload for the helper's output topic. This creates a sensor entity in
HA that holds the current-horizon primary value as its state and exposes the full
forecast array as JSON attributes. Users can then reference the sensor in HA
dashboards, automations, and `apexcharts-card` charts without any manual topic
configuration.

This plan covers only the discovery side — no new MQTT topics, no payload format
changes. The data at `output_topic` is unchanged; HA is simply told where to find
it.

---

## Relevant IMPLEMENTATION_DETAILS sections

This plan does not touch the mimirheim core. All changes are within
`mimirheim_helpers/`. The relevant architecture sections are:

- §6 — module boundary rules (helper tools must not import from `mimirheim/io/`)
- §10 — fault resilience (discovery failures must not crash the daemon)

---

## Scope

**In scope:**
- `helper_common/discovery.py` — new `publish_forecast_sensor()` function and
  updated `_all_possible_helper_discovery_topics()` / `_active_helper_discovery_topics()`
- `helper_common/config.py` — `forecast_sensor: bool` field on `HomeAssistantConfig`
- `_publish_discovery()` override in each concrete helper that has an output topic:
  nordpool, zonneplan_prices, pv_fetcher, baseload_static, baseload_ha, baseload_ha_db
- Unit tests for all changed modules

**Not in scope:**
- Scheduler and reporter (neither publishes a forecast output topic)
- Any change to `output_topic` payload format
- Any new MQTT topics or new retained messages
- Any change to the mimirheim core or solver

---

## Decisions

### What is the sensor state value?

The sensor state is the primary value for the **next-future step** (the first
step whose `ts` is greater than or equal to now). This gives a scalar that HA
can display in the entity card and use in automations.

- Price helpers: `import_eur_per_kwh` of the next-future step.
- PV and baseload helpers: `kw` of the next-future step.

The `value_template` extracts this using a Jinja2 expression. Because MQTT
`json_attributes` in HA are set from the full message, the array is available
for charting via `apexcharts-card` pointing at `json_attributes_path`.

### How is the current step extracted in HA?

HA Jinja2 templates cannot filter a JSON array by comparing ISO timestamps to
`now()` reliably without additional sensors. For the state value, use the first
element of the array as a pragmatic approximation — helpers publish future steps
only (past steps are filtered out before publishing). The full array is available
in attributes for tools that need more than the first value.

`value_template`:
- Price helpers: `{{ value_json[0].import_eur_per_kwh | default(0) | round(4) }}`
- PV / baseload helpers: `{{ value_json[0].kw | default(0) | round(3) }}`

### Should the sensor have a device_class?

- Price helpers: `monetary`, unit `EUR/kWh`. This is technically non-standard
  (HA has no built-in `monetary` device_class for sensor), so use no `device_class`,
  unit `EUR/kWh`.
- PV helpers: `power`, unit `kW`.
- Baseload helpers: `power`, unit `kW`.

### Does the sensor history pollute the HA recorder?

The sensor state changes on every successful fetch cycle (typically once or twice
per day for price helpers, less for PV). HA will record each state change,
including the full JSON attributes array (~2–4 kB for 48 steps). To avoid
unbounded recorder growth, publish the discovery payload with
`"enabled_by_default": false`. Users who want history can manually enable the
entity; users who only want charting leave it disabled (attributes are still
available to `apexcharts-card`).

### One sensor per helper or one sensor per output topic?

For pv-fetcher and all baseload helpers: one sensor per helper (single `output_topic`).

For pv-ml-learner: one sensor per configured array, because each array in `cfg.arrays`
publishes to its own `array_cfg.output_topic` independently. The discovery
`tool_name` per array is `pv_ml_learner_{array_name}` (matching the pattern used
for that array's forecast). This means `_all_possible_helper_discovery_topics()` is
called once per array. A removed array will leave a stale forecast sensor topic on
the broker because the array name is no longer known at connect time — this is an
accepted limitation of the first implementation and should be noted in the wiki.

### Stale topic cleanup

`_all_possible_helper_discovery_topics()` must include the forecast sensor topic
so that setting `forecast_sensor: false` removes the discovery payload on the
next connect.

The naming convention for the forecast sensor is:
`{discovery_prefix}/sensor/{tool_name}_forecast/config`

---

## Files to create or edit

```
mimirheim_helpers/
  common/
    helper_common/
      config.py                  ← add forecast_sensor field to HomeAssistantConfig
      discovery.py               ← publish_forecast_sensor() + updated possible/active sets
    tests/unit/
      test_discovery.py          ← new tests for forecast sensor publish + cleanup + invariant
  prices/nordpool/nordpool/
    __main__.py                  ← override _publish_discovery() to call publish_forecast_sensor
  prices/zonneplan/zonneplan_prices/
    __main__.py                  ← same
  pv/forecast.solar/pv_fetcher/
    __main__.py                  ← same
  pv/pv_ml_learner/pv_ml_learner/
    __main__.py                  ← call publish_forecast_sensor() once per array in _publish_discovery()
  baseload/static/baseload_static/
    __main__.py                  ← same
  baseload/homeassistant/baseload_ha/
    __main__.py                  ← same
  baseload/homeassistant_db/baseload_ha_db/
    __main__.py                  ← same
```

No new files. All changes are to existing modules.

---

## TDD workflow

### Step 1 — config.py: add `forecast_sensor` field

Write tests first in the relevant helper config test files (nordpool, zonneplan,
pv_fetcher, baseload_*) asserting that:

- `forecast_sensor` defaults to `False` when absent from YAML.
- `forecast_sensor: true` parses correctly.
- `extra="forbid"` still rejects unknown fields.

These tests will fail until the field is added to `HomeAssistantConfig`.

Then add `forecast_sensor: bool = Field(default=False, ...)` to
`HomeAssistantConfig` in `helper_common/config.py`.

Confirm tests pass.

### Step 2 — discovery.py: publish_forecast_sensor()

Write tests first in `test_discovery.py`:

**New tests to add:**

```
TestForecastSensor
  test_forecast_sensor_published_when_enabled
    When forecast_sensor=True, publish call to
    {prefix}/sensor/{tool_name}_forecast/config is present.
  test_forecast_sensor_not_published_when_disabled
    When forecast_sensor=False, no forecast sensor publish call.
  test_forecast_sensor_deleted_when_disabled
    When forecast_sensor=False, the forecast sensor topic receives a
    None payload (deleted).
  test_forecast_sensor_payload_has_required_keys
    Payload contains: name, unique_id, state_topic, value_template,
    json_attributes_topic, entity_category, enabled_by_default, device.
  test_forecast_sensor_state_topic_is_output_topic
    state_topic and json_attributes_topic both equal the supplied output_topic.
  test_forecast_sensor_unit_and_device_class_correct_for_price_type
    unit_of_measurement == "EUR/kWh", no device_class key.
  test_forecast_sensor_unit_and_device_class_correct_for_power_type
    unit_of_measurement == "kW", device_class == "power".
  test_all_possible_includes_forecast_sensor_topic
    _all_possible_helper_discovery_topics() includes
    {prefix}/sensor/{tool_name}_forecast/config.

TestInvariant (update existing)
  test_all_possible_covers_publish_and_delete_targets
    Update to account for the new forecast sensor topic by supplying
    forecast_sensor=True / False to verify both paths are covered.
```

All tests will fail. Then implement:

```python
def publish_forecast_sensor(
    client: Any,
    *,
    tool_name: str,
    tool_label: str,
    output_topic: str,
    value_template: str,
    unit: str,
    device_class: str | None,
    device_block: dict[str, Any],
    discovery_prefix: str = "homeassistant",
) -> None:
```

Update `_all_possible_helper_discovery_topics()` and
`_active_helper_discovery_topics()` to include the forecast sensor topic.
Update `publish_trigger_discovery()` signature to accept
`forecast_sensor: bool = False` and `output_topic: str | None = None`,
and call `publish_forecast_sensor()` internally when both are provided.

Confirm all discovery tests pass.

### Step 3 — concrete helper overrides

For each of the six helpers, update `_publish_discovery()` to pass
`forecast_sensor` and `output_topic` to `publish_trigger_discovery()`.

The base class `HelperDaemon._publish_discovery()` does not know `output_topic`,
so each helper overrides it:

```python
def _publish_discovery(self) -> None:
    ha = self._ha_config()
    if ha is None:
        return
    publish_trigger_discovery(
        self._client,
        tool_name=self.TOOL_NAME,
        tool_label=self._tool_label(),
        trigger_topic=self._config.trigger_topic,
        stats_topic=getattr(self._config, "stats_topic", None),
        forecast_sensor=ha.forecast_sensor,
        output_topic=self._config.output_topic,   # known by this helper
        forecast_value_template=...,              # helper-specific
        forecast_unit=...,                        # helper-specific
        forecast_device_class=...,                # helper-specific
        discovery_prefix=ha.discovery_prefix,
    )
```

Write a test for each helper's `_publish_discovery()` override:

```
test_{helper}_publishes_forecast_sensor_when_forecast_sensor_true
  Build a minimal config with ha_discovery.enabled=True and
  ha_discovery.forecast_sensor=True. Call _publish_discovery() via
  a mock client. Assert publish was called for
  homeassistant/sensor/{tool_name}_forecast/config.

test_{helper}_does_not_publish_forecast_sensor_when_forecast_sensor_false
  Same setup, forecast_sensor=False. Assert no call for the forecast topic.
```

One test class per helper (6 single-topic helpers + pv-ml-learner = 7 total).

For pv-ml-learner, the tests must reflect the per-array structure:

```
test_pv_ml_learner_publishes_forecast_sensor_per_array_when_enabled
  Config with two arrays ("south", "east") and forecast_sensor=True.
  Assert publish was called for:
    homeassistant/sensor/pv_ml_learner_south_forecast/config
    homeassistant/sensor/pv_ml_learner_east_forecast/config

test_pv_ml_learner_does_not_publish_forecast_sensor_when_disabled
  Same config, forecast_sensor=False. Assert no calls for the forecast topics.
```

Note: pv-ml-learner's `_publish_discovery(self, client)` already takes a `client`
argument (it subclasses `MqttDaemon` directly, not `HelperDaemon`). The override
continues to accept `client` as a parameter.

These tests will fail until the pv-ml-learner override is implemented.

### Step 4 — run full suite

```bash
uv run pytest
```

All tests must pass. No regressions.

---

## Acceptance criteria

- [ ] `HomeAssistantConfig` has `forecast_sensor: bool = False`.
- [ ] `_all_possible_helper_discovery_topics()` includes the `_forecast` sensor topic.
- [ ] When `ha_discovery.forecast_sensor: true`, each applicable helper publishes
      a retained discovery payload at `{prefix}/sensor/{tool_name}_forecast/config`.
- [ ] The forecast sensor payload contains: `state_topic`, `json_attributes_topic`
      (both equal to `output_topic`), `value_template`, `unit_of_measurement`,
      `entity_category: "diagnostic"`, `enabled_by_default: false`, `device`.
- [ ] When `ha_discovery.forecast_sensor: false` (or `ha_discovery` absent), the
      forecast sensor discovery topic receives a `None` payload on every connect
      (clean stale removal).
- [ ] For pv-ml-learner, one forecast sensor is published per configured array, with
      `tool_name = pv_ml_learner_{array_name}` and the array's `output_topic`.
- [ ] Scheduler and reporter are unaffected.
- [ ] All existing tests continue to pass.
- [ ] `uv run pytest` exits 0 with no new failures.
- [ ] Wiki and README updated (see Step 5).

### Step 5 — documentation updates

**`wiki/Home-Assistant.md`**

Add a new section after the "Entities created" table:

```
### Helper forecast sensors

When a helper's `ha_discovery.forecast_sensor` is `true`, the helper also
publishes a forecast sensor entity to HA. This gives the current next-step
value as the entity state and the full forecast array as JSON attributes.

| Helper | Entity name | State value | Attributes |
|--------|-------------|-------------|------------|
| Nordpool / Zonneplan | `{label} Forecast` | `import_eur_per_kwh` of next step | Full price array as `forecast` |
| PV Fetcher | `{label} Forecast` | `kw` of next step | Full power array as `forecast` |
| Baseload helpers | `{label} Forecast` | `kw` of next step | Full load array as `forecast` |
| PV ML Learner | `{label} {array_name} Forecast` | `kw` of next step | Full array forecast as `forecast` |

All forecast sensors are `enabled_by_default: false`. Enable them in HA manually
if you want state history recorded. The `forecast` attribute is available to
`apexcharts-card` even when the entity is disabled.

Enable in your helper config:

```yaml
ha_discovery:
  enabled: true
  forecast_sensor: true
```

**Note for pv-ml-learner:** one forecast sensor is published per configured array.
If an array is removed from config, its discovery topic will remain on the broker
as a stale entry until manually cleared. This is a known limitation of the first
implementation.
```

**Per-helper wiki pages** (`wiki/Helpers/Nordpool.md`, `wiki/Helpers/Zonneplan.md`,
`wiki/Helpers/PV-Fetcher.md`, `wiki/Helpers/Baseload-*.md`,
`wiki/Helpers/PV-ML-Learner.md`):

Add a short "Home Assistant forecast sensor" section at the end of each page
describing the `forecast_sensor` config field, what entity HA creates, and the
`apexcharts-card` attribute path (`$.forecast`).

**Auto-generated config reference pages** (`wiki/Reference/Config-*.md`):

Run `python3 scripts/extract_config_docs.py` after adding `forecast_sensor` to
`HomeAssistantConfig`. The script regenerates all per-helper config reference
pages. Commit the regenerated files alongside the code change.

**`wiki/Reference/Config-Common.md`**:

This page documents `HomeAssistantConfig`. After adding `forecast_sensor`, run
`python3 scripts/extract_config_docs.py --only common` to regenerate it.
