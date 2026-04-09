# Step 40 — Config editor completion: remaining devices and helper tabs

## Purpose

This step extends the config editor from step 39 to cover all remaining device
types and all helper service configurations. It is entirely mechanical: every
device and helper is registered against the `DeviceListEditor` CRUD component
or a flat form renderer that was proven generic in step 39. No new component
architecture is introduced.

The step also adds helper-specific concerns: discovering which helper config
files are present, rendering an enable/disable toggle per helper, and handling
the three mutually exclusive baseload helper variants.

---

## References

- `plans/39_config_editor_service.md` — must be complete before this step starts
- `mimirheim/config/schema.py` — device model class names
- `mimirheim_helpers/*/config.py` — helper config module class names
- AGENTS.md — `extra="forbid"` rule, no bare `except`

---

## Remaining device tabs

The following table lists each tab to be added and the component/pattern it uses.
All of these are registrations against existing components — no new component code.

| Tab | Component | Schema ref | Config key |
|---|---|---|---|
| EV Chargers | `DeviceListEditor` | `EvConfig` | `ev_chargers` |
| Hybrid Inverters | `DeviceListEditor` | `HybridInverterConfig` | `hybrid_inverters` |
| Deferrable Loads | `DeviceListEditor` | `DeferrableLoadConfig` | `deferrable_loads` |
| Static Loads | `DeviceListEditor` | `StaticLoadConfig` | `static_loads` |
| Thermal Boilers | `DeviceListEditor` | `ThermalBoilerConfig` | `thermal_boilers` |
| Space Heating | `DeviceListEditor` | `SpaceHeatingConfig` | `space_heating` |
| Combi Heat Pumps | `DeviceListEditor` | `CombiHeatPumpConfig` | `combi_heat_pumps` |

Each requires one new `DeviceListEditor` instantiation in `app.js` and one
`registerTab` call. No changes to `DeviceListEditor` itself.

---

## Helper tabs

Each helper tab renders a flat form for a single YAML file. Helpers differ
from device tabs in two ways:

1. **Enable/disable toggle.** The service activates when its config file exists.
   Each helper tab shows a prominent "Enable / Disable" toggle at the top.
   When toggled off, the file is removed (after a confirmation step). When
   toggled on, a blank form is shown for the user to fill in and save.

2. **Separate config files.** Helper tabs read/write their own config files,
   not `mimirheim.yaml`. The API needs two new endpoints (see below).

### Helper tabs

| Tab | Config file | Top-level schema class |
|---|---|---|
| Nordpool | `nordpool.yaml` | `NordpoolConfig` |
| PV Forecast | `pv-fetcher.yaml` | `PvFetcherConfig` |
| PV ML Learner | `pv-ml-learner.yaml` | `PvLearnerConfig` |
| Baseload | `baseload-static.yaml` / `baseload-ha.yaml` / `baseload-ha-db.yaml` | See below |
| Reporter | `reporter.yaml` | `ReporterConfig` |
| Scheduler | `scheduler.yaml` | `SchedulerConfig` |

### Baseload tab — variant selector

The baseload tab is the one case requiring a small amount of conditional logic.
Three mutually exclusive config files exist for three variants of the baseload
helper. The tab renders a variant selector (`<select>`) first:

- Static profile
- Home Assistant REST API
- Home Assistant database

Selecting a variant shows the corresponding form and targets the correct config
file for save/delete. Only one baseload variant may be enabled at once: enabling
one automatically disables the others (deletes their config files). This rule
is displayed plainly in the UI and enforced in the POST handler.

---

## New API endpoints

### `GET /api/helper-configs`

Returns a JSON dict mapping each known helper config filename to its current
state:

```json
{
  "nordpool.yaml":           {"enabled": true,  "config": { ... }},
  "pv-fetcher.yaml":         {"enabled": false, "config": {}},
  "pv-ml-learner.yaml":      {"enabled": false, "config": {}},
  "baseload-static.yaml":    {"enabled": true,  "config": { ... }},
  "baseload-ha.yaml":        {"enabled": false, "config": {}},
  "baseload-ha-db.yaml":     {"enabled": false, "config": {}},
  "reporter.yaml":           {"enabled": false, "config": {}},
  "scheduler.yaml":          {"enabled": true,  "config": { ... }}
}
```

### `POST /api/helper-config/<filename>`

Accepts `application/json` body with either:
- `{"enabled": false}` — delete the config file (after confirming it exists)
- `{"enabled": true, "config": { ... }}` — validate via the appropriate Pydantic
  model, write atomically to `{config_dir}/<filename>`

Returns `{"ok": true}` or `{"ok": false, "errors": [...]}`.

The `<filename>` parameter is validated against a hardcoded allowlist of the
eight known helper filenames. Any other value returns 400. This prevents path
traversal via the filename parameter.

### `GET /api/helper-schemas`

Returns a JSON dict mapping each helper filename to its schema:

```json
{
  "nordpool.yaml":      { ...NordpoolConfig.model_json_schema()... },
  "pv-fetcher.yaml":    { ...PvFetcherConfig.model_json_schema()... },
  ...
}
```

Computed once at startup and cached.

---

## Files to modify

