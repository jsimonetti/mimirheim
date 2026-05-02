# Plan 57 — Config editor MQTT topic UX improvements

## Motivation

Setting up mimirheim with helpers requires coordinating MQTT topic names across
multiple YAML files. A user who follows the Quick Start guide must ensure that:

- The `mqtt.topic_prefix` in `mimirheim.yaml` matches the `mimir_topic_prefix`
  field in every enabled helper (`nordpool.yaml`, `pv-fetcher.yaml`,
  `baseload-*.yaml`, `reporter.yaml`).
- The device name used in `pv_arrays`, `static_loads`, or `ev_chargers` in
  `mimirheim.yaml` matches the corresponding key used in `pv-fetcher.yaml`,
  `pv-ml-learner.yaml`, or whichever baseload variant is enabled.
- Topic strings that are left empty (using the auto-derived defaults) are
  interpreted correctly by the user — the placeholder text in the UI currently
  shows `{mimir_topic_prefix}/input/pv/{array_key}/forecast`, which is a
  template, not the resolved value.

These friction points cause silent misconfigurations: mimirheim starts but
receives no data, and the user has no indication which topic is misconfigured.

This plan implements three independent improvements to the config editor GUI,
each addressable separately. All three are purely presentation-layer changes —
no change to Pydantic models, no change to MQTT logic, no change to the solver.

---

## Scope and non-goals

**In scope:**
- Frontend (`static/app.js`, `static/style.css`) — placeholder resolution,
  auto-propagation of prefix, and device name dropdowns.
- Helper config schemas for `ui_source` metadata — `json_schema_extra` additions
  only, no behavioural change to the Pydantic models.
