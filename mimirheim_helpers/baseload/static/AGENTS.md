# baseload_static — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

It is the simplest of the three base load helpers: the profile is written out in
the config file, so there is no external system to fetch from.

---

## Source of truth

Before writing any code, read:

- `AGENTS.md` in the repo root — the code standards, testing discipline and
  environment rules that apply to every package here.
- `IMPLEMENTATION_DETAILS.md` in the repo root — mimirheim's architectural
  conventions.
- [wiki/Helpers/Baseload-Static.md](../../../wiki/Helpers/Baseload-Static.md) —
  setup guide and profile examples.
- [wiki/Developer/Helper-API.md](../../../wiki/Developer/Helper-API.md) — the
  MQTT contract for all mimirheim input topics.

`mimirheim_helpers/examples/baseload-static.yaml` is the annotated reference
configuration.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no
`pyproject.toml` of its own and must not be given one: the build only reads the
root file, so a local one would be silently ignored.

This tool has no extra, and needs none. It performs no I/O beyond MQTT, so
`paho-mqtt`, `pydantic` and `pyyaml` — all core `mimirheim` dependencies — are
enough. If you find yourself wanting to add one, check first whether the change
belongs in `baseload_ha` or `baseload_ha_db` instead: fetching from somewhere is
what those two are for, and keeping this one dependency-free is the point of
having three.

`helper_common` is a deliberate shared dependency, not a violation of anything.
`config.py` imports `MqttConfig` and `apply_mqtt_env_overrides`, and
`__main__.py` builds on `HelperDaemon`, so every helper validates broker
settings identically and handles triggers the same way.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                        # core plus every helper dependency
uv run pytest                               # the whole suite
uv run pytest mimirheim_helpers/baseload/static/tests
uv run ruff check .                         # must be clean before a change is done
uv run python -m baseload_static --config config.yaml
```

The module path is `baseload_static`, not a dotted path under
`mimirheim_helpers`. The package is published at the top level by
`[tool.hatch.build.targets.wheel]` in the root `pyproject.toml`, and
`container/etc/s6-overlay/s6-rc.d/baseload-static/run` invokes it the same way.

---

## Project structure

```
mimirheim_helpers/baseload/static/
  AGENTS.md            # this file
  baseload_static/
    __init__.py
    __main__.py        # entry point and StaticBaseloadDaemon
    config.py          # Pydantic config schema (BaseloadConfig)
    forecast.py        # tiles the profile across the horizon
    publisher.py       # formats the payload and publishes it retained
  tests/
    unit/
      test_config.py
      test_forecast.py
      test_publisher.py
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` (config) | A message here fires one build-and-publish cycle |
| Subscribes | `homeassistant/status` | HA birth message; re-publishes discovery |
| Publishes | `output_topic` (config) | Retained base load forecast in mimirheim format |
| Publishes | `mimir_trigger_topic` (config, optional) | Empty QoS 0 trigger, when `signal_mimir: true` |
| Publishes | `stats_topic` (config, optional) | Retained per-cycle JSON statistics |

The subscriptions, the retain guard, the debounce and the discovery publication
all come from `helper_common.daemon.HelperDaemon`. This package implements one
method, `_run_cycle`.

---

## The three base load helpers are mutually exclusive

`baseload-static.yaml`, `baseload-ha.yaml` and `baseload-ha-db.yaml` all publish
to the same topic, so only one may be enabled. The config editor enforces this:
`_load_helper_models` in `config_editor/server.py` lists the other two as
competing files, and enabling one deletes them.

If you add a fourth variant, add it to `_baseload_variants` there as well, or
two helpers will fight over the topic with no warning.

---

## Two profile shapes

`profile_kw` is a flat hourly list, tiled to fill `horizon_hours`.
`weekly_profiles_kw` maps a weekday number to its own list, for households
whose weekends differ from weekdays. `forecast.py` handles both; the weekday
lookup is what makes it more than a `list` multiplication.

All timestamps are UTC. Never apply a local timezone offset.

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
