# config-editor — Agent Instructions

This tool is **not independent of mimirheim**. It is packaged as part of the
root mimirheim wheel and has no separate `pyproject.toml`. Its bundled
schemas are generated from `mimirheim.config.schema` and every helper config
module (`scripts/generate_schema_json.py`), but the running server itself
discovers schemas from files, not from those imports -- see the next section.

---

## Critical architectural distinction

config-editor is not an input tool. It has no MQTT connections, no trigger
topics, and no daemon cycle. It is a web interface that reads and writes the
YAML configuration files in `/config`. It discovers every entry's JSON Schema
from `*.schema.json` files (bundled, generated ahead of time from mimirheim's
and every helper's Pydantic models; or drop-in, user-supplied) rather than
importing helper config modules directly at request time, so the front end
can render a validated editing form for any entry, installed or not.

`config_editor/registry.py` is the one runtime module that imports a
helper's Pydantic model directly, and only for two narrow reasons: a bundled
entry's optional second-pass validation (`x-mimirheim.python_model`, SPEC.md
§7), and `server.py`'s not-yet-enabled entries' default values. Neither
happens for a drop-in schema.

---

## Environment setup

config-editor runs inside the root mimirheim virtual environment. All commands
must be run from the repository root, not from this directory.

```bash
cd /path/to/hioo                             # repository root

uv sync --all-extras                         # install all dependencies including config-editor
uv run pytest mimirheim_helpers/config_editor/tests   # run tests for this module only
uv run pytest                                # run the full test suite
uv run python -m config_editor --config config-editor.yaml   # run the editor
```

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, HTTP API contract, configuration schema, **as currently implemented**.
- `SPEC.md` in this directory — the schema contract: schema file format, discovery, field vocabulary, document composition, cross-references, validation, save semantics, security, and HTTP API. §2 (Discovery), §3 (`x-mimirheim` envelope), §4 (Field vocabulary, including its Jedison-rendering half -- `x-format`, `x-enumSource`, `x-watch`/`x-template`, and the `mimir-topic-placeholder` custom editor), §5 (Document composition), §6 (Cross-references), §7 (Validation model), §8 (Save semantics), §9 (Enabled state), §10 (Rejection rules), and §11 (Security, including the CSP header) are **current behaviour**, implemented by `registry.py` and `server.py` (plan 68) and `static/app.js`, `static/topic-placeholder-editor.js`, and `server.py`'s CSP header (plan 69). §6's drop-in authoring CLI is still plan 70's job. Where `SPEC.md` and the current implementation disagree outside that one still-pending area, treat that as a bug, not an expected gap.
- `IMPLEMENTATION_DETAILS.md` in this directory — the Jedison library integration: exact API behaviour, required setup that is easy to get silently wrong, and the reasoning behind the per-entry-document architecture `SPEC.md` specifies without justifying. Relevant only to this directory's frontend; Jedison is not used anywhere else in the repo.
- `IMPLEMENTATION_DETAILS.md` in the repo root — Pydantic conventions, docstring format, code standards.

---

## Code standards

Apply all mimirheim code standards from the root `AGENTS.md` to this module
without exception:

- All public functions and methods must have complete type annotations.
- All Pydantic models must set `model_config = ConfigDict(extra="forbid")`.
- Never use a bare `except:` or `except Exception:` without logging with full traceback.
- Google-style docstrings on all public classes and functions.
- Module-level docstring on every module.
- No emoticons in code, comments, or documentation.

---

## Project structure

