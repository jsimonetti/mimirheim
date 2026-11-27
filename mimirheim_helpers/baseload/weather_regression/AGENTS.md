# baseload_weather: Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no
`pyproject.toml` of its own and must not be given one.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's
extra `baseload-weather` (SQLAlchemy, httpx, tzdata), which is also part of the
`helpers` meta-extra. The optional database drivers are in
`baseload-weather-postgres` and `baseload-weather-mysql`.

The only helper package this tool imports is `helper_common`. It deliberately
does not import `baseload_ha_db` or `pv_openmeteo`, even where they do similar
work: helpers may only depend on `helper_common`, so the recorder and
Open-Meteo access are local implementations.

---

## Environment

Run every command from the repo root:

```bash
uv sync --all-extras
uv run pytest mimirheim_helpers/baseload/weather_regression/tests
uv run ruff check .                           # must be clean before a change is done
uv run python -m baseload_weather --config config.yaml
```

The module path is `baseload_weather`, published at the top level of the wheel
by `[tool.hatch.build.targets.wheel]`; the container's s6 service
`baseload-weather` invokes it the same way.

---

## Source of truth

Before writing any code, read:

- `README.md` in this directory: external behaviour, model, configuration,
  output format, fault tolerance.
- `IMPLEMENTATION_DETAILS.md` in the repo root: architectural conventions.
- `mimirheim_helpers/common/AGENTS.md`: the shared daemon and its contract.

---

## Code standards

Apply all mimirheim code standards from the root `AGENTS.md`:

- Complete type annotations on public functions and methods.
- `model_config = ConfigDict(extra="forbid")` on every Pydantic model.
- No bare `except:` or `except Exception:`; catch specific exceptions.
- Google-style docstrings and a module docstring stating scope.
- No emoticons.

Module-specific rules:

- `model.py` is pure: no I/O, no logging of data, no wall-clock reads. Every
  input is passed in, keyed by the UTC epoch second of an hour's start.
- A missing hourly value means "no value", never zero. Do not zero-fill.
- `recorder.py` must keep SQLite read-only for every URL form and must never
  put database credentials in an exception message or its chain.
- The forecast is published before the optional model statistics; a failure
  before publishing must publish nothing.

---

## Project structure

```
mimirheim_helpers/baseload/weather_regression/
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  baseload_weather/
    __init__.py
    __main__.py        # daemon: cycle orchestration, Config Service wiring
    config.py          # Pydantic config schema
    formspec.py        # Config Service FormSpec (ADR-0002, 0006, 0007)
    recorder.py        # HA recorder statistics: energy deltas, temperature means
    openmeteo.py       # Open-Meteo hourly temperature client with retries
    model.py           # training data, fit levels, T_base search, forecast
    publisher.py       # MQTT publishing of forecast and model statistics
  tests/
    unit/
      test_config.py
      test_formspec.py
      test_recorder.py
      test_openmeteo.py
      test_model.py
      test_publisher.py
      test_cycle.py
      test_config_owner_wiring.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` | A message fires one cycle (shared baseload trigger by default) |
| Publishes | `output_topic` | Retained forecast, `horizon_hours + 1` hourly steps |
| Publishes | `mimir_trigger_topic` | Empty trigger after the forecast, if `signal_mimir` |
| Publishes | `model_stats_topic` | Retained fit statistics after a successful forecast, if set |
| Publishes | `stats_topic` | Retained run statistics from the shared daemon, if set |

The tool never imports from `mimirheim/` and never calls `build_and_solve()`.
