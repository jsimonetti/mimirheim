# open-meteo — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no
`pyproject.toml` of its own and must not be given one: the build only reads the
root file, so a local one would be silently ignored.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's
extra:

```toml
[project.optional-dependencies]
pv-openmeteo = ["open-meteo-solar-forecast>=0.1.32"]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.

The floor of `0.1.32` is not arbitrary. `Estimate.wh_period_15m` — the only
field in the response that carries interval-average power — was added in that
release. Do not lower it.

`helper_common` is a deliberate shared dependency. `__main__.py` builds on
`HelperDaemon`, which supplies the trigger subscription, the retain guard, the
debounce, the HA discovery and the rate-limit suppression this tool relies on.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                          # core plus every helper dependency
uv run pytest                                 # the whole suite, this package included
uv run pytest mimirheim_helpers/pv/open-meteo/tests
uv run ruff check .                           # must be clean before a change is done
uv run python -m pv_openmeteo --config config.yaml
```

The module path is `pv_openmeteo`, not a dotted path under
`mimirheim_helpers`. The package is published at the top level by
`[tool.hatch.build.targets.wheel]` in the root `pyproject.toml`, and the
container's s6 service invokes it the same way.

---

## Source of truth

Before writing any code, read:
- `README.md` in this directory — external behaviour, configuration schema, MQTT topics, output format, confidence decay.
- `IMPLEMENTATION_DETAILS.md` in the repo root — the mimirheim architectural conventions this tool follows.

---

## Code standards

Apply all mimirheim code standards from the root `AGENTS.md` to this tool
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
mimirheim_helpers/pv/open-meteo/
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  pv_openmeteo/        # named pv_openmeteo to avoid shadowing the
                       #   'open_meteo_solar_forecast' library
    __init__.py
    __main__.py        # entry point: config load, MQTT loop, signal handling
    config.py          # Pydantic config schema (PvOpenMeteoConfig and friends)
    fetcher.py         # calls open_meteo_solar_forecast (async); returns kW per timestamp
    series.py          # trims history and attaches confidence values
    publisher.py       # publishes the retained payload and the solve trigger
  tests/
    unit/
      test_config.py
      test_fetcher.py
      test_series.py
      test_publisher.py
      test_fetch_cycle.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` (config) | A message here fires one fetch-and-publish cycle for all arrays |
| Publishes | `arrays.<name>.output_topic` (config) | Retained PV forecast payload per array |
| Publishes | `mimir_trigger_topic` (config, optional) | Empty trigger sent after a successful cycle, if `signal_mimir: true` |

The tool never imports from `mimirheim/` and never calls `build_and_solve()`.

---

## Things that are the way they are on purpose

`fetcher.py` derives kW from `Estimate.wh_period_15m` rather than reading
`Estimate.watts`. The two are different series: `watts` is the instantaneous
power at each timestamp, `wh_period_15m` is the energy delivered across the
quarter hour. mimirheim schedules average power per step, so the energy series
is the correct source. Switching to `watts` would overstate production on the
steep parts of the morning and evening curve.

`ac_kwp` is passed as a scalar for a shared inverter and as a list for
per-plane inverters, because that is how the library distinguishes the two
topologies. `config.py` rejects a config that sets both, since the list
silently wins inside the library.

`use_horizon` is a property derived from whether `horizon` is set, not a field.
A profile that is present but disabled, or enabled but absent, are
configuration states that exist only to be got wrong.

`past_days` is computed from `open_meteo.past_hours` rather than exposed
directly: the API counts whole local days, and a fetch just after local
midnight needs yesterday to satisfy even a one-hour retention window.

`_publish_discovery` is overridden. `HelperDaemon` builds its forecast sensor
from a single top-level `config.output_topic`, which this helper does not have
because every array publishes to its own topic. Without the override the
`forecast_sensor` option would be silently ignored. The override follows the
pv_ml_learner pattern: one `publish_trigger_discovery` call per array with
`trigger_topic=None`, sharing one `device_id`.

That pattern carries a known limitation, the same one plan 59 accepted for
pv_ml_learner: the stale-topic sweep inside `publish_trigger_discovery` only
covers the tool name it was called with, so a renamed or deleted array leaves
its discovery sensor orphaned on the broker. The array name is no longer in
the config at the next connect, so nothing can derive the topic to clear it.
Fixing it properly needs a persisted record of previously published array
names; do not paper over it by broadening the sweep, which would delete the
entities of arrays that are merely disabled in another instance.

`CycleResult.horizon_hours` measures the distance from the fetch time to the
last published step, and reports the *minimum* across arrays. Counting steps
would include the retained `past_hours` of history, and taking the maximum
would claim coverage the solve does not have: mimirheim can only plan as far
as its shortest input series reaches.

Config loading goes through `helper_common.config.load_helper_config` rather
than a private loader. It is the pattern the other five trigger-driven helpers
use, and it catches YAML parse errors and the `ValueError` that
`apply_mqtt_env_overrides` raises for an empty document, both of which escape
as bare tracebacks from a loader that only guards `OSError` and
`ValidationError`.
