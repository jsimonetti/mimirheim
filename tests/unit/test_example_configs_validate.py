"""Every example config in mimirheim_helpers/examples/ must validate.

These files exist to be copied. A user who copies one and gets
`ERROR: Invalid configuration` has been handed a broken starting point by the
documentation, and the failure looks like their mistake rather than ours.

Two were broken when this test was written:

- `reporter.yaml` still carried a `chart_publishing:` section. The feature was
  removed and the model sets `extra="forbid"`, so the file failed validation
  outright. wiki/Helpers/Reporter.md even documents the failure -- "If either
  section is still present, reporter startup will fail" -- while the example
  shipped the section it warns about.
- `pv-ml-learner.yaml` used `homeassistant.db_path`, an old field name. The
  model requires `db_url`, and rejects the bare filesystem path the example
  gave it.

Commented-out lines are not covered here, so a stale example inside a comment
will still slip through. Only what YAML actually parses is checked.
"""
from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "mimirheim_helpers" / "examples"

# (example filename, module holding the model, model class name)
_CASES = [
    ("nordpool.yaml", "nordpool.config", "NordpoolConfig"),
    ("zonneplan.yaml", "zonneplan_prices.config", "ZonneplanPricesConfig"),
    ("pv-fetcher.yaml", "pv_fetcher.config", "PvFetcherConfig"),
    ("pv-ml-learner.yaml", "pv_ml_learner.config", "PvLearnerConfig"),
    ("baseload-ha.yaml", "baseload_ha.config", "BaseloadConfig"),
    ("baseload-ha-db.yaml", "baseload_ha_db.config", "BaseloadConfig"),
    ("baseload-static.yaml", "baseload_static.config", "BaseloadConfig"),
    ("reporter.yaml", "reporter.config", "ReporterConfig"),
    ("scheduler.yaml", "scheduler.config", "SchedulerConfig"),
    ("config-editor.yaml", "config_editor.config", "ConfigEditorConfig"),
]


def _load_model(module: str, cls: str) -> Any:
    return getattr(importlib.import_module(module), cls)


@pytest.mark.parametrize(
    ("filename", "module", "cls"), _CASES, ids=[c[0] for c in _CASES]
)
def test_example_config_validates(filename: str, module: str, cls: str) -> None:
    path = _EXAMPLES_DIR / filename
    assert path.exists(), f"{filename} is listed here but missing from examples/"
    raw = yaml.safe_load(path.read_text()) or {}

    _load_model(module, cls).model_validate(raw)


def test_every_example_file_is_covered() -> None:
    """A new example must be added to _CASES, not silently left unchecked."""
    on_disk = {p.name for p in _EXAMPLES_DIR.glob("*.yaml")}
    listed = {c[0] for c in _CASES}

    assert on_disk == listed, (
        f"examples/ and this test disagree. Only on disk: "
        f"{sorted(on_disk - listed)}. Only listed: {sorted(listed - on_disk)}."
    )
