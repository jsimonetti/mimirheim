# Plan 45 — mimirheim reporter: rendering library, analyse improvements, and event-driven reporting daemon

## Motivation

mimirheim produces a solve every 15 minutes but currently has no persistent,
browseable record of what it decided. The debug dump mechanism writes JSON to
disk but is tied to debug-level logging. The only consumer is
`scripts/analyse_dump.py`, which requires manual invocation and embeds its
rendering logic inline with no way to share it.

This plan delivers three tightly related things in a single coherent pass,
avoiding the write-then-refactor waste that would result from implementing them
as separate plans:

1. **`reporter/render.py` — rendering library written once, in the right place.**
   All rendering improvements (grid exchange visibility, summary panel,
   closed-loop shading, flag columns, row colouring, shared plotly.js) are
   implemented directly in `mimirheim_helpers/reporter/reporter/render.py`. There is
   nothing to extract later.

2. **`scripts/analyse_dump.py` — updated to import from `render.py`.**
   The inline rendering code in the script is replaced with imports from the
   reporter package. The script retains its own CLI logic, file-finding, and
   batch modes (`--all`, `--last N`).

3. **`mimirheim-reporter` daemon + mimirheim `reporting:` config.**
   A new `mimirheim_helpers/reporter/` tool that subscribes to a dump-available MQTT
   notification, reads dump files from a shared filesystem path, renders one HTML
   report per solve, maintains a browseable `index.html` with an `inventory.js`
   sidecar, and garbage-collects old reports. A new `reporting:` config section
   in Mimirheim — independent of `debug:` — enables dump writing and the notification
   at any log level.

---

## Critical design decisions

### The rendering library is the primary artifact

The daemon and the analysis script are both consumers of `render.py`. Writing
the rendering improvements there first means both consumers get them
simultaneously and no code is duplicated.

### Dumps stay on the filesystem; MQTT carries only a notification

A dump file is 60–150 KB. Publishing it as a retained MQTT message is unsafe:
most self-hosted brokers (Mosquitto on a Raspberry Pi) impose per-message limits
well below that. mimirheim writes dump files to disk. The MQTT notification carries
only a JSON pointer (< 200 bytes) with the file paths. The reporter reads the
files directly via a shared bind-mount volume.

The dump files are a durable, always-on artifact regardless of whether the
reporter is running.

### `reporting:` is decoupled from `debug:`

`debug.enabled` controls log verbosity and debug-purpose dump writing for
`analyse_dump.py`. It is not changed. A new `reporting.enabled` flag controls
reporting-purpose dump writing and the MQTT notification, independently of log
level. A production deployment runs `debug.enabled: false` (INFO logging, no
debug dumps) and `reporting.enabled: true` (reporting dumps + notification).

### The reporter is a `HelperDaemon` subclass

It follows the same scaffolding as `mimirheim_helpers/scheduler` and peers:
`HelperDaemon` from `helper-common`, Pydantic config, `__main__.py` entry
point, uv workspace member, pytest tests.

### The output is fully static HTML

No web server beyond the ability to serve static files is required. The UI
works offline, does not require a running daemon to view, and can be archived.

---

## Relevant source locations

```
mimirheim/config/schema.py               — add ReportingConfig, add field to MimirheimConfig
mimirheim/__main__.py                    — call reporting dump + publish notification
mimirheim/config/example.yaml            — add reporting: section
mimirheim_helpers/reporter/             — new tool (full layout below)
scripts/analyse_dump.py             — replace inline render with import from reporter
```

---

## Part A — mimirheim changes

### A1. `ReportingConfig` schema

Add to `mimirheim/config/schema.py` before `MimirheimConfig`:

