# reporter — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process.

Unlike the input helpers it produces nothing mimirheim consumes. It reads the
solve dumps mimirheim writes and renders an HTML report archive for a human.

---

## Source of truth

Before writing any code, read:

- `AGENTS.md` in the repo root — the code standards, testing discipline and
  environment rules that apply to every package here.
- `IMPLEMENTATION_DETAILS.md` in the repo root — mimirheim's architectural
  conventions, including the shape of the dump files this tool parses.
- [wiki/Helpers/Reporter.md](../../wiki/Helpers/Reporter.md) — setup guide and
  what the report contains.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no
`pyproject.toml` of its own and must not be given one: the build only reads the
root file, so a local one would be silently ignored.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's
extra:

```toml
[project.optional-dependencies]
reporter = ["plotly>=6.9.0"]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.

`plotly` is used twice over: as the charting library, and as the source of the
`plotly.min.js` bundle the daemon copies into `output_dir` from the package's
own `package_data`. Reports are served as static files, so the JS has to be on
disk next to them.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                        # core plus every helper dependency
uv run pytest                               # the whole suite
uv run pytest mimirheim_helpers/reporter/tests
uv run ruff check .                         # must be clean before a change is done
uv run python -m reporter --config config.yaml
```

The module path is `reporter`, not a dotted path under `mimirheim_helpers`. The
package is published at the top level by
`[tool.hatch.build.targets.wheel]` in the root `pyproject.toml`, and
`container/etc/s6-overlay/s6-rc.d/reporter/run` invokes it the same way.

---

## Project structure

```
mimirheim_helpers/reporter/
  AGENTS.md            # this file
  reporter/
    __init__.py
    __main__.py        # entry point: argument parsing, config load, run
    config.py          # Pydantic config schema (ReporterConfig) and loader
    daemon.py          # ReporterDaemon: notifications, catch-up, GC
    render.py          # builds one report's HTML
    _render_helpers.py # the individual figures
    metrics.py         # derived numbers shown in the report
    inventory.py       # maintains inventory.js for index.html
    gc.py              # enforces max_reports
    static/            # index.html and index.css, copied into output_dir
  tests/
    conftest.py        # the committed fixture dump pair
    fixtures/          # a real input/output dump pair, committed on purpose
    unit/
    integration/
```

---

## MQTT interface

| Direction | Topic | Description |
|-----------|-------|-------------|
| Subscribes | `reporting.notify_topic` (config) | JSON pointer to a new dump pair, published by mimirheim after each solve |

The reporter only subscribes. It publishes nothing, which is why it extends
`helper_common.daemon.MqttDaemon` rather than `HelperDaemon`: it is
event-driven, not trigger-driven, and has no `_run_cycle`.

It does not poll the filesystem. Missed notifications are recovered by the
catch-up scan in `run()`, which renders any dump pair on disk that has no
report yet.

---

## Do not trust the notification payload

`ts`, `input_path` and `output_path` all arrive over MQTT. Anyone who can
publish to the broker can set them, and on a shared Home Assistant broker that
is a wider set than mimirheim.

`ts` becomes a filename, so `_iso_to_safe_ts` validates it against a whitelist
of ISO 8601 characters and rejects `..`. Before that it only substituted
colons, and a notification carrying `ts = "../escaped/PWNED"` wrote an HTML
file outside `output_dir`.

Both dump paths must resolve inside `reporting.dump_dir` — see `_is_within`.
Without that check a crafted notification could have the reporter read any file
the process can reach and embed it in a report, which the config editor then
serves over HTTP.

Keep both checks. If you add another field that becomes a path or a filename,
validate it the same way.

---

## Test fixtures are committed on purpose

`tests/fixtures/` holds a real dump pair, in the repository. It used to be
copied at session start from the gitignored `mimirheim_dumps/` directory, and
the tests skipped when it was absent — which on CI was every run, silently.

Refreshing the fixtures is now an explicit developer action:
`scripts/refresh_reporter_fixtures.py`. Do not make the render tests depend on
a directory that only exists on a machine that has run mimirheim, and do not
turn a missing committed fixture into a skip. A missing fixture is a broken
checkout.

---

## Code standards

The root `AGENTS.md` governs. The points that come up most in this package:

- Test-driven development. The test exists and fails before the implementation.
- Complete type annotations on every public function and method.
- `model_config = ConfigDict(extra="forbid")` on every Pydantic model.
- Google-style docstrings on all public classes and functions, and a
  module-level docstring on every module.
- Never a bare `except:` or `except Exception:` without logging the full
  traceback. `_on_notification` and `_render_and_save` catch broadly on
  purpose: a bad payload or an unrenderable dump must not kill the paho
  callback thread and take the daemon with it. Both log the traceback.
- No emoticons in code, comments or documentation.

The many `dict()` calls in the Plotly figure code are deliberate and match
Plotly's own documented style. Do not convert them to literals.
