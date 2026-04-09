# homeassistant_db — Agent Instructions

This tool is **independent of mimirheim**. It has its own `pyproject.toml`, its own virtual environment, and its own dependency set. It communicates with mimirheim exclusively via MQTT topics. There is no shared Python package, no shared virtual environment, and no shared imports between this tool and mimirheim.

---

## Critical separation rule

**Never add dependencies required by this tool to the mimirheim `pyproject.toml`.** If this tool needs a library, add it here:

```
mimirheim_helpers/baseload/homeassistant_db/pyproject.toml
```

The mimirheim `pyproject.toml` at the repo root must not list `sqlalchemy` or any other dependency that belongs to this tool.

---

## Environment setup

All commands must be run from this directory (`mimirheim_helpers/baseload/homeassistant_db/`), not from the repo root.

```bash
cd mimirheim_helpers/baseload/homeassistant_db

uv sync --group dev           # create .venv and install runtime + test dependencies
uv run pytest                 # run tests
uv run python -m baseload_ha --config config.yaml   # run the tool
```

Use `uv sync` without `--group dev` only when you want runtime dependencies without pytest.
Running `uv sync` from the repo root creates the mimirheim venv, not this tool's venv.

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, configuration schema, MQTT topics, output format, database prerequisites.
- `IMPLEMENTATION_DETAILS.md` in the repo root — the mimirheim architectural conventions this tool follows.

The wiki provides supplementary user-facing documentation for this tool:
- [wiki/Helpers/Baseload-HA-DB.md](../../../../wiki/Helpers/Baseload-HA-DB.md) — setup guide, SQLAlchemy URL examples, Docker volume mount pattern.
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
mimirheim_helpers/baseload/homeassistant_db/
  pyproject.toml       # dependencies: paho-mqtt, pydantic, pyyaml, sqlalchemy
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  baseload_ha_db/
    __init__.py
    __main__.py        # entry point: config load, MQTT loop, signal handling
    config.py          # Pydantic config schema (BaseloadConfig, HaConfig, etc.)
    fetcher.py         # queries HA recorder DB via SQLAlchemy; returns hourly readings
    forecast.py        # computes same-hour average profile from historical readings
    publisher.py       # formats payload and publishes retained to output_topic
  tests/
    unit/
      test_config.py
      test_fetcher.py
      test_forecast.py
      test_publisher.py
      test_on_message.py
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

## Database access notes

The tool queries the HA recorder database via SQLAlchemy using two tables:

- `statistics_meta` — maps human-readable entity IDs to integer primary keys.
- `statistics` — one row per entity per hour, with `start_ts` (Unix float) and `mean` (float).

This schema is present in all HA recorder backends (SQLite, PostgreSQL, MariaDB) from HA 2023.3 onwards.

The `db_url` config field is a standard SQLAlchemy connection URL. The SQLite driver is built into Python. PostgreSQL (`psycopg2-binary`) and MariaDB (`pymysql`) require the matching optional extra:

```bash
uv pip install mimirheim-baseload-homeassistant-db[postgres]
uv pip install mimirheim-baseload-homeassistant-db[mysql]
```

All queries in `fetcher.py` are `SELECT`-only. No writes are performed.


with a `Bearer` token in the `Authorization` header. The response format changed in HA 2022.11; do not target earlier versions. Parse the response as a dict keyed by `entity_id` where each value is a list of `{"start": ..., "mean": ...}` objects.
