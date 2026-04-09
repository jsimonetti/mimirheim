# nordpool — Agent Instructions

This tool is **independent of mimirheim**. It has its own `pyproject.toml`, its own virtual environment, and its own dependency set. It communicates with mimirheim exclusively via MQTT topics. There is no shared Python package, no shared virtual environment, and no shared imports between this tool and mimirheim.

---

## Critical separation rule

**Never add dependencies required by this tool to the mimirheim `pyproject.toml`.** If this tool needs a library, add it here:

```
mimirheim_helpers/prices/nordpool/pyproject.toml
```

The mimirheim `pyproject.toml` at the repo root must not list `pynordpool` or any other dependency that belongs to this tool.

---

## Environment setup

All commands must be run from this directory (`mimirheim_helpers/prices/nordpool/`), not from the repo root.

```bash
cd mimirheim_helpers/prices/nordpool

uv sync --group dev       # create .venv and install runtime + test dependencies
uv run pytest             # run tests
uv run python -m nordpool --config config.yaml   # run the tool
```

Use `uv sync` without `--group dev` only when you want runtime dependencies without pytest.
Running `uv sync` from the repo root creates the mimirheim venv, not this tool's venv.

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, configuration schema, MQTT topics, output format.
- `IMPLEMENTATION_DETAILS.md` in the repo root — the mimirheim architectural conventions this tool follows (Pydantic, fault resilience, MQTT patterns). This tool adopts the same conventions even though it is not part of mimirheim.

The wiki provides supplementary user-facing documentation for this tool:
- [wiki/Helpers/Nordpool.md](../../../wiki/Helpers/Nordpool.md) — setup guide, area codes, pricing formula examples.
- [wiki/Developer/Helper-API.md](../../../wiki/Developer/Helper-API.md) — MQTT contract for all mimirheim input topics.

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
mimirheim_helpers/prices/nordpool/
  pyproject.toml       # dependencies: pynordpool, paho-mqtt, pydantic, pyyaml
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  nordpool/
    __init__.py
    __main__.py        # entry point: config load, MQTT loop, signal handling
    config.py          # Pydantic config schema (NordpoolConfig, MqttConfig, etc.)
    fetcher.py         # HTTP fetch from Nordpool via pynordpool; returns list[PriceStep]
    publisher.py       # formats payload and publishes retained to output_topic
  tests/
    unit/
      test_config.py
      test_fetcher.py
      test_publisher.py
    conftest.py
```

---

## MQTT interface

This tool communicates with mimirheim strictly via MQTT:

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` (config) | A message here fires one fetch-and-publish cycle |
| Publishes | `output_topic` (config) | Retained prices payload in mimirheim format |
| Publishes | `mimir_trigger_topic` (config, optional) | Empty trigger sent after publishing, if `signal_mimir: true` |

The tool never imports from `mimirheim/` and never calls `build_and_solve()`.
