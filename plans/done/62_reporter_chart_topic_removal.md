# Plan 62 — Deprecate and remove reporter chart_topic

## Purpose

After plans 59 and 61, every series that the reporter currently publishes to
`chart_topic` is available as an HA MQTT sensor with JSON attributes:

| chart_topic series | Replacement after plans 59+61 |
|---|---|
| `import_price`, `export_price` | Helper forecast sensor (nordpool / zonneplan output topic) |
| `pv_kw` *(aggregated)* | Per-array helper forecast sensors (plan 59) |
| `baseload_kw` *(aggregated)* | Per-helper baseload forecast sensors (plan 59) |
| `grid_import_kw`, `grid_export_kw` | Grid sensor `forecast` attribute (plan 61) |
| `battery__{name}__soc_kwh` | Battery setpoint sensor `forecast.soc_kwh` attribute (plan 61) |
| `battery__{name}__charge_kw` | Battery setpoint sensor `forecast.kw` attribute (plan 61) |

`summary_topic` is not affected: it publishes economic scalars
(`naive_cost_eur`, `optimised_cost_eur`, `saving_eur`, `self_sufficiency_pct`,
etc.) that no other topic exposes. The reporter is not removed.

`chart_topic` itself is deprecated and removed. The `chart_publisher.py` module
and its tests are deleted. The `_publish_chart_data()` and
`_publish_chart_discovery()` methods in `ReporterDaemon` are removed. The HA
discovery entity previously registered for `chart_topic` (`chart_series` sensor)
is removed.

---

## Prerequisites

This plan must not be started until:

1. Plan 59 (`59_helper_forecast_sensors.md`) is complete and all tests pass.
2. Plan 61 (`61_setpoint_forecast_attributes.md`) is complete and all tests pass.

Both plans provide the replacement data paths that make `chart_topic` redundant.
Do not implement this plan in parallel with either prerequisite.

---

## Semantic differences to document before starting

The following differences exist between `chart_topic` and the plan 59/61
replacements. None represent lost information, but all require dashboard
configuration changes. The changelog and wiki migration guide must call each
one out explicitly.

### 1. PV and baseload aggregation

`chart_topic`'s `pv_kw` and `baseload_kw` series are **aggregated totals** across
all configured arrays and loads. Plan 59 provides **per-source** sensors (one per
array, one per load helper). A user who previously charted a single `pv_kw` series
now needs to add multiple series or configure their `apexcharts-card` to sum them.

This is an intentional improvement in granularity, not a regression.

### 2. Timestamps

~~Plan 59/61 attributes were indexed arrays of plain objects with no embedded
timestamps. Users had to compute step timestamps from `solve_time_utc` + step
index × 15 minutes.~~

**Resolved.** The `mqtt_publisher.py` fix (post plan 61) now injects a `ts` ISO
timestamp into every schedule step before publishing the retained schedule blob.
All forecast attributes — grid, per-device setpoint, and SOC — therefore include
`{"ts": "2026-05-31T12:00:00Z", ...}` on every entry. The apexcharts-card
`data_generator` pattern `s => [new Date(s.ts).getTime(), value]` works directly,
matching the `[[ts, value]]` convention that `chart_topic` used. This difference
no longer exists.

### 3. Battery charge_kw sign convention

`chart_topic` publishes `battery__{name}__charge_kw` as `max(0.0, -kw)` — charge
power as a positive number, discharge clipped to zero. Plan 61's `forecast.kw` is
the signed solver value (negative = charging, positive = discharging). To
reproduce the old series in an apexcharts-card transform:

```js
transform: "return x.map(s => Math.max(0, -s.kw))"
```

### 4. Battery SOC series starting point

`chart_topic` emits `len(schedule) + 1` SOC entries: the **initial** SOC (before
step 0) followed by the end-of-step SOC for each step. Plan 61 starts from step
0's end-SOC only; there is no t-minus-1 entry. The initial SOC is available
separately as the battery input sensor.

