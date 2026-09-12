# Step 71 (part 5 of 5) — HTTP service, real registry entries, and deployment

## Purpose

This step assembles every prior part into a running, independent process:
the registry (71_1) is populated with real mimirheim and helper
configuration models, the adapter pipeline (71_1, 71_2, 71_4) and the
validate-all/write-all save path (71_3) are wired behind HTTP endpoints, the
vendored Jedison/Bootstrap frontend (71_4) is served and drives a browser
form, and the service is packaged, containerised, and documented as its own
optional helper, independent of `mimirheim_helpers/config_editor/` (v1).

This is the only step that requires a running process for its tests and the
only step where config-editor-v2 becomes something a user can actually open
in a browser.

---

## References

- `mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md` — sections
  "Registry", "Deployment", "Non-goals" (re-read before wiring real entries —
  do not register a model that lives in a separate process or container)
- `mimirheim_helpers/config_editor/config_editor/server.py`, `__main__.py`
  (v1) — structural reference for the HTTP server, s6 service script, and
  Dockerfile wiring; do not import from v1
- `plans/71_1_registry_and_adapter_dispatch.md` through
  `plans/71_4_jedison_vendoring_and_hint_mapping.md` — must be complete first
- IMPLEMENTATION_DETAILS.md (root) §6, §7 — module boundary rules, if any
  apply to a helper importing mimirheim's own config schema

---

## Files to create

```
mimirheim_helpers/config_editor_v2/config_editor_v2/
    __main__.py
    config.py            — ConfigEditorV2Config: port, config_dir, log_level
    server.py            — HTTP routing, wires registry + adapter + save

mimirheim_helpers/config_editor_v2/static/
    index.html
    app.js               — fetches schema/data per entry, renders via Jedison,
                            submits back through the same adapter data-transform
                            path exercised in 71_1-71_3

container/etc/s6-overlay/s6-rc.d/config-editor-v2/
    type
    run

container/etc/s6-overlay/s6-rc.d/user/contents.d/
    config-editor-v2

tests/unit/test_server.py
tests/unit/test_registry_real_entries.py
```

## Files to modify

- `pyproject.toml` — new `config-editor-v2` extra; add the package to
  `[tool.hatch.build.targets.wheel]`, following the existing `config-editor`
  entry's pattern exactly.
- `container/Dockerfile` — expose the chosen port, copy the new service
  scripts, following the existing `config-editor` service's pattern.
- `mimirheim_helpers/examples/config-editor-v2.yaml` — example config,
  following the existing `config-editor.yaml` example's structure and its
  host-networking port-conflict note.
- `wiki/Helpers/Config-Editor-V2.md` — new wiki page (see below). Do not
  merge this into the existing `wiki/Helpers/Config-Editor.md`; v1 and v2 are
  documented separately since they are independent, separately deployable
  services, per IMPLEMENTATION_DETAILS.md's Purpose section.

---

## Registry entries

Populate `REGISTRY` (71_1) with real entries: `mimirheim.config.schema.MimirheimConfig`
plus every helper configuration model that is importable from this editor's
own Python environment (every `*Config` model under `mimirheim_helpers/*/`
that has a corresponding YAML file), per the Registry section of
IMPLEMENTATION_DETAILS.md and its Non-goal excluding models that live in a
separate process or container. Enumerate the full set against the current
`mimirheim_helpers/` tree at implementation time rather than against the
list in this plan, since new helpers may have been added since this plan was
written.

---

## `config.py`

Follows the same shape as v1's `ConfigEditorConfig` (port, config_dir,
log_level), as its own independent Pydantic model in this helper's own
package — do not import v1's `ConfigEditorConfig`.

---

## HTTP API

- `GET /` — serves `static/index.html`.
- `GET /static/vendor/*`, `GET /static/app.js`, etc. — static file serving
  with the same path-traversal protection as v1 (`_safe_join`-equivalent).
- `GET /api/entries` — returns the registry as JSON: name, filename, and the
  fully adapter-and-Jedison-transformed schema for each entry (chaining
  `adapter.transform_schema`, the `nullable-list` transform, and
  `jedison_mapping.to_jedison_schema` per field).
- `GET /api/entries/{name}/data` — returns the current on-disk data for one
  entry as a parsed dict, or its model's defaults if the file does not exist
  yet.
- `POST /api/save` — accepts a JSON body mapping entry name to submitted
  form data for every entry the frontend currently holds (not just the one
  being edited, since save semantics validate everything together). Runs
  each entry's data through `adapter.transform_incoming_data` and the
  `nullable-list` data transform, then `save.validate_all`. If the error
  list is non-empty, returns HTTP 422 with the `FieldError` list, grouped by
  `entry_name` so the frontend can route the user to the right tab and
  field. If empty, calls `save.write_all` and returns HTTP 200.