```python
class ReportingConfig(BaseModel):
    """Configuration for the mimirheim reporting integration.

    Controls whether mimirheim writes reporting-purpose dump files and publishes
    a dump-available notification to MQTT after each successful solve.
    Independent of DebugConfig. Does not affect log verbosity.

    Attributes:
        enabled: When True, mimirheim writes a dump pair after every successful
            solve and publishes a notification to notify_topic.
        dump_dir: Directory to write dump files into. Required when enabled
            is True. May be the same path as debug.dump_dir.
        max_dumps: Maximum number of dump file pairs to retain. Older pairs
            are rotated out. 0 = unlimited.
        notify_topic: MQTT topic for the dump-available notification. The
            mimirheim-reporter daemon subscribes to this topic.
    """
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False)
    dump_dir: Path | None = Field(default=None)
    max_dumps: int = Field(default=200, ge=0)
    notify_topic: str = Field(default="mimir/status/dump_available")

    @model_validator(mode="after")
    def _dump_dir_required_when_enabled(self) -> "ReportingConfig":
        if self.enabled and self.dump_dir is None:
            raise ValueError(
                "reporting.dump_dir is required when reporting.enabled is True"
            )
        return self
```

Add `reporting: ReportingConfig = Field(default_factory=ReportingConfig)` to
`MimirheimConfig`.

### A2. Notification in `__main__.py`

Add a private function `_publish_reporting_notification` that:

1. Calls `debug_dump(bundle, result, config, config.reporting.dump_dir, config.reporting.max_dumps)`.
2. Publishes to `config.reporting.notify_topic` with QoS 0, **not retained**:
   ```json
   {
     "ts": "2026-04-02T14:00:00Z",
     "input_path": "/data/dumps/2026-04-02T14-00-00Z_input.json",
     "output_path": "/data/dumps/2026-04-02T14-00-00Z_output.json"
   }
   ```

Call it from the solve loop after the existing `debug_dump` call:

```python
if result is not None and config.reporting.enabled:
    _publish_reporting_notification(bundle, result, config, mqtt_client)
```

Not retained: a retained message would cause the reporter to re-process the
last dump on every reconnect. The reporter handles missed notifications via
filesystem catch-up on startup (see Part B, Daemon startup).

Not published on infeasible solves: there is no output file to render.

### A3. `example.yaml` addition

New `reporting:` section between `debug:` and `readiness:`:

```yaml
# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
# Controls whether mimirheim writes dump files for the mimirheim-reporter daemon and
# publishes a dump-available notification. Independent of debug logging.
#
# Set enabled: true in production to run alongside mimirheim-reporter.
# dump_dir must be a volume accessible to both the mimirheim and reporter containers.

reporting:
  enabled: false
  dump_dir: /data/dumps     # required when enabled is true
  max_dumps: 200             # dump file pairs to retain; 0 = unlimited
  notify_topic: "mimir/status/dump_available"
```

### A4. Tests

**`tests/unit/test_config_schema.py`** (append):
- `test_reporting_config_defaults` — enabled=False, dump_dir=None, max_dumps=200
- `test_reporting_config_dir_required_when_enabled` — enabled=True, no dump_dir → ValidationError
- `test_reporting_config_enabled_with_dir` — enabled=True + dump_dir → valid
- `test_reporting_config_extra_field_rejected`
- `test_hioo_config_reporting_field_defaults`

**`tests/unit/test_main_reporting_notification.py`** (new file):
- `test_notification_published_after_successful_solve`
- `test_notification_not_published_when_reporting_disabled`
- `test_notification_not_published_on_infeasible`

---

## Part B — `reporter/render.py` (rendering library)

This is the primary implementation work in the plan.

### Package location

`mimirheim_helpers/reporter/reporter/render.py`

This module is the single source of truth for all HTML rendering. It imports
only `plotly` and the Python standard library. It has no imports from `mimirheim`.

### Public API

```python
def build_combined_figure(inp: dict, out: dict) -> go.Figure:
    """Build a single Plotly Figure from a parsed dump pair.

    Layout (rows from top to bottom):
        Row 1 (2 cols): economic summary table | exchange + self-sufficiency table
        Row 2: Unoptimised energy-flow stacked bars (naive baseline)
        Row 3: Optimised energy-flow stacked bars + grid import/export bars
               + net exchange line overlay
        Rows 4..N: One row per dispatchable device — SOC curve + price lines
                   + closed-loop shading bands
        Last row: Step-by-step data table with ZEX/LB flag columns and
                  economic-state row colouring
    """
```

All other functions in the module are private.

### R1 — Grid import/export bars + net exchange line (was Plan 45 Improvement 1)