```
mimirheim_helpers/config_editor/
  README.md                   # external specification (authoritative, current implementation)
  SPEC.md                    # schema contract specification (authoritative; see "Source of truth" above for what is current vs. target)
  IMPLEMENTATION_DETAILS.md  # Jedison integration details (authoritative)
  AGENTS.md                   # this file
  config_editor/
    __init__.py
    __main__.py               # entry point: config load, server start, signal handling
    config.py                 # ConfigEditorConfig Pydantic model; load_config()
    registry.py                # schema discovery, meta-schema validation, context
                                # composition, two-pass validation, save semantics
                                # (SPEC.md §2-§9); no HTTP imports
    server.py                 # ConfigEditorServer: HTTP server, registry-backed API
    yaml_io.py                 # comment-preserving, atomic YAML read/write, shared
                                # by server.py and registry.py
    schemas/
      bundled/                 # one <id>.schema.json per entry, generated by
                                # scripts/generate_schema_json.py, committed and
                                # drift-tested
      _meta/
        mimirheim-helper-schema.meta.json   # structural meta-schema (SPEC.md §10 rule 3)
    static/
      index.html
      app.js                        # nav rail, entry construction/save/preview, deep links
      topic-placeholder-editor.js   # the "mimir-topic-placeholder" custom Jedison editor
      style.css
      vendor/
        jedison.1.21.0.umd.js  # vendored Jedison build (pinned, checksummed; see SPEC.md)
  tests/
    conftest.py
    unit/
      test_config.py          # ConfigEditorConfig schema and load_config() tests
      test_registry.py         # registry.py: rejection rules, discovery, composition,
                                # validation, save semantics
      test_schema_drift.py     # bundled schemas match their Pydantic models
```

Server, CRUD, and frontend-smoke tests for `server.py` and `static/` itself
(`test_config_editor_server.py`, `test_config_editor_crud_generic.py`,
`test_config_editor_frontend_smoke.py`) live in the root `tests/unit/`
directory, not here -- see "Testing approach" below.

---

## HTTP API

The current, live HTTP API -- registry-backed endpoints only, no deprecated
aliases -- is documented in `README.md` §4. `SPEC.md` §12 documents the same
endpoints in more detail (request/response shapes, status codes) and is
authoritative for those. Do not duplicate either table here — update the
relevant document instead.

---

## IP allowlist

When running as a HA add-on, the `CONFIG_EDITOR_ALLOWED_IP` environment variable
is set by `container/etc/cont-init.d/00-options-env.sh` to the container's
default gateway IP (the ingress proxy). `load_config()` reads this variable and
sets `cfg.allowed_ip`; the server then rejects non-matching IPs with HTTP 403.

When the variable is absent (plain Docker), `allowed_ip` is `None` and no IP
restriction applies.

---

## Testing approach

Tests for this module live in `mimirheim_helpers/config_editor/tests/`. They are
discovered and run by the root `pytest` configuration.

- Config schema tests (`test_config.py`): verify `ConfigEditorConfig` defaults,
  field bounds, `extra="forbid"`, and the `CONFIG_EDITOR_ALLOWED_IP` env override.
- Registry tests (`test_registry.py`): discovery, meta-schema and per-rule
  rejection, drop-in collision handling, context composition, the two-pass
  validation model, and save semantics -- against fixture schema files, not
  the real bundled ones.
- Schema drift tests (`test_schema_drift.py`): every bundled
  `schemas/bundled/<id>.schema.json` matches what
  `scripts/generate_schema_json.py` would emit today.
- Server and CRUD tests (`tests/unit/test_config_editor_server.py` in the
  root `tests/` directory): verify API behaviour in-process without a live
  socket, including the CSP header (SPEC.md §11) and `GET /api/entry`'s
  redacted `mqtt_env`.
- CRUD and frontend-smoke tests (`tests/unit/test_config_editor_crud_generic.py`
  and `tests/unit/test_config_editor_frontend_smoke.py` in the root `tests/`
  directory): start a real `ConfigEditorServer` on a live socket. The
  frontend-smoke test does not execute or render any JavaScript -- it only
  confirms `index.html` references the right scripts and that every one is
  actually served; see that file's module docstring for what it deliberately
  does not cover (that is plan 69's manual verification checklist's job).

Write the config test first (TDD). Server behaviour is tested in the root test
suite. Integration tests (live socket, full HTTP round-trip) require no MQTT
broker but do require a running `ConfigEditorServer`; mark them with
`@pytest.mark.integration`.
