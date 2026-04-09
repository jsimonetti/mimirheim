# pv_ml_learner — Agent Instructions

This tool is **independent of mimirheim**. It has its own `pyproject.toml`, its own
virtual environment, and its own dependency set. It communicates with mimirheim
exclusively via MQTT topics. There is no shared Python package, no shared virtual
environment, and no shared imports between this tool and mimirheim.

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

## Critical separation rule

**Never add dependencies required by this tool to the mimirheim `pyproject.toml`.** If
this tool needs a library, add it here:

```
mimirheim_helpers/pv/learner/pyproject.toml
```

---

## Environment setup

All commands must be run from this directory (`mimirheim_helpers/pv/learner/`), not
from the repo root.

```bash
cd mimirheim_helpers/pv/learner

uv sync --group dev           # create .venv and install all dependencies
uv run pytest                 # run tests
uv run python -m pv_ml_learner --config config.yaml   # run the daemon
```

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
