# config-editor — Web-based configuration editor

**config-editor** is a lightweight web interface for editing mimirheim and
helper YAML configuration files inside the running container. It serves a
single-page application that renders validated forms for every configuration
model, reads the current config files from `/config`, and writes changes back
atomically.

---

## Contents

1. [Purpose](#1-purpose)
2. [How it works](#2-how-it-works)
3. [Configuration](#3-configuration)
4. [HTTP API](#4-http-api)
5. [Running](#5-running)
6. [Security](#6-security)

---

## 1. Purpose

All other mimirheim services read their configuration from YAML files at
startup or on restart. config-editor provides a browser-based interface to
create and edit those files without needing shell access to the container or
a separate file editor add-on.

config-editor is the only component in the stack with no MQTT connection. It
is a web server that reads and writes files in the `/config` directory.

---

## 2. How it works

At startup, config-editor:

1. Loads `config-editor.yaml` and validates it against `ConfigEditorConfig`.
2. Starts a `ThreadingHTTPServer` on the configured port (default 8099).
3. Discovers every `*.schema.json` file: bundled ones shipped inside the
   config-editor package, plus any drop-in schema in `<config_dir>/schemas/`.
   No Python helper modules are imported to determine what schemas exist --
   each schema file carries its own `x-mimirheim` envelope naming the YAML
   file it edits and, for bundled schemas only, an optional dotted path to a
   second-pass Pydantic validator. Discovery runs once at startup and again,
   without restarting the process, on `POST /api/reload`. See `SPEC.md` §2
   for the full discovery and validation contract.
4. Serves the single-page editor frontend and JSON API endpoints until
   SIGTERM or SIGINT.

The frontend uses the JSON Schema for each config model to display a validated
form. Submitting the form sends the edited dict as a POST body; the server
validates it against the Pydantic model before writing the file, so invalid
configurations are rejected before they can break anything.

Config files are written atomically: the new content is first written to a
temporary file in the same directory, then renamed into place. This ensures
the previous config file is never partially overwritten.

### Comment preservation

config-editor uses **ruamel.yaml** for round-trip YAML editing, which preserves
comments, formatting, and key ordering when saving configuration files. This
means you can maintain inline documentation in your YAML files and continue
using the GUI editor without losing those comments.

**Example:**

Your `mimirheim.yaml` with comments:
```yaml
batteries:
  home_battery:
    capacity_kwh: 10.0  # Tesla Powerwall 2
    # Charge efficiency degrades above 0.8 SOC
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
```

After editing `capacity_kwh` to `12.0` in the GUI, your file becomes:
```yaml
batteries:
  home_battery:
    capacity_kwh: 12.0  # Tesla Powerwall 2
    # Charge efficiency degrades above 0.8 SOC
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
```

All comments are preserved, and only the edited value is updated.

**Limitations:**
- New devices or sections added via the GUI initially have no comments (you can
  add them manually afterward)
- Comments attached to deleted items are removed along with the item

---

## 3. Configuration

Create `config-editor.yaml` in `/config` (or pass any path with `--config`).
An empty file is valid and enables the editor with all defaults.

```yaml
# All fields are optional. Defaults are shown.

# TCP port the editor listens on.
# port: 8099

# Directory containing the mimirheim YAML config files.
# Must match the container /config bind-mount or addon_config path.
# config_dir: /config

# Python logging level: DEBUG, INFO, or WARNING.
# log_level: INFO
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `port` | int | `8099` | TCP port. Must be in range 1024–65535. |
| `config_dir` | path | `/config` | Directory where config files are read from and written to. |
| `log_level` | string | `INFO` | Python logging level name. |

### Drop-in schemas

`config_dir` may also contain a `schemas/` subdirectory
(`/config/schemas/*.schema.json` by default). Every file there is scanned
alongside the bundled schemas at startup and on `POST /api/reload`, using the
same discovery, validation, and rendering path -- a drop-in schema is not a
lesser citizen than a bundled one. An id already claimed by a bundled schema
is rejected (the bundled schema always wins); two drop-ins claiming the same
id are resolved in sorted filename order, first wins. A malformed drop-in is
recorded as a load-time problem (visible via `GET /api/registry`) and never
prevents another schema, bundled or drop-in, from loading. See `SPEC.md` for
the full schema file format and rejection rules.

### HA add-on: allowed_ip

When running as a HA add-on, the `CONFIG_EDITOR_ALLOWED_IP` environment
variable is injected automatically by the container init scripts. The server
uses this to restrict access to the HA ingress proxy IP (the container's
default gateway), preventing direct LAN access to the editor. This field
does not appear in `config-editor.yaml` and cannot be set manually.

---

## 4. HTTP API

The current API is registry-backed: every entry (`mimirheim.yaml` and every
helper, bundled or drop-in) is discovered and validated the same way -- see
`SPEC.md` §2-§9 for the full contract this table summarises.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Single-page editor frontend |
| `GET` | `/static/<file>` | Frontend static assets |
| `GET` | `/api/registry` | Every discovered entry: id, its `x-mimirheim` fields, `enabled` (file exists), and load-time rejection problems |
| `GET` | `/api/entry/<id>` | `{"schema": ..., "value": ..., "enabled": bool}` for one entry, rebuilt from disk on every call |
| `POST` | `/api/save` | Body `{"entries": {"<id>": {"enabled": true, "config": {...}} \| {"enabled": false}, ...}}`. Validate-all-then-write-all over the submitted entries. Returns `{"ok": true}` or `{"ok": false, "errors": {"<id>": [...]}}` |
| `POST` | `/api/preview` | Same request shape as `/api/save`. Returns `{"ok": true, "diffs": {"<filename>": "<unified diff>", ...}}` and writes nothing |
| `POST` | `/api/reload` | Re-runs discovery without restarting the process. Returns the same shape as `GET /api/registry` |

There is no deprecated API. The tool's original, hardcoded-helper-list
endpoints (`/api/schema`, `/api/config`, `/api/helper-configs`,
`/api/helper-schemas`, `/api/helper-config/<filename>`) have been removed.
`static/app.js` speaks the old shapes and is non-functional against this
server until plan 69's frontend rewrite replaces it -- expected, since the
config-editor rewrite does not ship until every plan in it has landed.

All responses are `application/json`. Status codes: 200 success, 400
malformed JSON or bad `Content-Length`, 403 disallowed IP, 404 unknown entry
id or path, 422 validation failure.

---

## 5. Running

### In a container (default)

config-editor starts automatically when `config-editor.yaml` is present in
`/config`. No additional steps are required.

### Standalone

```bash
uv run python -m config_editor --config /config/config-editor.yaml
```

Access the editor at `http://<host>:8099`.

---

## 6. Security

config-editor has no authentication. It is designed for use on a trusted
private network or inside a container behind the HA ingress proxy.

**Do not expose port 8099 directly to an untrusted network.** The editor
can read and write all config files in `/config` including MQTT passwords.

When running as a HA add-on, ingress provides HA session-based authentication
and the `allowed_ip` restriction further limits accepted connections to the
ingress proxy. Direct LAN access returns HTTP 403.
