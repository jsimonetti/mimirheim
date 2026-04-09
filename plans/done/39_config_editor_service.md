# Step 39 — Config editor service: foundation, CRUD component, batteries, PV proof

## Purpose

This step builds the config editor as an optional in-container web service.
It proves the two architectural claims that all of step 40 depends on:

1. The CRUD component (list + add + remove, form driven by schema annotations)
   is generic: it accepts a device type purely through configuration.
2. A second device type (PV arrays) can be added to the editor by registering
   it against the proven component, with no changes to component code.

The acceptance criterion for claim 2 is strictly enforced: PV array support
in this step must not require any changes to `app.js` beyond a new device
registration entry.

---

## References

- `plans/38_schema_ui_annotations.md` — must be complete before this step starts
- `mimirheim/config/schema.py`, `mimirheim/config/schema.json` — schema source
- `mimirheim_helpers/reporter/` — structural reference for a helper package
- `container/etc/s6-overlay/s6-rc.d/reporter/` — s6 service activation reference
- IMPLEMENTATION_DETAILS §6 — module boundary rules
- AGENTS.md — `extra="forbid"` rule, no bare `except`

---

## Architecture overview

```
[browser]
    │  GET /             → serves static/index.html
    │  GET /static/*     → serves static assets (app.js, style.css)
    │  GET /api/schema   → MimirheimConfig.model_json_schema() as JSON
    │  GET /api/config   → current /config/mimirheim.yaml as parsed JSON dict
    │  POST /api/config  → validate via Pydantic, write YAML, return result
    │
[ConfigEditorServer]  (stdlib http.server, no external framework)
    │
[/config/mimirheim.yaml]   (bind-mounted volume, must NOT be read-only)
```

### Why stdlib `http.server` rather than FastAPI

The editor is a simple static-file server plus three JSON endpoints. FastAPI
and uvicorn add ~50 MB of dependencies, a startup process, and an ASGI
abstraction layer for no benefit at this scale. The stdlib `http.server`
`BaseHTTPRequestHandler` handles the three endpoints cleanly. If the editor
grows to require async I/O or middleware, the decision should be revisited
with a proper plan step.

### No build step for the frontend

The frontend is a single `index.html` with one inline `<script>` block and
one linked `app.js` file. There is no bundler, no npm, no compiled artefact.
The only external dependency loaded from the browser is none — everything is
self-contained in the two static files. This constraint keeps the container
image size unchanged and eliminates CI build complexity.

---

## Files to create

```
mimirheim_helpers/config_editor/
    __init__.py
    __main__.py
    config.py         — Pydantic model for the editor's own YAML config
    server.py         — HTTP server: route dispatch, schema endpoint, config R/W
    static/
        index.html    — shell: tab bar, content pane, save button
        app.js        — rendering logic: schema reader, form builder, CRUD component
        style.css     — minimal styling, no framework

container/etc/s6-overlay/s6-rc.d/config-editor/
    type              — "longrun"
    run               — s6 run script (gated on /config/config-editor.yaml)

container/etc/s6-overlay/s6-rc.d/user/contents.d/
    config-editor     — empty file registering the service in the user bundle

tests/unit/test_config_editor_server.py
tests/unit/test_config_editor_crud_generic.py
```

---

## Files to modify

- `pyproject.toml` — new `config-editor` optional extra (`fastapi` is not used;
  no extra dependencies are required for the stdlib server)
- `container/Dockerfile` — expose port 8099, copy new service scripts, chmod
- `mimirheim_helpers/examples/config-editor.yaml` — example config for the service;
  must include a commented-out `port` override with an explicit note about host
  networking mode:
  ```yaml
  # Enable the mimirheim config editor web UI.
  # Access at: http://<host>:8099
  #
  # In host networking mode, change the port if 8099 conflicts with another service:
  # port: 8321
  #
  # All fields are optional. An empty file (or 'touch config-editor.yaml') is
  # sufficient to enable the editor on the default port.
  ```
- `wiki/Helpers/Config-Editor.md` — new wiki page (see below)

---

## Editor service config model (`config.py`)

The editor itself is configured by `/config/config-editor.yaml`. This file
gates the service (absent = disabled) and carries port and path config.

```python
class ConfigEditorConfig(BaseModel):
    """Configuration for the mimirheim config editor web service.

    Attributes:
        port: TCP port the editor listens on. Default 8099. Map this port
            in your container run command: -p 8099:8099.
        config_dir: Path to the directory containing mimirheim YAML config files.
            Default /config. This must be the same directory that is bind-mounted
            into the container.
        log_level: Logging level. Default INFO.
    """
    model_config = ConfigDict(extra="forbid")

    port: int = Field(default=8099, ge=1024, le=65535, description="TCP port to listen on.")
    config_dir: Path = Field(
        default=Path("/config"),
        description="Path to the config directory. Must match the container volume mount.",
    )
    log_level: str = Field(default="INFO", description="Logging level: DEBUG, INFO, WARNING.")
```

