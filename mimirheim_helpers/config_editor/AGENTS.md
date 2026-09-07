# config-editor — Agent Instructions

This tool is **not independent of mimirheim**. Unlike the other helpers, it
imports directly from `mimirheim.config.schema` and from every helper config
module to generate the JSON schemas served by its API. It is packaged as part
of the root mimirheim wheel and has no separate `pyproject.toml`.

---

## Critical architectural distinction

config-editor is not an input tool. It has no MQTT connections, no trigger
topics, and no daemon cycle. It is a web interface that reads and writes the
YAML configuration files in `/config`. It exposes mimirheim's and all helpers'
Pydantic schemas through a JSON API so the front end can render a validated
editing form.

Because it imports from the mimirheim and helper config modules, it cannot be
run in isolation from the main package environment.

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
- `SPEC.md` in this directory — the schema contract: schema file format, discovery, field vocabulary, document composition, cross-references, validation, save semantics, security, and HTTP API, **as the rewrite (plans 68-70) targets it**. Where `SPEC.md` and the current implementation disagree, that is expected until those plans land — `SPEC.md` describes where the code is going, not where it is. Do not assume it already matches deployed behaviour.
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
  SPEC.md                    # schema contract specification (authoritative, target design)
  IMPLEMENTATION_DETAILS.md  # Jedison integration details (authoritative)
  AGENTS.md                   # this file
  config_editor/
    __init__.py
    __main__.py               # entry point: config load, server start, signal handling
    config.py                 # ConfigEditorConfig Pydantic model; load_config()
    server.py                 # ConfigEditorServer: HTTP server and all API handlers
    static/
      index.html
      app.js
      style.css
      vendor/
        jedison.umd.js        # vendored Jedison build (pinned, checksummed; see SPEC.md)
  tests/
    conftest.py
    unit/
      test_config.py          # ConfigEditorConfig schema and load_config() tests
```

---

## HTTP API

The current, live HTTP API is documented in `README.md` §4. The target HTTP
API this tool is being rewritten toward is documented in `SPEC.md` §12. Do
not duplicate either table here — update the relevant document instead.

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
- Server and CRUD tests (`tests/unit/test_config_editor_server.py` and
  `tests/unit/test_config_editor_crud_generic.py` in the root `tests/` directory):
  verify API behaviour in-process without a live socket.

Write the config test first (TDD). Server behaviour is tested in the root test
suite. Integration tests (live socket, full HTTP round-trip) require no MQTT
broker but do require a running `ConfigEditorServer`; mark them with
`@pytest.mark.integration`.
