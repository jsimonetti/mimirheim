# homeassistant — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no `pyproject.toml`
of its own and must not be given one: the build only reads the root file, so a
local one would be silently ignored.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's extra:

```toml
[project.optional-dependencies]
baseload-ha = ["httpx>=0.27"]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.

`helper_common` is a deliberate shared dependency, not a violation of anything.
`config.py` imports `MqttConfig` and `apply_mqtt_env_overrides`, and
`__main__.py` builds on `HelperDaemon`, so every helper validates broker
settings identically and handles triggers the same way.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                          # core plus every helper dependency
uv run pytest                                 # the whole suite, this package included
uv run pytest mimirheim_helpers/baseload/homeassistant/tests
uv run ruff check .                           # must be clean before a change is done
uv run python -m baseload_ha --config config.yaml
```

The module path is `baseload_ha`, not a dotted path under
`mimirheim_helpers`. The package is published at the top level by
`[tool.hatch.build.targets.wheel]` in the root `pyproject.toml`, and the
container's s6 service invokes it the same way.

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, configuration schema, MQTT topics, output format, HA API prerequisites.
- `IMPLEMENTATION_DETAILS.md` in the repo root — the mimirheim architectural conventions this tool follows.

The wiki provides supplementary user-facing documentation for this tool:
- [wiki/Helpers/Baseload-HA.md](../../../../wiki/Helpers/Baseload-HA.md) — setup guide, lookback/decay explanation, Long-Lived Access Token instructions.
- [wiki/Developer/Helper-API.md](../../../../wiki/Developer/Helper-API.md) — MQTT contract for all mimirheim input topics.

---

## Code standards

Apply all mimirheim code standards from the root `AGENTS.md` to this tool without exception:

- All public functions and methods must have complete type annotations.
- All Pydantic models must set `model_config = ConfigDict(extra="forbid")`.
- Never use a bare `except:` or `except Exception:` without logging with full traceback.
- Google-style docstrings on all public classes and functions.
- Module-level docstring on every module.
- No emoticons in code, comments, or documentation.

---

## Project structure

```
mimirheim_helpers/baseload/homeassistant/
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  baseload_ha/
    __init__.py
    __main__.py        # entry point: config load, MQTT loop, signal handling
    config.py          # Pydantic config schema (BaseloadConfig, HaConfig, etc.)
    fetcher.py         # calls HA statistics API; returns list[HourlyReading]
    forecast.py        # computes same-hour average profile from historical readings
    publisher.py       # formats payload and publishes retained to output_topic
  tests/
    unit/
      test_config.py
      test_fetcher.py
      test_forecast.py
      test_publisher.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` (config) | A message here fires one fetch-and-publish cycle |
| Publishes | `output_topic` (config) | Retained base load forecast payload |
| Publishes | `mimir_trigger_topic` (config, optional) | Empty trigger sent after publishing, if `signal_mimir: true` |

The tool never imports from `mimirheim/` and never calls `build_and_solve()`.

---

## HA API notes

The tool calls:

```
GET {ha_url}/api/recorder/statistics_during_period
```

with a `Bearer` token in the `Authorization` header. The response format changed in HA 2022.11; do not target earlier versions. Parse the response as a dict keyed by `entity_id` where each value is a list of `{"start": ..., "mean": ...}` objects.
