# Plan 64 — Remove all MQTT publishing from the reporter

## Purpose

After plan 62 (`chart_topic` removed) and plan 63 (`config output capability
properties`), the reporter publishes exactly one thing to MQTT: a scalar economic
summary to `summary_topic`. Three of those scalars (`naive_cost_eur`,
`optimised_cost_eur`, `soc_credit_eur`) are already present in the `last_solve`
topic published by mimirheim core. The remaining three (`self_sufficiency_pct`,
`grid_import_kwh`, `grid_export_kwh`) are schedule-level aggregates that can be
computed trivially from `result.schedule` at the point `publish_last_solve_status`
is called.

Once core publishes all six fields, `summary_topic` carries no information that
is not already available from `last_solve`. The reporter's MQTT client can then be
used exclusively as a subscriber (receiving dump-available notifications), never
as a publisher. `ChartPublishingConfig`, `ReporterDiscoveryConfig`,
`_publish_summary_data()`, `_publish_summary_discovery()`, `summary_publisher.py`,
and all associated tests can be deleted.

`strategy` and `solve_status` are also already in `last_solve`, and `saving_eur`,
`saving_pct`, `effective_cost_eur` are trivially derived from the cost fields in
any HA template sensor.

---

## Prerequisites

Plan 63 must be complete and all tests must pass before starting this plan.

---

## Decisions

### Where the schedule aggregation lives

The computation of `self_sufficiency_pct`, `grid_import_kwh`, and
`grid_export_kwh` from a `list[ScheduleStep]` is added as a private helper
`_schedule_summary()` in `mimirheim/io/mqtt_publisher.py`. It is called only by
`publish_last_solve_status`. It is not added to `SolveResult` (those fields are
derived; adding them to the model would require updating every golden file and
every test fixture that constructs `SolveResult`).

The `_STEP_HOURS = 15 / 60` constant is defined locally in `mqtt_publisher.py`
(the same constant exists in the reporter's `metrics.py` independently — no
shared dependency is introduced).

`self_sufficiency_pct` formula:
```
load_served_local = max(0.0, load_total_kwh - grid_import_kwh)
self_sufficiency_pct = round(load_served_local / load_total_kwh * 100.0, 1)
                       if load_total_kwh > 0.0 else 0.0
```

where `load_total_kwh` sums `max(0.0, -sp.kw) * _STEP_HOURS` for all
`DeviceSetpoint` entries with `type in ("static_load", "deferrable_load")`.

### HA discovery: `last_solve` attributes

The `solve_status` sensor in `ha_discovery.py` already sets
`json_attributes_topic: config.outputs.last_solve`. Because HA exposes every
key in the retained JSON as a sensor attribute, the three new fields appear
automatically without any change to `ha_discovery.py`.

### Config field removal strategy

`ChartPublishingConfig` and `ReporterDiscoveryConfig` are deleted entirely.
`chart_publishing` and `ha_discovery` fields are removed from `ReporterConfig`.
A `reporter.yaml` that still contains either section is rejected at startup by
`extra="forbid"`. This is the correct behaviour. The migration guide must tell
users to remove those sections.

### Reporter becomes a pure subscriber

After this plan, `ReporterDaemon`:
- Subscribes to `reporting.notify_topic` only.
- Never calls `self._client.publish(...)`.
- Does not subscribe to `homeassistant/status`.
- Has no `_chart_config` or `_discovery_config` attributes.
- Has no `_on_connect` override (the base class `MqttDaemon._on_connect`
  handles the connection; the reporter adds only the `notify_topic` subscription).

The daemon module docstring already says it does not subscribe to
`homeassistant/status` beyond what the base class handles. After this plan that
statement is true without qualification.

---

## Relevant IMPLEMENTATION_DETAILS sections

- §10 — fault resilience. The reporter is a helper; changes here cannot affect
  solver operation.
- §6 — module boundaries. `mqtt_publisher.py` is in `mimirheim/io/`; the new
  helper function stays in that file.

---

## Scope

### In scope (core — `mimirheim/`)

- `mimirheim/io/mqtt_publisher.py` — add `_schedule_summary()` helper; add
  `self_sufficiency_pct`, `grid_import_kwh`, `grid_export_kwh` to the success
  branch of `publish_last_solve_status`.
- `tests/unit/test_mqtt_publisher.py` — add tests for the three new fields in
  `test_last_solve_status_includes_cost_fields` (or a new companion test).