In `_build_energy_flows_traces`, add to the *optimised* chart only:

- `Grid import` bar trace: `grid_import_kw * STEP_HOURS`, colour `#cc3300`,
  positive (consumption side).
- `Grid export` bar trace: `-(grid_export_kw * STEP_HOURS)`, colour `#33aa44`,
  negative (generation side).
- Net exchange overlay: `go.Scatter` line, `net_kw = grid_import_kw - grid_export_kw`
  per step, colour `#ff6600`, `mode="lines"`, not in legend.

The naive chart receives no grid bars: with no storage dispatch the grid balance
is already fully determined by `PV - base_load`.

### R2 — Summary dashboard panel (was Plan 45 Improvement 2)

New helper `_build_summary_tables(inp, out, schedule) -> tuple[go.Table, go.Table]`.

**Left table — Economic summary** (9 rows):
Solve time, strategy, status, horizon (h + steps), naive cost (€), raw
optimised (€), SOC credit (€), effective cost (€, bold), saving vs naive (€, bold).

**Right table — Exchange and self-sufficiency** (per-device + totals):
Total import (kWh), total export (kWh), self-consumption (%), self-sufficiency
(%), dispatch suppressed, then one row per dispatchable device: `{name}:
charge X.XX kWh / discharge X.XX kWh`.

Metrics are computed from the schedule data at render time. No additional inputs
required.

The two tables occupy the first subplot row using `specs=[{"type":"table"},
{"type":"table"}]` with 2 columns. All subsequent rows use 1 column.

### R3 — Closed-loop shading in SOC chart rows (was Plan 45 Improvement 3)

New private helper `_closed_loop_shapes_and_annotations(schedule, name, xs, row_xref, row_ydomain)`.

For each device's SOC row, scan for truthy `zero_exchange_active`,
`zero_export_mode` (legacy), or `exchange_mode` values. For each contiguous run:

- `go.layout.Shape`: `type="rect"`, `xref=row_xref`, `yref="paper"`,
  `y0`/`y1` from the row's y domain, `fillcolor="rgba(34,85,204,0.10)"`,
  `layer="below"`, `line_width=0`.
- `go.layout.Annotation`: midpoint text `"ZEX"` at 5% from top.

EV `loadbalance_active=True` runs: purple tint `rgba(153,51,204,0.10)`,
label `"LB"`.

Shape x-coords are timestamp strings matching `xs`.

### R4 — Closed-loop flag columns in data table (was Plan 45 Improvement 4)

In `_build_data_table`, after per-device SOC columns, add for each device:

- `{name} ZEX`: `"yes"` / `"no"` / `"—"`. `"yes"` cells: `fill_color="#c8e6c9"`.
- `{name} LB`: same for `loadbalance_active`. `"yes"` cells: `fill_color="#e1bee7"`.

Columns only added when at least one step has a non-null value.

Per-column cell fill is the column fill array (one colour per row). Row-level
colouring (R5) is applied to base columns; flag columns use their own per-cell
colours and fall back to the row colour otherwise.

### R5 — Table row colour-coding (was Plan 45 Improvement 5)

Replace flat alternating fill with a 5-tier priority scheme per row:

1. Any device in closed-loop mode → `"#e8eaf6"` (light indigo)
2. Grid export > 0.05 kW → `"#e8f5e9"` (light green)
3. Grid import > 0.05 kW, price ≥ 75th-percentile → `"#fff3e0"` (light amber)
4. Grid import > 0.05 kW (otherwise) → `"#fce4ec"` (light pink)
5. Default → alternating `"#f9f9f9"` / `"#ffffff"`

The 75th-percentile import price threshold is computed from
`inp["horizon_prices"]` at render time.

### R6 — Shared plotly.js (was Plan 45 Improvement 7)

`build_combined_figure` does not call `write_html`. Callers do. The caller is
responsible for passing `include_plotlyjs="directory"` to `write_html`. This is
documented in the function docstring and enforced in both call sites:
`analyse_dump.py` and `reporter/daemon.py`.

---

## Part C — `scripts/analyse_dump.py` changes

### C1. Replace inline rendering with import

