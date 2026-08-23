# zonneplan — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

It fetches Dutch electricity prices from a Zonneplan customer account. That
account is the thing that makes this helper different from `nordpool`: it needs
credentials, and getting them involves a flow with a human in the middle.

---

## Source of truth

Before writing any code, read:

- `README.md` in this directory — external behaviour, configuration schema,
  MQTT topics, output format. Authoritative for anything a user can observe.
- `AGENTS.md` in the repo root — the code standards, testing discipline and
  environment rules that apply to every package here.
- `IMPLEMENTATION_DETAILS.md` in the repo root — mimirheim's architectural
  conventions.
- [wiki/Helpers/Zonneplan.md](../../../wiki/Helpers/Zonneplan.md) — setup guide
  and the authentication walkthrough.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no
`pyproject.toml` of its own and must not be given one: the build only reads the
root file, so a local one would be silently ignored.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's
extra:

```toml
[project.optional-dependencies]
zonneplan = ["requests>=2.34.2"]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.

This helper uses `requests` where `baseload_ha` and `pv_ml_learner` use
`httpx`. That is not worth unifying on its own; if you do change it, change the
whole file and its tests, not one call site.

`helper_common` is a deliberate shared dependency, not a violation of anything.
`config.py` imports `MqttConfig` and `apply_mqtt_env_overrides`, and
`__main__.py` builds on `HelperDaemon`.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                        # core plus every helper dependency
uv run pytest                               # the whole suite
uv run pytest mimirheim_helpers/prices/zonneplan/tests
uv run ruff check .                         # must be clean before a change is done
uv run python -m zonneplan_prices --config config.yaml
```

The module path is `zonneplan_prices`, not `zonneplan` and not a dotted path
under `mimirheim_helpers`. The directory is named after the vendor and the
package after what it does; `container/etc/s6-overlay/s6-rc.d/zonneplan/run`
invokes `python -m zonneplan_prices`.

---

## Project structure

```
mimirheim_helpers/prices/zonneplan/
  README.md            # external specification (authoritative)
  AGENTS.md            # this file
  zonneplan_prices/
    __init__.py
    __main__.py        # entry point and ZonneplanPricesDaemon
    config.py          # Pydantic config schema (ZonneplanPricesConfig)
    auth.py            # the email OTP flow, as one function
    token.py           # token and pending-auth state files
    api.py             # HTTP calls to the Zonneplan API
    fetcher.py         # turns an API response into price steps
    publisher.py       # formats the payload and publishes it retained
  tests/               # note: flat, not tests/unit/, unlike the other helpers
    test_api.py
    test_auth.py
    test_config.py
    test_fetcher.py
    test_main.py
    test_token.py
```

The flat `tests/` layout is an inconsistency with every other helper, which use
`tests/unit/`. It is harmless and left alone; do not fix it in a commit that is
about something else.

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `trigger_topic` (config) | A message here fires one fetch-and-publish cycle |
| Subscribes | `homeassistant/status` | HA birth message; re-publishes discovery |
| Publishes | `output_topic` (config) | Retained prices payload in mimirheim format |
| Publishes | `mimir_trigger_topic` (config, optional) | Empty QoS 0 trigger, when `signal_mimir: true` |
| Publishes | `stats_topic` (config, optional) | Retained per-cycle JSON statistics |

---

## Authentication: the part to be careful with

There is no API key. The user gives their email address, Zonneplan sends them a
link, and the daemon polls until they click it. `auth.attempt_auth` runs this
whole flow and is called from `_run_cycle` whenever there is no valid token.

The flow is stateless with respect to the running process. Every step is
persisted, so a container restart mid-flow resumes rather than starting over
and sending a second email:

- No token file and no pending file: send the login email, write the pending
  file, start polling.
- Fresh pending file: resume polling the same UUID.
- Stale pending file: the OTP has expired, so delete it and start again.

Keep that property. An implementation that holds the UUID only in memory will
email the user on every restart.

Both state files hold credentials and are written by `token._write_secret_json`,
which creates them at 0600 via `mkstemp` and renames them into place. Do not
replace it with `Path.write_text`: that creates at 0644 minus umask, which left
the refresh token world-readable, and it truncates before writing, so a crash
mid-write corrupts the token and forces the user through the email flow again.
There are tests asserting the file mode.

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
- Never log a token, a refresh token, or an auth UUID.

All timestamps are UTC. Never apply a local timezone offset.
