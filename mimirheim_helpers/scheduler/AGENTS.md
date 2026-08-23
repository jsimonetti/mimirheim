# scheduler — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

---

## Source of truth

Before writing any code, read:

- `README.md` in this directory — external behaviour, cron expression syntax,
  MQTT topic conventions. Authoritative for anything a user can observe.
- `AGENTS.md` in the repo root — the code standards, testing discipline and
  environment rules that apply to every package here, this one included.
- `IMPLEMENTATION_DETAILS.md` in the repo root — mimirheim's architectural
  conventions.

The wiki provides supplementary user-facing documentation:

- [wiki/Helpers/Scheduler.md](../../wiki/Helpers/Scheduler.md) — setup guide,
  cron patterns, multi-topic scheduling, limitations.

---

## Environment

There is one `pyproject.toml`, one lockfile and one virtual environment, all at
the repo root. Run every command from there, not from this directory:

```bash
uv sync --all-extras                                  # core plus every helper dependency
uv run pytest                                         # the whole suite, this package included
uv run pytest mimirheim_helpers/scheduler/tests       # just this package
uv run ruff check .                                   # must be clean before a change is done
uv run python -m scheduler --config config.yaml       # run the tool
```

The module path is `scheduler`, not `mimirheim_helpers.scheduler`. The package
is published at the top level by `[tool.hatch.build.targets.wheel]` in the root
`pyproject.toml`, and `container/etc/s6-overlay/s6-rc.d/scheduler/run` invokes
it the same way.

### Dependencies

Runtime dependencies belong in the root `pyproject.toml`, under this tool's
extra:

```toml
[project.optional-dependencies]
scheduler = ["apscheduler>=3.11.3,<4.0"]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.

`helper_common` is a deliberate shared dependency, not a violation of anything:
`config.py` imports `MqttConfig` and `apply_mqtt_env_overrides` from it, so that
every helper validates broker settings identically and picks up the same Home
Assistant Supervisor environment overrides.

---

## Project structure

```
mimirheim_helpers/scheduler/
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  example.yaml         # annotated reference configuration
  scheduler/
    __init__.py
    __main__.py        # entry point: argument parsing, MQTT connect, shutdown
    config.py          # Pydantic config schema (SchedulerConfig) and loader
    cron.py            # standard cron expressions, translated for APScheduler
    loop.py            # registers cron jobs and publishes triggers
  tests/
    conftest.py        # the scheduler fixture, which stops what a test starts
    unit/
      test_config.py
      test_cron.py
      test_loop.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Publishes | value of each `schedules` entry (config) | Empty message (not retained, QoS 0) published when the cron expression (key) fires |

The scheduler only publishes — it never subscribes to any topic. It has no
awareness of what tool listens on each trigger topic. Note that this makes it
the opposite of the other input helpers: it is the component that *sends* the
triggers they consume, so `helper_common.daemon.HelperDaemon`, which exists to
receive them, is the wrong base class here.

---

## Implementation note: cron scheduling

Scheduling is APScheduler's job. `loop.py` registers one `BackgroundScheduler`
cron job per schedule entry and then blocks until the stop event is set. Do not
reintroduce a hand-rolled timer, a min-heap of next fire times, or a
`time.sleep` loop: APScheduler already handles drift, coalescing and misfire
grace, and a second implementation of that is what this module deliberately
does not have.

One incompatibility needs care. The configuration file promises standard cron,
where the day-of-week field numbers Sunday as 0 and accepts 7 as a second
spelling of Sunday. APScheduler numbers the same field from Monday and rejects
7. `cron.py` reconciles the two, and it is the only place that should call
`CronTrigger.from_crontab`. Build every trigger through `cron.build_trigger` so
that startup validation and job registration cannot interpret an expression
differently.

All cron expressions and all `datetime` objects are UTC. Never apply a local
timezone offset.

---

## Code standards

The root `AGENTS.md` governs. The points that come up most in this package:

- Test-driven development. The test exists and fails before the implementation.
- Complete type annotations on every public function and method.
- `model_config = ConfigDict(extra="forbid")` on every Pydantic model.
- Google-style docstrings on all public classes and functions, and a
  module-level docstring on every module.
- Never a bare `except:` or `except Exception:` without logging the full
  traceback.
- No emoticons in code, comments or documentation.

A test that injects its own `BackgroundScheduler` must use the `scheduler`
fixture from `tests/conftest.py`. `run()` shuts down only a scheduler it
created itself, so an injected one that nothing stops stays alive with armed
jobs for the rest of the session.