Remove all rendering functions from `analyse_dump.py`. Replace with:

```python
try:
    from reporter.render import build_combined_figure, _TZ_SCRIPT
except ImportError:
    sys.exit(
        "The mimirheim-reporter package is required.\n"
        "Run: uv sync\n"
        "Then retry from the mimirheim workspace root."
    )
```

All CLI logic, file-finding helpers, `_load_json`, `_find_latest_pair`,
`_ts_from_path`, and the `write_html` call remain in `analyse_dump.py`.

### C2. Batch CLI modes (`--all`, `--last N`)

Add two new mutually exclusive CLI flags:

- `--all` — process every dump pair found in `--dir`, sorted oldest-first, one
  HTML per pair.
- `--last N` (int ≥ 1) — process the N most recent pairs. `--last 1` is the
  current default when `--dir` is used.

Both flags require `--dir`. Both are mutually exclusive with positional
`INPUT_JSON OUTPUT_JSON` arguments.

All output files in a single invocation go to the same `--output-dir`, so only
one `plotly.min.js` is written regardless of N.

Progress is printed per file: `[2/12] 2026-04-02T14:00:00Z → analysis.html`.

---

## Part D — `mimirheim-reporter` daemon

### D1. Package layout

```
mimirheim_helpers/reporter/
  pyproject.toml
  example.yaml
  reporter/
    __init__.py
    __main__.py       — entry point: load config, connect MQTT, run daemon
    config.py         — ReporterConfig Pydantic model
    daemon.py         — ReporterDaemon(HelperDaemon) subclass
    render.py         — rendering library (Part B)
    inventory.py      — manages inventory.js
    gc.py             — garbage collection
    static/
      index.html      — navigation page
      index.css       — minimal stylesheet
  tests/
    conftest.py       — copies fixture dumps from mimirheim_dumps/ into tests/fixtures/
    unit/
      test_config_schema.py
      test_inventory.py
      test_gc.py
    integration/
      test_render_against_fixture.py
```

### D2. `pyproject.toml`

```toml
[project]
name = "mimirheim-reporter"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "helper-common",
    "paho-mqtt>=2.0",
    "pydantic>=2.7",
    "pyyaml>=6.0",
    "plotly>=6.6.0",
]

[tool.uv.sources]
helper-common = { workspace = true }
```

Add `"mimirheim_helpers/reporter"` to the `[tool.uv.workspace] members` list in the
root `pyproject.toml`.

### D3. `ReporterConfig` schema (`reporter/config.py`)

```python
class ReporterReportingSection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dump_dir: Path              # shared volume path; required
    output_dir: Path            # where HTML reports + index.html are written; required
    max_reports: int = Field(default=100, ge=0)   # 0 = unlimited
    notify_topic: str = Field(default="mimir/status/dump_available")

class ReporterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mqtt: MqttConfig
    reporting: ReporterReportingSection
    homeassistant: HomeAssistantConfig = Field(default_factory=HomeAssistantConfig)
```

### D4. `ReporterDaemon` (`reporter/daemon.py`)

Subclasses `HelperDaemon`. `TOOL_NAME = "hioo_reporter"`.

Overrides `_on_connect` to subscribe to `config.reporting.notify_topic` in
addition to the trigger topic inherited from `HelperDaemon`.

`_run_cycle` is a no-op (the daemon is event-driven, not trigger-driven).

`_on_notification(client, userdata, message)` — called on every message to
the notification topic:

1. Parse JSON; validate `ts`, `input_path`, `output_path` keys. Log warning and
   return on failure.
2. Assert both files exist. Log warning and return if not.
3. Check `output_dir / f"{safe_ts}_report.html"` exists. If so, log DEBUG
   "already rendered, skipping" and return. This makes the handler idempotent.
4. Load both JSON files.
5. Call `render.build_combined_figure(inp, out)`.
6. Write HTML to `output_dir / f"{safe_ts}_report.html"` with
   `include_plotlyjs="directory"`.
7. Call `inventory.update(output_dir, ts, inp, out)`.
8. Call `gc.collect(output_dir, config.reporting.max_reports)`.