Note that plan 61's SOC values are sourced directly from `device_soc_kwh` in the
solver output, which is more accurate than the `chart_topic` reconstruction (which
used average segment efficiency to re-derive SOC from the kw series).

---

## Relevant IMPLEMENTATION_DETAILS sections

- §10 — fault resilience. The reporter is a helper, not the core solver. Removing
  a method in the reporter daemon cannot affect solver operation.

---

## Scope

**In scope:**
- `reporter/reporter/daemon.py` — remove `_publish_chart_data()`,
  `_publish_chart_discovery()`, `chart_topic` references from `_on_connect`,
  `_on_message`, and any import.
- `reporter/reporter/config.py` — remove `chart_topic` field from
  `ChartPublishingConfig` (or remove the entire `ChartPublishingConfig` class
  if `summary_topic` is the only remaining field; consolidate if appropriate).
- `reporter/reporter/chart_publisher.py` — delete `build_chart_payload()`. The
  `build_summary_payload()` function remains; if both functions live in
  `chart_publisher.py`, rename the module to `summary_publisher.py` and update
  the import in `daemon.py`.
- `reporter/tests/unit/test_daemon_chart.py` — delete this file entirely.
- `reporter/tests/unit/test_config_schema.py` — remove all `chart_topic` fixture
  data and assertions.
- `wiki/Reference/Config-Reporter.md` — remove `chart_topic` field documentation.
  This file is auto-generated; run `python3 scripts/extract_config_docs.py` after
  the config change.
- `wiki/Helpers/Reporter.md` — remove or update the section describing
  `chart_topic`.
- `CHANGELOG.md` or release notes — document the removal and migration path.

**Not in scope:**
- `summary_topic` and `build_summary_payload()` — these are not affected.
- Reporter HTML rendering, inventory, or GC logic.
- Any mimirheim core module.

---

## Decisions

### Config field removal strategy

Remove `chart_topic` from `ChartPublishingConfig`. A config file that still
contains `chart_topic: ...` will be rejected at startup by
`model_config = ConfigDict(extra="forbid")`. This is the correct behaviour: the
field is gone and users must remove it.

Add a clear error message to the migration guide so users know what to do. Do not
add a compatibility shim.

### HA discovery entity removal

The `{device_id}_chart_series` component in the reporter's discovery payload must
be removed. Publishing a discovery payload without the `chart_series` component
causes HA to remove that entity from its registry on the next discovery refresh.
Because the reporter uses the single-payload HA MQTT device JSON format, no
explicit deletion of the old topic is needed — the new payload simply omits the
component.

### chart_publisher.py or summary_publisher.py

`chart_publisher.py` currently contains both `build_chart_payload()` and
`build_summary_payload()`, plus `_parse_utc()`, `_fmt_utc()`, and the metrics
imports. After removing `build_chart_payload()`, whatever remains can stay in
`chart_publisher.py` (renamed `summary_publisher.py`) or remain as-is if the
filename is not misleading. Decide at implementation time and update the import
in `daemon.py` accordingly.

---

## Files to delete

- `reporter/reporter/chart_publisher.py` — if `build_chart_payload()` is the
  only substantial function and helpers can be inlined into what remains. If
  `build_summary_payload()` and its helpers are also in this file, rename the
  file rather than deleting it (see above).
- `reporter/tests/unit/test_daemon_chart.py` — entirely covers `chart_topic`
  behaviour that no longer exists.

---

## TDD workflow

Because this plan removes behaviour rather than adding it, the TDD workflow is
inverted: tests are deleted or reduced first, and then the implementation follows.

### Step 1 — confirm baseline

```bash
uv run pytest
```

Confirm all tests pass before touching any file.

### Step 2 — delete test_daemon_chart.py

Delete `reporter/tests/unit/test_daemon_chart.py` in full.

Run `uv run pytest` — still green (the deleted file only tested `chart_topic`
behaviour that is about to be removed).

### Step 3 — remove chart_topic from test_config_schema.py

In `reporter/tests/unit/test_config_schema.py`:

- Remove any fixture dict entries that set `"chart_topic": ...`.
- Remove assertions that reference `cfg.chart_topic` or
  `cfg.chart_publishing.chart_topic`.