### In scope (reporter — `mimirheim_helpers/reporter/`)

- `reporter/reporter/summary_publisher.py` — delete entirely.
- `reporter/reporter/config.py` — delete `ChartPublishingConfig` and
  `ReporterDiscoveryConfig` classes; remove `chart_publishing` and `ha_discovery`
  fields from `ReporterConfig`; update the `ReporterConfig` docstring.
- `reporter/reporter/daemon.py` — remove `_publish_summary_data()`,
  `_publish_summary_discovery()`, `_on_connect` override, `_on_message` override,
  `_chart_config` and `_discovery_config` attributes, `build_summary_payload`
  import, `ChartPublishingConfig` import; update module docstring.
- `reporter/tests/unit/test_summary_publisher.py` — delete entirely.
- `reporter/tests/unit/test_config_schema.py` — remove the `ChartPublishingConfig`
  section (four tests), the `ReporterDiscoveryConfig` section (two tests), and
  `test_reporter_chart_publishing_in_root_config` and
  `test_reporter_ha_discovery_in_root_config`.

### In scope (wiki/docs)

- `wiki/Reference/Config-Reporter.md` — regenerate via
  `python3 scripts/extract_config_docs.py --only reporter`.
- `wiki/Helpers/Reporter.md` — remove the MQTT summary publishing section; add
  migration note for `chart_publishing` and `ha_discovery` removal; update
  feature list.

### Not in scope

- `ha_discovery.py` — no changes needed (attributes appear automatically).
- Reporter HTML rendering, inventory, GC, or static file logic.
- Any other mimirheim core module.

---

## Files to delete

- `reporter/reporter/summary_publisher.py`
- `reporter/tests/unit/test_summary_publisher.py`

---

## TDD workflow

### Step 1 — confirm baseline

```bash
uv run pytest
```

Confirm all tests pass. Record the count.

### Step 2 — write failing tests for the three new `last_solve` fields

In `tests/unit/test_mqtt_publisher.py`, add a new test:

```python
def test_last_solve_status_includes_schedule_metrics() -> None:
    """publish_last_solve_status() includes self_sufficiency_pct,
    grid_import_kwh, and grid_export_kwh in the ok payload."""
```

The test must construct a `SolveResult` with a minimal schedule containing:
- Two steps, each with `grid_import_kw=2.0`, `grid_export_kw=0.0`.
- One `static_load` device per step with `kw=-1.0` (consuming 1 kW).

Expected values:
- `grid_import_kwh` = 2 steps × 2.0 kW × 0.25 h = 1.0 kWh
- `grid_export_kwh` = 0.0 kWh
- `load_total_kwh` = 2 × 1.0 × 0.25 = 0.5 kWh
- `load_served_local` = max(0.0, 0.5 - 1.0) = 0.0 kWh
- `self_sufficiency_pct` = 0.0 (all load imported)

Also add a second test with a PV device fully covering the load (no grid import):
- `grid_import_kw=0.0`, `grid_export_kw=0.0`, `pv` device `kw=1.0`,
  `static_load` device `kw=-1.0`.
- `self_sufficiency_pct` = 100.0

Run `uv run pytest` — the two new tests must fail (fields absent from payload).

### Step 3 — implement `_schedule_summary()` and update `publish_last_solve_status`

In `mimirheim/io/mqtt_publisher.py`:

1. Add the module-level constant `_STEP_HOURS: float = 15 / 60.0` (if not
   already present).
2. Add the private function:

```python
def _schedule_summary(schedule: list[ScheduleStep]) -> dict[str, float]:
    """Compute grid and self-sufficiency metrics from a solved schedule.

    Iterates over the schedule once to produce three aggregate values used
    in the last_solve status payload. All energy values are in kWh; the
    step duration is fixed at 15 minutes (0.25 h).

    Args:
        schedule: Ordered list of ScheduleStep objects from a SolveResult.

    Returns:
        Dict with keys grid_import_kwh, grid_export_kwh, self_sufficiency_pct.
    """
```

3. In the success branch of `publish_last_solve_status`, call
   `_schedule_summary(result.schedule)` and merge the three fields into the
   payload dict.

Run `uv run pytest` — both new tests must pass; no regressions.

### Step 4 — delete reporter summary tests

Delete:
- `reporter/tests/unit/test_summary_publisher.py`