**Daemon startup catch-up:** before entering the message loop, the daemon scans
`dump_dir` for all `*_input.json` / `*_output.json` pairs, sorted newest-first,
and processes any pair without a corresponding `output_dir/{safe_ts}_report.html`.
After catch-up, `inventory.rebuild_from_disk(output_dir)` is called once to
ensure `inventory.js` reflects all reports that exist, then GC runs once.

`inventory.rebuild_from_disk` is more robust than incremental update for catch-up
because it handles the case where `inventory.js` was deleted or corrupted while
the daemon was down.

### D5. Inventory management (`reporter/inventory.py`)

`inventory.js` in `output_dir`:

```js
// Auto-generated by mimirheim-reporter. Do not edit manually.
window.MIMIRHEIM_REPORTS = [
  {
    "ts": "2026-04-02T14:00:00Z",
    "file": "2026-04-02T14-00-00Z_report.html",
    "strategy": "minimize_cost",
    "solve_status": "optimal",
    "horizon_steps": 112,
    "naive_cost_eur": 1.3255,
    "optimised_cost_eur": 0.3858,
    "soc_credit_eur": 1.0390,
    "effective_cost_eur": -0.6532,
    "saving_eur": 1.9787,
    "dispatch_suppressed": false,
    "total_import_kwh": 2.14,
    "total_export_kwh": 18.73,
    "self_sufficiency_pct": 94.2
  }
];
```

Sorted newest-first. All metrics pre-computed so `index.html` needs no
additional parsing.

Public functions:

- `update(output_dir, ts, inp, out)` — append/update one entry, rewrite file.
- `remove(output_dir, ts)` — remove entry by ts, rewrite file.
- `rebuild_from_disk(output_dir)` — scan `output_dir` for all `*_report.html`
  files, read the corresponding `dump_dir` JSON to compute metrics, rewrite from
  scratch. Used at daemon startup.

### D6. `index.html` (`reporter/static/index.html`)

A self-contained static page. Requirements:

1. Loads `inventory.js` via `<script src="inventory.js">`.
2. Summary banner: report count, date range, total saving across all retained
   solves, average self-sufficiency percentage.
3. Sortable, filterable table:
   - Columns: Time (local timezone via `Intl.DateTimeFormat`), Strategy, Horizon,
     Naive (€), Effective (€, colour-coded: green for negative/revenue, amber for
     positive, grey for suppressed), Saving (€), Import (kWh), Export (kWh),
     Self-suf. (%), link (opens report in new tab).
   - Filter bar: date-range pickers, strategy dropdown, checkbox to hide
     dispatch-suppressed rows.
4. Sparkline SVG (pure SVG, no library): saving-per-solve trend for the most
   recent 48 entries. Scale adapts to the min/max saving in the visible range.
5. Footer: "mimirheim-reporter | last updated {ts from newest entry}".

No JavaScript frameworks. Total inline script < 250 lines. Must work from
`file://` when `index.html` and `inventory.js` are in the same directory.

The reporter copies `index.html` from the package `static/` into `output_dir`
on first startup if it does not already exist. It never overwrites an existing
`index.html` (operators may customise it).

### D7. Garbage collection (`reporter/gc.py`)

`collect(output_dir: Path, max_reports: int) -> None`:

1. Read `inventory.js`.
2. If `max_reports == 0` or `len(entries) <= max_reports`, return.
3. For each entry beyond the limit (oldest first):
   - Delete the `.html` file.
   - Call `inventory.remove(output_dir, ts)`.
4. Log one INFO line: `"GC: removed N reports; M retained."`.

### D8. Tests

`conftest.py` copies one real dump pair from `mimirheim_dumps/` into
`tests/fixtures/` so tests are self-contained.

**`tests/unit/test_config_schema.py`:**
- `test_reporter_config_defaults`
- `test_reporter_config_dump_dir_required`
- `test_reporter_config_output_dir_required`
- `test_reporter_config_extra_field_rejected`
- `test_max_reports_ge_zero`

**`tests/unit/test_inventory.py`:**
- `test_update_creates_inventory_js`
- `test_update_appends_entry_sorted_newest_first`
- `test_update_replaces_duplicate_ts`
- `test_remove_deletes_entry`
- `test_rebuild_from_disk_empty_dir`