---

## HTTP API

### `GET /`
Returns `static/index.html` with `Content-Type: text/html`.

### `GET /static/<filename>`
Returns the requested file from the `static/` directory.
Only serves files with extensions `.js`, `.css`, `.html`. All other paths
return 404. Paths containing `..` return 403.

### `GET /api/schema`
Returns the full `MimirheimConfig.model_json_schema()` as `application/json`.
The schema is computed once at startup and cached. Callers do not need to
handle pagination or streaming — the schema fits comfortably in a single
response.

### `GET /api/config`
Reads the file at `{config_dir}/mimirheim.yaml`. If the file does not exist,
returns `{"exists": false, "config": {}}`. If it exists, parses the YAML,
returns `{"exists": true, "config": <parsed dict>}`.

Does not validate via Pydantic on the GET path — returns the raw parsed dict
so the frontend can display whatever the user last saved, including partially
complete configs.

### `POST /api/config`
Accepts `application/json` body: a dict representing the new config.

1. Attempt `MimirheimConfig.model_validate(body)`. If validation fails,
   return `{"ok": false, "errors": <list of Pydantic error dicts>}` with
   HTTP 422. Do not write any file.
2. If validation succeeds, serialise to YAML and write to
   `{config_dir}/mimirheim.yaml` atomically (write to a temp file in the
   same directory, then rename). Return `{"ok": true}` with HTTP 200.

The atomic write (temp + rename) prevents a partial file being visible to
the mimirheim solver if the container is restarted mid-write.

---

## Frontend architecture (`app.js`)

The frontend is structured around three concepts:

### Schema reader
Fetches `GET /api/schema` on page load and builds a lookup map:
`definition_name → {properties, required, ui_instance_name_description, ...}`.

### Form builder
Given a schema definition name and a data dict, renders a `<form>` element
with one input per field. Applies the following rendering rules:

| Field type | Rendered as |
|---|---|
| `string` | `<input type="text">` |
| `number` / `integer` | `<input type="number">` with `min`/`max` from schema |
| `boolean` | `<input type="checkbox">` |
| `string` enum | `<select>` |
| `null` or `anyOf` with null | adds a "not set" / blank option |
| `array` of objects | a sub-list with add/remove buttons (used for segments, stages) |

`ui_label` is used as the `<label>` text. `ui_unit` is appended in muted text
after the input. `ui_hint` is rendered as `<small>` help text below the input.

`ui_group` controls visibility: `"basic"` fields are always shown; `"advanced"`
fields are initially hidden behind a "Show advanced settings" toggle per device.

### CRUD component

The CRUD component manages a named-map device section (e.g. batteries). It
takes a single configuration object:

```js
const BatterysCrud = new DeviceListEditor({
    sectionKey: "batteries",          // key in the top-level config dict
    schemaRef: "BatteryConfig",       // $defs key in the schema
    tabLabel: "Batteries",
    newInstanceNamePlaceholder: "e.g. home_battery",
});
```

The component renders:
1. A list of existing instances with their names as labels.
2. An "Add battery" button + name input.
3. Clicking an instance shows its form in the right panel.
4. A remove button per instance (with a confirmation step).

The component does not know anything about batteries specifically. It reads the
schema by `schemaRef` and renders whatever fields the schema defines. Adding PV
array support requires only:

```js
const PvArraysCrud = new DeviceListEditor({
    sectionKey: "pv_arrays",
    schemaRef: "PvConfig",
    tabLabel: "PV Arrays",
    newInstanceNamePlaceholder: "e.g. roof_pv",
});
```

This is the genericity proof required by the acceptance criteria.

### Tab bar

Tabs are registered at startup:

```js
registerTab("General",    renderGeneralForm);
registerTab("Batteries",  BatterysCrud.render);
registerTab("PV Arrays",  PvArraysCrud.render);
```

The active tab label is stored in `location.hash` so browser back/forward works.

---

## s6 service (`container/etc/s6-overlay/s6-rc.d/config-editor/run`)

```sh
#!/bin/sh
exec 2>&1
CONFIG=/config/config-editor.yaml
if [ ! -f "$CONFIG" ]; then
    echo "config-editor: $CONFIG not found — service disabled"
    exec sleep infinity
fi
exec /app/.venv/bin/python -m config_editor --config "$CONFIG"
```

The service type file contains `longrun` (matching all other services).

---

## Tests

### `tests/unit/test_config_editor_server.py`

These tests instantiate `ConfigEditorServer` directly (no subprocess, no
actual file I/O) by passing a temp directory as `config_dir`.

- `test_get_schema_returns_mimirheim_schema` — GET /api/schema returns a dict
  with a `"title"` key equal to `"MimirheimConfig"`.
