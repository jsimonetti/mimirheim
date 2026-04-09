# scheduler — Agent Instructions

This tool is **independent of mimirheim**. It has its own `pyproject.toml`, its own virtual environment, and its own dependency set. It communicates with input tools and mimirheim exclusively via MQTT. There is no shared Python package, no shared virtual environment, and no shared imports between this tool and mimirheim.

---

## Critical separation rule

**Never add dependencies required by this tool to the mimirheim `pyproject.toml`.** If this tool needs a library, add it here:

```
mimirheim_helpers/scheduler/pyproject.toml
```

The mimirheim `pyproject.toml` at the repo root must not list `croniter` or any other dependency that belongs to this tool.

---

## Environment setup

All commands must be run from this directory (`mimirheim_helpers/scheduler/`), not from the repo root.

```bash
cd mimirheim_helpers/scheduler

uv sync --group dev                # create .venv and install runtime + test dependencies
uv run pytest                      # run tests
uv run python -m scheduler --config config.yaml   # run the tool
```

Use `uv sync` without `--group dev` only when you want runtime dependencies without pytest.
Running `uv sync` from the repo root creates the mimirheim venv, not this tool's venv.

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, cron expression syntax, MQTT topic conventions.
- `IMPLEMENTATION_DETAILS.md` in the repo root — the mimirheim architectural conventions this tool follows.

The wiki provides supplementary user-facing documentation for this tool:
- [wiki/Helpers/Scheduler.md](../../../wiki/Helpers/Scheduler.md) — setup guide, cron patterns, multi-topic scheduling, limitations.

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
mimirheim_helpers/scheduler/
  pyproject.toml       # dependencies: croniter, paho-mqtt, pydantic, pyyaml
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  scheduler/
    __init__.py
    __main__.py        # entry point: config load, MQTT connect, main loop
    config.py          # Pydantic config schema (SchedulerConfig); schedules is list[dict[str, str]]
    loop.py            # computes next fire times; publishes triggers at the right moment
  tests/
    unit/
      test_config.py
      test_loop.py
    conftest.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Publishes | value of each `schedules` entry (config) | Empty message (not retained, QoS 0) published when the cron expression (key) fires |

The scheduler only publishes — it never subscribes to any topic. It has no awareness of what tool listens on each trigger topic.

---

## Implementation note: cron scheduling

Use `croniter` to compute the next fire time for each schedule entry:

```python
from croniter import croniter
from datetime import datetime, timezone

def next_fire(cron_expr: str, after: datetime) -> datetime:
    return croniter(cron_expr, after).get_next(datetime).replace(tzinfo=timezone.utc)
```

The main loop sleeps until the earliest next fire time across all schedule entries, publishes to the appropriate topic, then recomputes. Do not use a per-schedule thread or asyncio task per entry — a single-threaded event loop with `time.sleep` is sufficient and eliminates concurrency complexity.

All cron expressions and all `datetime` objects in the main loop are UTC. Never apply a local timezone offset.