---

## Frontend (`app.js`)

- On load, calls `GET /api/entries`, builds one tab per entry.
- Selecting a tab calls `GET /api/entries/{name}/data` and renders a Jedison
  form from that entry's transformed schema and current data.
- Save triggers `POST /api/save` with every currently-loaded entry's current
  form data (fetch any not-yet-visited entry's data first so partial saves
  never omit an entry). On a 422 response, surface each `FieldError` under
  its `entry_name`'s tab, switching to that tab for the first error.

---

## s6 service

Mirrors v1's gating pattern: absent `/config/config-editor-v2.yaml` means
the service sleeps harmlessly; present means it starts. Use a distinct
default port from v1's 8099 to allow both editors to run simultaneously
during a migration period, since IMPLEMENTATION_DETAILS.md's Deployment
section explicitly allows more than one editor to run against the same
config directory at once. Confirm the exact default port with the user.

---

## Tests

### `tests/unit/test_registry_real_entries.py`

- `test_mimirheim_config_entry_resolves` — the real `MimirheimConfig`
  registry entry resolves via `registry.resolve_model`.
- `test_every_registered_entry_validates_defaults` — for every entry in
  `REGISTRY`, `save.validate_all` with no submitted data for that entry
  produces no error, proving the "untouched entry validates against
  defaults" requirement holds for every real registered model, not just the
  71_3 fixtures.

### `tests/unit/test_server.py`

In-process HTTP server tests, following v1's `test_config_editor_crud_generic.py`
pattern (real server on a random port, `urllib.request` against it):

- `test_get_entries_returns_registered_schemas` — `GET /api/entries`
  includes an entry for `MimirheimConfig` with a Jedison-shaped schema.
- `test_get_entry_data_when_file_absent_returns_defaults` — no on-disk file
  yet; response reflects the model's defaults.
- `test_post_save_all_valid_writes_every_file` — a full valid payload for
  every registered entry writes every file.
- `test_post_save_one_invalid_entry_writes_nothing` — mirrors the save
  semantics acceptance criterion directly: one entry invalid, assert HTTP
  422, assert no file on disk changed for any entry, including the ones that
  individually validated.
- `test_post_save_error_response_grouped_by_entry_name` — the 422 body's
  errors are grouped/attributable by `entry_name`, matching `FieldError`.
- `test_static_path_traversal_returns_403` — same protection as v1.

---

## Wiki page (`wiki/Helpers/Config-Editor-V2.md`)

Cover: what config-editor-v2 is and how it differs from v1 (schema-driven
via `x-mimir-` hints rather than v1's model-specific rendering, if that
remains true by this point — confirm against the final implementation
before writing this); prerequisites (writable `/config` mount); how to
enable; the port field and its default; that validation always goes through
the real Pydantic model, not the adapter's own schema; that a save is
all-or-nothing across every registered file; and the same no-authentication
security note v1 carries.

---

## Acceptance criteria

- All tests in `test_registry_real_entries.py` and `test_server.py` pass.
- `uv run pytest` (full suite) shows no regressions.
- `uv run ruff check .` is clean.
- A user who creates `/config/config-editor-v2.yaml` and restarts the
  container can open the service in a browser, see a tab per registered
  configuration file, edit fields, and save; an invalid submission on one
  tab blocks the save for every tab and reports the error against the
  correct tab and field.
- A user who does not create that file sees no change in container
  behaviour.
- Both config-editor-v2 and v1's config-editor can run at the same time
  without port conflict or shared state, if both are enabled.

---

## Commit

```bash
git add mimirheim_helpers/config_editor_v2/ \
        container/etc/s6-overlay/s6-rc.d/config-editor-v2/ \
        container/etc/s6-overlay/s6-rc.d/user/contents.d/config-editor-v2 \
        container/Dockerfile \
        pyproject.toml \
        mimirheim_helpers/examples/config-editor-v2.yaml \
        wiki/Helpers/Config-Editor-V2.md \
        tests/unit/test_server.py \
        tests/unit/test_registry_real_entries.py
git commit -m "feat(config-editor-v2): wire registry, adapter, and save into a running service

Completes config-editor-v2 as an independent, optional container
service: real mimirheim/helper config models registered, HTTP API
serving Jedison-shaped schemas and handling all-or-nothing saves,
vendored frontend assets served statically, s6-gated on
config-editor-v2.yaml presence.
"
```