**`tests/unit/test_gc.py`:**
- `test_gc_no_op_when_below_limit`
- `test_gc_removes_oldest_beyond_limit`
- `test_gc_zero_limit_is_unlimited`
- `test_gc_deletes_html_files`

**`tests/integration/test_render_against_fixture.py`:**
- `test_render_produces_figure` — `build_combined_figure(inp, out)` returns a
  `go.Figure` with non-empty title.
- `test_write_html_produces_plotlyjs_sidecar` — `write_html` with
  `include_plotlyjs="directory"` writes both the `.html` and `plotly.min.js`.

---

## Part E — Docker

A `container/reporter.Dockerfile` alongside the existing `container/Dockerfile`.

Recommended Compose snippet:

```yaml
services:
  mimirheim:
    volumes:
      - dumps:/data/dumps

  reporter:
    build:
      context: .
      dockerfile: container/reporter.Dockerfile
    volumes:
      - dumps:/data/dumps      # read-only: dump files written by mimirheim
      - reports:/data/reports  # read-write: HTML reports + index.html
    command: ["python", "-m", "reporter", "--config", "/config/reporter.yaml"]

volumes:
  dumps:
  reports:
```

---

## Implementation sequence

1. **Part A** — `ReportingConfig` schema + notification (TDD: tests first).
2. **Part B** — `reporter/render.py` with rendering improvements R1–R6, using
   real dump fixtures to verify visually.
3. **Part C1** — Update `analyse_dump.py` to import from `reporter.render`.
   Verify identical output against a known dump. Then add C2 batch modes.
4. **Part D1–D3** — Package skeleton, `pyproject.toml`, config schema (tests first).
5. **Part D4–D5** — `daemon.py` + `inventory.py` (unit tests first).
6. **Part D6** — `index.html` + `index.css` (manual visual verification).
7. **Part D7–D8** — GC + integration tests.
8. **Part E** — `reporter.Dockerfile`.
9. **Part A3 verification** — full mimirheim test suite must remain green (467 tests).

---

## Acceptance criteria

### Part A

- `ReportingConfig` validates: `enabled=True` requires `dump_dir`; `enabled=False`
  with no `dump_dir` is valid; extra fields rejected.
- After a successful solve with `reporting.enabled: true`, exactly one notification
  message is published to `reporting.notify_topic` containing `ts`, `input_path`,
  `output_path`.
- No notification on infeasible solve. No notification when `reporting.enabled: false`.
- `debug.dump_dir` / `debug.enabled` behaviour unchanged.
- All 467 existing mimirheim tests remain green.

### Part B + C

- `build_combined_figure` produces a `go.Figure` containing:
  - A 2-column summary row at top.
  - Grid import (red) and export (green) bars in the optimised energy-flow chart.
  - Net exchange line overlay on the optimised chart.
  - Per-device SOC rows with closed-loop shading bands labelled `"ZEX"` or `"LB"`.
  - Data table with `ZEX` and `LB` flag columns (green/purple cell highlights).
  - Data table rows colour-coded by economic state (indigo/green/amber/pink/default).
- `analyse_dump.py --dir mimirheim_dumps` produces an HTML that renders correctly and
  references `plotly.min.js` via relative path (not inline).
- `analyse_dump.py --all` processes all dump pairs in the directory without error;
  only one `plotly.min.js` is written.
- `analyse_dump.py --last 3` processes the three most recent pairs.

### Part D

- Daemon starts, connects, subscribes to `notify_topic`, logs startup at INFO.
- On notification: writes `{ts}_report.html`, updates `inventory.js`, runs GC.
- Duplicate notification for an existing report: logs DEBUG "skipping", no
  overwrite.
- Startup catch-up generates all missing reports for pairs in `dump_dir`.
- `inventory.js` is valid JavaScript, sorted newest-first, one entry per report.
- `index.html` loads in a browser from `file://` without console errors.
- GC enforces `max_reports` limit; corresponding HTML files and inventory entries
  are removed.
- All unit and integration tests in `mimirheim_helpers/reporter/tests/` pass.