- If a test specifically validates that `chart_topic` is accepted, delete that
  test. If a test validates overall `ChartPublishingConfig` structure, update it
  to reflect the post-removal shape.

Run `uv run pytest` — still green (removed assertions no longer conflict with
anything).

### Step 4 — remove chart_topic from config.py

Remove `chart_topic` from `ChartPublishingConfig` in `reporter/reporter/config.py`.

Run `uv run pytest` — still green (no remaining test references `chart_topic`).

### Step 5 — remove chart methods from daemon.py

Remove from `ReporterDaemon`:

- `_publish_chart_data()` method.
- `_publish_chart_discovery()` method.
- The `if chart_cfg.chart_topic is not None:` branch inside
  `_publish_chart_data()` (already removing the whole method).
- The `chart_topic` check and `_publish_chart_discovery` call inside
  `_on_connect`.
- The `_publish_chart_discovery` call inside `_on_message`.
- Any import of `build_chart_payload` from `chart_publisher`.

If `chart_topic` was the only reason the reporter subscribed to
`homeassistant/status`, and `summary_topic` with its discovery sensor also
requires re-publishing on HA reconnect, then the `homeassistant/status`
subscription and re-discovery call must be preserved for the summary sensor.
Check `_on_connect` and `_on_message` carefully before removing anything there.

Run `uv run pytest` — still green.

### Step 6 — remove or rename chart_publisher.py

If `build_chart_payload()` can be removed without touching `build_summary_payload()`,
do so and keep the file as `chart_publisher.py` (or rename to `summary_publisher.py`
at your discretion). Update the `daemon.py` import.

Run `uv run pytest` — all tests pass.

### Step 7 — update wiki and docs

- Run `python3 scripts/extract_config_docs.py --only reporter` to regenerate
  `wiki/Reference/Config-Reporter.md`.
- Update `wiki/Helpers/Reporter.md` to remove the `chart_topic` section and add
  a migration note pointing users to plan 59 and plan 61 as replacements.
- Add a changelog entry describing the removal and the migration path:
  - `chart_topic` is removed. Users who configured `chart_topic` must remove it
    from their `reporter.yaml` or startup will fail.
  - Replacement: use the HA MQTT entities created by plan 59 (helper forecast
    sensors) and plan 61 (setpoint forecast attributes) in `apexcharts-card`.
  - Call out the three remaining semantic differences:
    1. `pv_kw` and `baseload_kw` are now per-source rather than aggregated.
    2. ~~Timestamps~~ — no action needed; every forecast entry now includes a `ts`
       ISO string. Use `s => [new Date(s.ts).getTime(), value]` in `data_generator`.
    3. `battery__{name}__charge_kw` was `max(0, -kw)`; `forecast.kw` is signed —
       apply `Math.max(0, -s.kw)` in the apexcharts-card transform.
    4. Battery SOC series no longer includes the pre-step-0 initial SOC entry;
       source initial SOC from the battery input sensor if needed.

### Step 8 — final test run

```bash
uv run pytest
```

All tests must pass. No regressions.

---

## Acceptance criteria

- [ ] `chart_topic` field is absent from `ChartPublishingConfig`. A config YAML
      that contains `chart_topic` is rejected at startup with a Pydantic
      validation error.
- [ ] `ReporterDaemon` no longer calls `_publish_chart_data()` or
      `_publish_chart_discovery()`.
- [ ] The `{device_id}_chart_series` HA entity is absent from the reporter
      discovery payload.
- [ ] `build_chart_payload()` no longer exists in the codebase.
- [ ] `test_daemon_chart.py` is deleted.
- [ ] `summary_topic` and `build_summary_payload()` are unaffected and continue
      to work.
- [ ] `wiki/Reference/Config-Reporter.md` is regenerated and contains no
      reference to `chart_topic`.
- [ ] `wiki/Helpers/Reporter.md` contains a migration note.
- [ ] All existing tests continue to pass.
- [ ] `uv run pytest` exits 0 with no new failures.