Remove from `reporter/tests/unit/test_config_schema.py`:
- The `# ChartPublishingConfig` section and its four tests
  (`test_chart_publishing_config_defaults`, `test_chart_publishing_config_extra_field_rejected`,
  `test_chart_publishing_config_max_payload_bytes_ge_zero`,
  `test_chart_publishing_config_zero_is_unlimited`).
- The `# ReporterDiscoveryConfig` section and its two tests
  (`test_reporter_discovery_config_defaults`,
  `test_reporter_discovery_config_extra_field_rejected`).
- `test_reporter_ha_discovery_in_root_config`
- `test_reporter_chart_publishing_in_root_config`
- The `from reporter.config import ChartPublishingConfig` import.
- The `from reporter.config import ReporterDiscoveryConfig` import.

Run `uv run pytest` — still green (removed tests referenced code that is about to
be deleted).

### Step 5 — remove reporter MQTT code

**`reporter/reporter/summary_publisher.py`** — delete.

**`reporter/reporter/config.py`**:
- Delete `ChartPublishingConfig` class entirely.
- Delete `ReporterDiscoveryConfig` class entirely.
- Remove `chart_publishing` field from `ReporterConfig`.
- Remove `ha_discovery` field from `ReporterConfig`.
- Update `ReporterConfig` docstring to remove references to both.

**`reporter/reporter/daemon.py`**:
- Remove `from reporter.summary_publisher import build_summary_payload`.
- Remove `from reporter.config import ChartPublishingConfig, ReporterConfig`
  → change to `from reporter.config import ReporterConfig` (ChartPublishingConfig
  no longer exists).
- Remove `self._chart_config = config.chart_publishing` from `__init__`.
- Remove `self._discovery_config = config.ha_discovery` from `__init__`.
- Remove `_on_connect` override entirely (notify_topic subscription moves to
  `run()` or is handled by the base class — check `MqttDaemon` interface first
  to confirm the correct pattern; if the base class requires an `_on_connect`
  override to subscribe, keep a minimal override that calls super and subscribes
  to `notify_topic` only).
- Remove `_on_message` override if the only non-chart routing it did was
  `notify_topic` → `_on_notification`. Again check the base class contract.
- Remove `_publish_summary_data()` method.
- Remove `_publish_summary_discovery()` method.
- Update module docstring.

Run `uv run pytest` — still green.

### Step 6 — update wiki and docs

```bash
python3 scripts/extract_config_docs.py --only reporter
```

Update `wiki/Helpers/Reporter.md`:
- Remove the "MQTT summary publishing" section.
- Update the feature list (item 6 "Optionally publishes summary statistics to
  MQTT" → remove or replace with a note that economic metrics are published by
  mimirheim core to the `last_solve` topic).
- Add migration note: users with `chart_publishing:` or `ha_discovery:` sections
  in `reporter.yaml` must remove those sections before upgrading; startup will
  fail with a Pydantic validation error otherwise.

### Step 7 — final test run

```bash
uv run pytest
```

All tests must pass. Move this file:

```bash
mv plans/64_reporter_mqtt_removal.md plans/done/
```

---

## Acceptance criteria

- [ ] `publish_last_solve_status` success payload includes `self_sufficiency_pct`,
      `grid_import_kwh`, `grid_export_kwh`.
- [ ] `ChartPublishingConfig` class does not exist anywhere in the codebase.
- [ ] `ReporterDiscoveryConfig` class does not exist anywhere in the codebase.
- [ ] `chart_publishing` and `ha_discovery` fields absent from `ReporterConfig`.
      A `reporter.yaml` containing either field is rejected at startup.
- [ ] `summary_publisher.py` does not exist.
- [ ] `ReporterDaemon` has no `_publish_summary_data()` or
      `_publish_summary_discovery()` method.
- [ ] `ReporterDaemon` does not call `self._client.publish(...)` anywhere.
- [ ] `ReporterDaemon` does not subscribe to `homeassistant/status`.
- [ ] `test_summary_publisher.py` deleted.
- [ ] `wiki/Reference/Config-Reporter.md` regenerated; contains no reference to
      `chart_publishing`, `summary_topic`, or `ha_discovery`.
- [ ] `wiki/Helpers/Reporter.md` contains migration note.
- [ ] All existing tests continue to pass.
- [ ] `uv run pytest` exits 0 with no new failures.