- `mimirheim_helpers/config_editor/server.py` — new `/api/helper-configs`,
  `/api/helper-config/<filename>`, `/api/helper-schemas` endpoints; allowlist
  for helper filenames; per-helper Pydantic model imports
- `mimirheim_helpers/config_editor/static/app.js` — new device tab registrations
  (EV, hybrid inverter, deferrable load, static load, heating devices); helper
  tab renderer; enable/disable toggle component; baseload variant selector
- `mimirheim_helpers/config_editor/static/index.html` — no structural changes
  expected; tab bar ordering update only
- `tests/unit/test_config_editor_server.py` — new tests for helper endpoints
- `wiki/Helpers/Config-Editor.md` — extend with all device tabs and helper tabs
- `wiki/Quick-Start.md` — add a note directing newcomers to the config editor
  as an alternative to hand-editing YAML

Note: helper schema annotations (`ui_label`, `ui_group`, etc.) and their
coverage tests were completed in step 38. No annotation work remains here.

---

## Tests

### `tests/unit/test_config_editor_server.py` additions

- `test_get_helper_configs_returns_all_known_helpers` — GET /api/helper-configs
  with an empty config dir returns all eight known helpers as `"enabled": false`.
- `test_get_helper_configs_enabled_when_file_present` — write a valid
  `nordpool.yaml` stub to the temp dir; assert `nordpool.yaml` has
  `"enabled": true` in the response.
- `test_post_helper_config_valid_writes_file` — POST `{"enabled": true, "config": {...}}`
  for `nordpool.yaml`; assert the file is written and contains valid YAML.
- `test_post_helper_config_disable_deletes_file` — write a `nordpool.yaml`,
  then POST `{"enabled": false}`; assert the file no longer exists.
- `test_post_helper_config_unknown_filename_returns_400` — POST to
  `/api/helper-config/../../etc/passwd`; assert 400.
- `test_post_helper_config_invalid_returns_422` — POST an invalid Nordpool
  config dict; assert 422 and errors list.
- `test_post_baseload_enable_disables_other_variants` — write `baseload-ha.yaml`,
  then POST to enable `baseload-static.yaml`; assert `baseload-ha.yaml` is deleted.

---

## Tab ordering in the UI

The final tab bar order, left to right:

1. General
2. Batteries
3. PV Arrays
4. EV Chargers
5. Hybrid Inverters
6. Deferrable Loads
7. Static Loads
8. Heating *(sub-tabs: Boiler, Space Heating, Combi Heat Pump)*
9. Nordpool
10. PV Forecast
11. PV ML Learner
12. Baseload
13. Reporter
14. Scheduler

The Heating group warrants sub-tabs because the three heating device types are
conceptually related and individually sparse (few fields each). All other groups
are flat tabs. This is the only place where sub-tabs are used.

## Wiki updates

### `wiki/Helpers/Config-Editor.md` extension

Extend the page created in step 39 with:

- The full tab list (all device types, all helper tabs) with a brief description
  of what each tab configures
- The enable/disable toggle behaviour for helper tabs: creating or deleting the
  helper config file, with a note about the corresponding s6 service needing a
  container restart to pick up changes
- The baseload variant selector: explain why only one variant can be active at
  a time, and what happens to the competing files when the user switches
- A table mapping each helper tab to its config file so users can cross-reference
  with the other `wiki/Helpers/` pages

### `wiki/Quick-Start.md` addition

After the existing Step 1 ("Create the directory layout"), add a short callout
box pointing newcomers to the config editor as an alternative to following the
manual YAML steps:

```
> **Easier option: use the config editor**
> If you do not want to write YAML by hand, enable the config editor service
> by creating an empty `config/config-editor.yaml` file. Open
> `http://<host>:8099` in a browser and the editor will guide you through
> all the steps below. You can return to this page for context on any field.
> See [Helpers/Config-Editor](Helpers/Config-Editor.md) for setup details.
```

This addition does not replace the existing YAML walkthrough — the manual steps
are still correct and remain in place. The callout is purely additive.

---

## Acceptance criteria

- All new tests in `test_config_editor_server.py` pass.
- `uv run pytest` (full suite) shows no regressions.
- A user can open the editor, navigate to the Nordpool tab, enable it by
  toggling on and filling in the area code, save, and have `nordpool.yaml`
  written correctly to the config directory.
- Enabling a baseload variant from the Baseload tab when another variant is
  already enabled deletes the old file without the user having to do so manually.
- The `/api/helper-config/<filename>` endpoint rejects any filename not in the
  hardcoded allowlist and returns 400, not 403 or 500.

---

## Commit

```bash
git add mimirheim_helpers/config_editor/ \
        tests/unit/test_config_editor_server.py \
        wiki/Helpers/Config-Editor.md \
        wiki/Quick-Start.md
git commit -m "feat: complete config editor with all device types and helper tabs

- Register all remaining device types against the generic DeviceListEditor
- Add helper config tabs (Nordpool, PV Forecast, PV ML Learner, Baseload,
  Reporter, Scheduler) with enable/disable toggle
- Baseload variant selector: enabling one variant deletes the others
- New helper API endpoints with hardcoded filename allowlist
- wiki/Helpers/Config-Editor.md extended with full tab reference and
  helper enable/disable behaviour
- wiki/Quick-Start.md: add config editor callout for newcomers
"
```