- Unit tests for the server's schema endpoint that verify `ui_source` appears
  in the served schema (so the frontend's dependency is contractually tested).

**Not in scope:**
- Changes to `mimirheim.yaml` config schema (`MimirheimConfig`).
- Changes to helper runtime behaviour, MQTT publish logic, or topic derivation.
- Changes to the solver, core, or any non-config-editor module.
- Any new HTTP endpoints (all three improvements use existing GET/POST API).
- Scheduler: it uses fully user-defined topics and has no device coupling, so it
  has no `mimir_topic_prefix` field and is not affected by this plan.
- Config-editor itself: it has no MQTT topics.

---

## Current behaviour

### Problem 1 — Placeholders show templates, not values

Every topic field displays placeholder text such as:

```
{mimir_topic_prefix}/input/pv/{array_key}/forecast
```

The user sees a template. They cannot tell whether `{array_key}` means the
key they typed in the `arrays` dict above, or something else. When the field
is left empty, no hint confirms the auto-derived value.

### Problem 2 — `mimir_topic_prefix` is per-helper and siloed

Each helper has its own `mimir_topic_prefix: str = "mimir"` field hidden under
"Show advanced settings". If a user changes `mqtt.topic_prefix` in
`mimirheim.yaml` to `energy`, they must manually update `mimir_topic_prefix`
in every enabled helper. Because the field is hidden, most users will never
notice until topics break and the schedule becomes stale.

Affected helpers: nordpool, pv-fetcher, pv-ml-learner, baseload-static,
baseload-ha, baseload-ha-db, reporter.

### Problem 3 — Device names must match but no link is shown

PV helpers (pv-fetcher, pv-ml-learner) have per-array configs where the array
key (or `name` field) must match a key in `pv_arrays` in `mimirheim.yaml`.
All baseload helpers have `mimir_static_load_name` which must match a key in
`static_loads`. The UI shows a free-text field with no indication of what
values are valid.

---

## Relevant source locations

```
mimirheim_helpers/config_editor/config_editor/static/app.js
  — buildFieldRow()          renders a single field including label and input
  — buildFormSection()       groups fields into basic / advanced
  — renderHelperTab()        builds per-helper enable toggle + form
  — HELPER_FILE_TO_TITLE     maps filenames to display names

mimirheim_helpers/config_editor/config_editor/static/style.css
  — styles for form fields, placeholders, toggles

mimirheim_helpers/config_editor/config_editor/server.py
  — _api_get_config()        returns raw mimirheim.yaml as dict
  — _api_get_helper_configs() returns all helper configs in one call
  — _api_get_schema()        returns MimirheimConfig.model_json_schema()
  — _api_get_helper_schemas() returns per-helper model_json_schema() dicts

mimirheim_helpers/prices/nordpool/nordpool/config.py
  — NordpoolConfig           mimir_topic_prefix, output_topic, mimir_trigger_topic

mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py
  — PvFetcherConfig / ArrayConfig  mimir_topic_prefix, per-array output_topic

mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py
  — PvLearnerConfig / ArrayConfig  mimir_topic_prefix, per-array output_topic, name field

mimirheim_helpers/baseload/static/baseload_static/config.py
mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py
mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py
  — BaseloadConfig (all three)  mimir_topic_prefix, mimir_static_load_name, output_topic

mimirheim_helpers/reporter/reporter/config.py
  — ReporterConfig           mimir_topic_prefix, notify_topic

tests/unit/test_config_editor_server.py
  — Existing tests for GET/POST config and helper config endpoints
```

---

## Design decisions

### D1. Phase 1: Resolve placeholders to real values in the frontend

When rendering a topic field that is empty, compute the resolved topic string
client-side and display it as a small hint below the input element:

```
Auto-derived: mimir/input/pv/roof_pv/forecast
```

This requires `app.js` to know:
1. The current `mqtt.topic_prefix` from the loaded mimirheim config.
2. The current device name in context (e.g. the key of the array being rendered).

Both are already present in memory at render time. No new API call is needed.

**Implementation in `app.js`:**

```javascript
/**
 * Resolve a ui_placeholder template to a concrete topic string.
 *
 * Supported substitutions:
 *   {mimir_topic_prefix} / {mqtt.topic_prefix}  — top-level prefix
 *   {array_key} / {name}                         — device name from context
 *   {mimir_static_load_name}                     — static load device name
 *   {mimir_array_key}                            — PV array device name
 *
 * Returns null if not all substitutions can be resolved.
 */
function resolvePlaceholder(template, ctx) { ... }
```

A `ctx` object is threaded through form-building calls, carrying:
- `prefix`: resolved from `mimirheimConfig.mqtt?.topic_prefix ?? "mimir"`
- `deviceName`: the current map key or `name` field (when inside a per-device section)

The hint element is only shown when the field is empty. When the user types in
the field, the hint disappears (replaced by the user's custom value).

**Styling:** A `<span class="topic-hint">` displayed beneath the input in grey,
font-size 0.85em. CSS class already follows the existing `field-hint` pattern.

### D2. Phase 2: Auto-propagate `mimir_topic_prefix` across helpers

When the user changes `mqtt.topic_prefix` in `mimirheim.yaml`:

1. The frontend detects the change (via `input` event on that field).
2. It compares the new prefix to each enabled helper's `mimir_topic_prefix`.
3. For any helper where `mimir_topic_prefix` still equals the **old** prefix
   (i.e., it was tracking the mimirheim prefix, not customised independently),
   it updates the helper's in-memory config to the new prefix value.
4. A banner is shown: "Updated topic prefix in N helpers. Save each helper to
   persist." This makes the sync visible and opt-out-able — users who reload
   without saving keep their old config.

**Power user opt-out:** If a helper's `mimir_topic_prefix` differs from
mimirheim's `mqtt.topic_prefix`, it is treated as intentionally customised and
is **not** updated automatically. The banner names the helpers that were
skipped so the user knows which ones diverge.

**No new API endpoints.** The updated values are persisted on the next POST to
each helper's endpoint (when the user saves).

### D3. Phase 3: Device name dropdowns for coupled fields

Fields that must match a mimirheim device name are replaced with a `<select>`
dropdown when the corresponding section in `mimirheim.yaml` is non-empty.

**Mechanism:**

Add `"ui_source": "<section_name>"` to `json_schema_extra` on the relevant
fields in each helper config schema. `<section_name>` is the key in
`MimirheimConfig` that holds the device map:

| Helper field | `ui_source` value |
|---|---|
| `baseload-static.mimir_static_load_name` | `"static_loads"` |
| `baseload-ha.mimir_static_load_name` | `"static_loads"` |
| `baseload-ha-db.mimir_static_load_name` | `"static_loads"` |
| `pv-fetcher.arrays` (dict keys) | `"pv_arrays"` |
| `pv-ml-learner.arrays[*].name` | `"pv_arrays"` |

In `buildFieldRow()`, when `fieldSchema.ui_source` is set:
- Read `mimirheimConfig[fieldSchema.ui_source]` to get the device map.
- If the map is non-empty, render a `<select>` element populated with its keys.
  Append an "Other (type manually)" option that reveals a text input for
  power-user overrides.
- If the map is empty (user has not yet configured that section in
  `mimirheim.yaml`), fall back to a plain text input with a tooltip: "Add
  devices to mimirheim.yaml first to see a dropdown here."

**For pv-fetcher / pv-ml-learner (dict-keyed arrays):**

These helpers use a dict where the key itself is the array name (for
pv-fetcher) or a `name` field inside each list item (for pv-ml-learner). The
`ui_source` is applied at the section level: when the user adds a new array
entry, the UI offers a dropdown for the key/name field rather than a blank text
input.

**`ui_source` is read-only metadata.** It does not change validation or the
Pydantic model — `extra="forbid"` is already on all models, and `ui_source` is
stored in `json_schema_extra`, which Pydantic does not validate.

### D4. Test coverage strategy

Phase 1 and 2 are pure JavaScript changes — they are not tested by the Python
test suite. Testing these requires a browser or a JS test runner, neither of
which is in scope for this codebase. Acceptance is by manual verification
(see acceptance criteria below).

Phase 3 adds `ui_source` to Python schema `json_schema_extra`. The existing
test `test_helper_schema_endpoint()` in `test_config_editor_server.py` tests
that each helper's JSON schema is returned by the server. Extend this test to
assert that the `ui_source` field appears on the expected properties in the
schema response. This gives a regression guard that the metadata survives Pydantic
serialisation.

Write the tests first (they will fail because `ui_source` is not yet in the
schemas), then add the metadata.

### D5. Ordering of phases

Implement in this order:

1. Phase 3 first (schema metadata + tests) — Python-only, verifiable with
   `uv run pytest`, no browser needed. Establishes the contract.
2. Phase 1 second (placeholder resolution) — Pure JS. Lowest risk; purely
   additive.
3. Phase 2 last (prefix propagation) — JS but modifies in-memory config state,
   so more care is needed.

---

## TDD workflow

### Phase 3 — Schema `ui_source` metadata

**Step 3a — Write failing tests**

In `tests/unit/test_config_editor_server.py`, add:

```python
class TestHelperSchemaUiSource:
    def test_baseload_static_static_load_name_has_ui_source(self, server):
        _, _, body = server._api_get_helper_schemas()
        schemas = json.loads(body)
        props = schemas["baseload-static.yaml"]["$defs"]["BaseloadConfig"]["properties"]
        assert props["mimir_static_load_name"]["ui_source"] == "static_loads"

    def test_baseload_ha_static_load_name_has_ui_source(self, server): ...
    def test_baseload_ha_db_static_load_name_has_ui_source(self, server): ...

    def test_pv_fetcher_array_key_has_ui_source(self, server):
        # pv-fetcher arrays dict — ui_source on the value schema (ArrayConfig)
        schemas = json.loads(body)
        array_config_props = schemas["pv-fetcher.yaml"]["$defs"]["ArrayConfig"]["properties"]
        # or wherever the schema nests the array entry schema
        assert array_config_props["output_topic"].get("ui_source") == "pv_arrays"
        # (exact path depends on how Pydantic serialises the dict-of-model schema)

    def test_pv_ml_learner_array_name_has_ui_source(self, server):
        schemas = json.loads(body)
        # pv-ml-learner arrays is a list of ArrayConfig, each with a 'name' field
        array_config_props = schemas["pv-ml-learner.yaml"]["$defs"]["ArrayConfig"]["properties"]
        assert array_config_props["name"]["ui_source"] == "pv_arrays"
```

Run `uv run pytest tests/unit/test_config_editor_server.py -k ui_source` —
confirm all fail (KeyError or AssertionError on missing `ui_source`).

**Step 3b — Add `ui_source` to helper schemas**

Edit each helper config model's relevant field to add `"ui_source"` to
`json_schema_extra`:

```python
# baseload-static, baseload-ha, baseload-ha-db (identical change in all three)
mimir_static_load_name: str = Field(
    default="base_load",
    description="...",
    json_schema_extra={
        "ui_label": "mimirheim static load name",
        "ui_group": "advanced",
        "ui_source": "static_loads",   # <-- new
    },
)
```

```python
# pv-fetcher: ArrayConfig.output_topic
output_topic: str | None = Field(
    default=None,
    description="...",
    json_schema_extra={
        "ui_placeholder": "{mimir_topic_prefix}/input/pv/{array_key}/forecast",
        "ui_source": "pv_arrays",   # <-- new
    },
)
```

```python
# pv-ml-learner: ArrayConfig.name
name: str = Field(
    description="...",
    json_schema_extra={
        "ui_label": "Array name",
        "ui_source": "pv_arrays",   # <-- new
    },
)
```

Run `uv run pytest tests/unit/test_config_editor_server.py -k ui_source` —
confirm all pass.

Run full suite: `uv run pytest` — confirm no regressions.

### Phase 1 — Placeholder resolution (JS, manual verification)

No Python tests. Acceptance is manual:

1. Start config editor with a populated `mimirheim.yaml` containing
   `mqtt.topic_prefix: energy` and a PV array `solar`.
2. Open pv-fetcher helper tab. Leave the array's output topic blank.
3. Verify hint reads: **"Auto-derived: energy/input/pv/solar/forecast"**.
4. Type a custom value in the field. Verify hint disappears.
5. Clear the field. Verify hint reappears.

### Phase 2 — Prefix propagation (JS, manual verification)

No Python tests. Acceptance is manual:

1. Start with `mqtt.topic_prefix: mimir` and nordpool + pv-fetcher enabled,
   both with `mimir_topic_prefix: mimir`.
2. Change `mqtt.topic_prefix` to `energy` in the mimirheim.yaml form.
3. Verify banner appears: "Updated topic prefix in nordpool, pv-fetcher. Save
   each helper to persist."
4. Verify nordpool helper now shows `mimir_topic_prefix: energy` in the form.
5. Manually change nordpool's `mimir_topic_prefix` to `custom` before step 2.
6. Repeat step 2. Verify nordpool is excluded from the banner update and named
   in the "skipped" list.

---

## Files to create or edit

### Phase 3 (Python)

| File | Change |
|---|---|
| `mimirheim_helpers/baseload/static/baseload_static/config.py` | Add `"ui_source": "static_loads"` to `mimir_static_load_name` field |
| `mimirheim_helpers/baseload/homeassistant/baseload_ha/config.py` | Same |
| `mimirheim_helpers/baseload/homeassistant_db/baseload_ha_db/config.py` | Same |
| `mimirheim_helpers/pv/forecast.solar/pv_fetcher/config.py` | Add `"ui_source": "pv_arrays"` to `ArrayConfig.output_topic` (or array key field — check actual schema shape) |
| `mimirheim_helpers/pv/pv_ml_learner/pv_ml_learner/config.py` | Add `"ui_source": "pv_arrays"` to `ArrayConfig.name` field |
| `tests/unit/test_config_editor_server.py` | Add `TestHelperSchemaUiSource` class (write before schema edits) |

### Phase 1 (JavaScript)

| File | Change |
|---|---|
| `mimirheim_helpers/config_editor/config_editor/static/app.js` | Add `resolvePlaceholder(template, ctx)` function; thread `ctx` through `buildFieldRow` and per-device section builders; append `<span class="topic-hint">` to topic inputs |
| `mimirheim_helpers/config_editor/config_editor/static/style.css` | Add `.topic-hint` rule (grey, 0.85em) |

### Phase 2 (JavaScript)

| File | Change |
|---|---|
| `mimirheim_helpers/config_editor/config_editor/static/app.js` | Add prefix-change listener on `mqtt.topic_prefix` field; add `syncHelperPrefixes(oldPrefix, newPrefix)` function; add sync banner element and show/hide logic |

---

## Acceptance criteria

### Phase 3

- [ ] `uv run pytest tests/unit/test_config_editor_server.py -k ui_source` passes
  with one test per affected field (at minimum: 3 baseload + 2 PV helpers = 5 tests).
- [ ] `uv run pytest` passes with no regressions.
- [ ] `ui_source` does not appear in any Pydantic validation path — confirm by
  verifying all existing `extra="forbid"` tests still pass.

### Phase 1

- [ ] Empty topic field shows "Auto-derived: <resolved topic>" below the input.
- [ ] Hint disappears when user types in the field; reappears when field is cleared.
- [ ] Hint correctly reflects changes to `mqtt.topic_prefix` or device name in
  the same form session without requiring a page reload.
- [ ] No hint shown when a topic field already has an explicit value.

### Phase 2

- [ ] Changing `mqtt.topic_prefix` triggers an in-memory update of
  `mimir_topic_prefix` in all helpers where it matched the old prefix.
- [ ] A visible banner lists updated and skipped helpers.
- [ ] Helpers with a custom (diverged) `mimir_topic_prefix` are not overwritten.
- [ ] The update is not persisted until the user saves each helper individually.
- [ ] Reloading the page without saving discards the in-memory update.
