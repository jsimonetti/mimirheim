# pv_ml_learner — Agent Instructions

This tool is a helper package inside the mimirheim repository. It ships in the
single `mimirheim` wheel, alongside the solver and every other helper, and it
runs as its own daemon process communicating over MQTT.

---

## Source of truth

**`../../../plans/41_pv_ml_forecast_helper.md`** is the authoritative specification
for this tool: architecture, module boundaries, data flow, KNMI and Meteoserver API
details, configuration schema, and the full 12-step implementation sequence.

Read that file in full before making any changes to this tool.

The wiki provides supplementary user-facing documentation for this tool:
- [wiki/Helpers/PV-ML-Learner.md](../../../wiki/Helpers/PV-ML-Learner.md) — setup guide, training/inference scheduling, hyperparameter tuning.
- [wiki/Developer/Helper-API.md](../../../wiki/Developer/Helper-API.md) — MQTT contract for all mimirheim input topics.

---

## Dependencies

There is one `pyproject.toml`, at the repo root. This tool has no `pyproject.toml`
of its own and must not be given one: the build only reads the root file, so a
local one would be silently ignored.

Runtime dependencies belong in the root `pyproject.toml`, under this tool's extra:

```toml
[project.optional-dependencies]
pv-ml-learner = [
    "httpx>=0.27", "knmi-py>=0.2", "sqlalchemy>=2.0.52",
    "xgboost>=3.4.1", "scikit-learn>=1.9.0", "joblib>=1.3", "pandas>=3.0.5",
]
```

Anything added there must also be added to the `helpers` meta-extra, which the
container build and full developer environments install.
Two further extras exist for the optional database drivers:
`pv-ml-learner-postgres` and `pv-ml-learner-mysql`.

`helper_common` is a deliberate shared dependency, not a violation of anything.
`__main__.py` builds on `MqttDaemon` rather than `HelperDaemon`, because
this daemon has two independent trigger topics (train and infer) instead of
one.

---

## Environment

There is one lockfile and one virtual environment, both at the repo root. Run
every command from there, not from this directory:

```bash
uv sync --all-extras                          # core plus every helper dependency
uv run pytest                                 # the whole suite, this package included
uv run pytest mimirheim_helpers/pv/pv_ml_learner/tests
uv run ruff check .                           # must be clean before a change is done
uv run python -m pv_ml_learner --config config.yaml
```

The module path is `pv_ml_learner`, not a dotted path under
`mimirheim_helpers`. The package is published at the top level by
`[tool.hatch.build.targets.wheel]` in the root `pyproject.toml`, and the
container's s6 service invokes it the same way.

---

## Module boundaries

- `config.py` — Pydantic schema only. No imports from any other pv_ml_learner module.
- `storage.py` — SQLAlchemy Core schema and repository functions. No MQTT, no HTTP.
- `knmi_fetcher.py` — KNMI data only. No Meteoserver, no HA, no MQTT.
- `meteoserver_fetcher.py` — Meteoserver data only. No KNMI, no HA, no MQTT.
- `ha_actuals.py` — Home Assistant database reader. Read-only. No MQTT.
- `dataset_builder.py` — Joins storage data into training rows. No I/O.
- `features.py` — Builds feature matrices from training rows and McRow objects. No I/O.
- `trainer.py` — XGBoost training. Reads from storage, writes model file. No MQTT.
- `predictor.py` — Loads model, produces forecast steps. No MQTT.
- `publisher.py` — Publishes forecast via MQTT. No training, no DB writes.
- `__main__.py` — Daemon: schedules, MQTT loop, orchestrates all modules.

---

## Code standards

All rules from the root `AGENTS.md` apply here:
- All public functions and methods must have complete type annotations.
- All Pydantic models use `ConfigDict(extra="forbid")`.
- Never use bare `except:` or `except Exception:` without re-raising or logging.
- No emoticons anywhere.
- Google-style docstrings on all public classes and functions.
- Module-level docstrings on every module.

---

## Testing discipline

Follow the TDD workflow from plan 41: write the test first, confirm it fails,
implement, confirm it passes. Do not proceed to the next step until the current
step's tests are green.

Test commands:

```bash
uv run pytest tests/unit/ -q          # unit tests only
uv run pytest tests/integration/ -q   # integration tests only
uv run pytest -q                      # all tests
```