- `test_get_config_when_file_absent` — GET /api/config with no
  `mimirheim.yaml` present returns `{"exists": false, "config": {}}`.
- `test_get_config_returns_parsed_yaml` — write a minimal YAML to the temp dir,
  GET /api/config, assert that `config["grid"]["import_limit_kw"] == 25.0`.
- `test_post_config_valid_writes_file` — POST a valid config dict to
  /api/config, assert HTTP 200, assert `mimirheim.yaml` now exists in the temp
  dir and contains valid YAML.
- `test_post_config_invalid_returns_422` — POST a dict with
  `grid.import_limit_kw = -1.0`, assert HTTP 422 and response body contains
  `"errors"`.
- `test_post_config_atomic_write` — mock the rename syscall to raise
  `OSError` after the temp file is written; assert the original
  `mimirheim.yaml` is unchanged (i.e. no partial write is visible).
- `test_static_path_traversal_returns_403` — GET `/static/../config.py`
  returns 403.

### `tests/unit/test_config_editor_crud_generic.py`

These tests use Python's `http.server` in-process via `threading.Thread` to
start a real server on a random port against a temp config dir, then use
`urllib.request` to hit the real endpoints.

- `test_crud_battery_round_trip` — POST a config with one battery instance,
  GET it back, assert the battery instance is present with the correct name.
- `test_crud_pv_round_trip` — POST a config with one PV instance, GET it back,
  assert the PV instance is present. This test must pass purely by virtue of the
  generic CRUD path — no PV-specific server code should be required.
- `test_crud_add_second_battery` — POST a config with two battery instances.
  Assert both are returned on GET.
- `test_crud_field_validation_battery_capacity` — POST a battery with
  `capacity_kwh: 0` (fails `ge=0` is actually ok — use a string instead).
  Assert 422.

---

## Wiki page (`wiki/Helpers/Config-Editor.md`)

Create this page as part of the step 39 commit. It covers the core service
introduced here; step 40 will extend it with helper tabs.

The page must cover:

- What the config editor is and what problem it solves (one paragraph)
- Prerequisites: the `/config` volume mount must not be read-only
- How to enable: create `config-editor.yaml` (minimum: empty file)
- The `port` field and when to change it (host networking, port conflict)
- How to access the UI (`http://<host>:<port>`)
- The General, Batteries, and PV Arrays tabs available at this step
- A note that the editor validates config via Pydantic before writing — invalid
  configs are rejected with field-level error messages, not silently discarded
- A note that saving writes `mimirheim.yaml` atomically; the solver picks up
  the new config on its next trigger cycle without a container restart
- A security note: the editor has no authentication. It should only be
  accessible on a trusted private network, not exposed to the internet.

---

## Acceptance criteria

- All tests in `test_config_editor_server.py` and `test_config_editor_crud_generic.py`
  pass.
- `uv run pytest` (full suite) shows no regressions.
- A user who creates `/config/config-editor.yaml` and restarts the container
  can open `http://container-ip:8099` and:
  - See a General tab with the MQTT and grid fields rendered.
  - See a Batteries tab with an empty list and an "Add battery" button.
  - Add a battery instance, fill in basic fields, save, and have
    `mimirheim.yaml` written to the config directory.
  - See a PV Arrays tab that renders PV instances using the same CRUD component
    with no custom code.
- A user who does NOT create `config-editor.yaml` sees no change in container
  behaviour — the service sleeps harmlessly.
- The `app.js` file contains exactly one `DeviceListEditor` class definition.
  Adding the PV tab required only a new instantiation of that class, not any
  modification of the class itself. This can be verified by code review at PR
  time.

---

## Commit

```bash
git add mimirheim_helpers/config_editor/ \
        container/etc/s6-overlay/s6-rc.d/config-editor/ \
        container/etc/s6-overlay/s6-rc.d/user/contents.d/config-editor \
        container/Dockerfile \
        pyproject.toml \
        mimirheim_helpers/examples/config-editor.yaml \
        wiki/Helpers/Config-Editor.md \
        tests/unit/test_config_editor_server.py \
        tests/unit/test_config_editor_crud_generic.py
git commit -m "feat: add optional config editor web service

Adds a lightweight in-container web UI for editing mimirheim.yaml.
Activated by creating /config/config-editor.yaml; absent = disabled.

- stdlib HTTP server, no external framework dependency
- GET /api/schema serves annotated MimirheimConfig JSON Schema
- GET /api/config and POST /api/config for read/write with Pydantic validation
- Atomic YAML write (temp + rename) prevents partial file visibility
- Generic DeviceListEditor CRUD component driven by schema annotations
- Batteries and PV Arrays tabs prove component genericity
- Path traversal protection on static file serving
- wiki/Helpers/Config-Editor.md documents setup, port config, and security note
"
```
