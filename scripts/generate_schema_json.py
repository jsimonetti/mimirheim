"""Generate mimirheim/config/schema.json and the bundled config-editor schemas.

Run this script whenever MimirheimConfig, any of its sub-models, or any
helper's Pydantic config model changes.

Two things are generated:

1. ``mimirheim/config/schema.json`` from ``MimirheimConfig.model_json_schema()``.
   Validated by
   ``tests/unit/test_schema_ui_annotations.py::test_schema_json_is_up_to_date``.
2. One ``mimirheim_helpers/config_editor/config_editor/schemas/bundled/<id>.schema.json``
   per row of the table below, each carrying its ``x-mimirheim`` envelope
   (SPEC.md §3). Validated by
   ``mimirheim_helpers/config_editor/tests/unit/test_schema_drift.py``.

Usage:
    uv run python scripts/generate_schema_json.py
"""

import importlib
import json
from pathlib import Path
from typing import Any, TypedDict

from mimirheim.config.schema import MimirheimConfig

REPO_ROOT = Path(__file__).parents[1]


class BundledEntrySpec(TypedDict):
    """One row of the bundled-schema generation table (plan 68, Decision 2)."""

    id: str
    file: str
    category: str
    python_package: str
    python_model: str
    exclusive_group: str | None
    required: bool


# One row per bundled entry. `order` within a category is derived below from
# this list's own row order, per plan 68 Decision 2: "order within each
# category follows the order shown above."
BUNDLED_ENTRIES: list[BundledEntrySpec] = [
    {
        "id": "mimirheim",
        "file": "mimirheim.yaml",
        "category": "core",
        "python_package": "mimirheim",
        "python_model": "mimirheim.config.schema:MimirheimConfig",
        "exclusive_group": None,
        "required": True,
    },
    {
        "id": "nordpool",
        "file": "nordpool.yaml",
        "category": "prices",
        "python_package": "nordpool",
        "python_model": "nordpool.config:NordpoolConfig",
        "exclusive_group": None,
        "required": False,
    },
    {
        "id": "zonneplan",
        "file": "zonneplan.yaml",
        "category": "prices",
        "python_package": "zonneplan_prices",
        "python_model": "zonneplan_prices.config:ZonneplanPricesConfig",
        "exclusive_group": None,
        "required": False,
    },
    {
        "id": "pv-fetcher",
        "file": "pv-fetcher.yaml",
        "category": "pv",
        "python_package": "pv_fetcher",
        "python_model": "pv_fetcher.config:PvFetcherConfig",
        "exclusive_group": None,
        "required": False,
    },
    {
        "id": "pv-ml-learner",
        "file": "pv-ml-learner.yaml",
        "category": "pv",
        "python_package": "pv_ml_learner",
        "python_model": "pv_ml_learner.config:PvLearnerConfig",
        "exclusive_group": None,
        "required": False,
    },
    {
        "id": "baseload-static",
        "file": "baseload-static.yaml",
        "category": "baseload",
        "python_package": "baseload_static",
        "python_model": "baseload_static.config:BaseloadConfig",
        "exclusive_group": "baseload",
        "required": False,
    },
    {
        "id": "baseload-ha",
        "file": "baseload-ha.yaml",
        "category": "baseload",
        "python_package": "baseload_ha",
        "python_model": "baseload_ha.config:BaseloadConfig",
        "exclusive_group": "baseload",
        "required": False,
    },
    {
        "id": "baseload-ha-db",
        "file": "baseload-ha-db.yaml",
        "category": "baseload",
        "python_package": "baseload_ha_db",
        "python_model": "baseload_ha_db.config:BaseloadConfig",
        "exclusive_group": "baseload",
        "required": False,
    },
    {
        "id": "reporter",
        "file": "reporter.yaml",
        "category": "reporting",
        "python_package": "reporter",
        "python_model": "reporter.config:ReporterConfig",
        "exclusive_group": None,
        "required": False,
    },
    {
        "id": "scheduler",
        "file": "scheduler.yaml",
        "category": "scheduling",
        "python_package": "scheduler",
        "python_model": "scheduler.config:SchedulerConfig",
        "exclusive_group": None,
        "required": False,
    },
]

BUNDLED_SCHEMA_DIR = (
    REPO_ROOT / "mimirheim_helpers" / "config_editor" / "config_editor" / "schemas" / "bundled"
)


def _import_model(dotted_path: str) -> Any:
    """Import the Pydantic model class named by a ``"module.path:ClassName"`` string."""
    module_name, _, class_name = dotted_path.partition(":")
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _category_orders(entries: list[BundledEntrySpec]) -> dict[str, int]:
    """Return {entry id: order}, counting up within each category in table order."""
    orders: dict[str, int] = {}
    counters: dict[str, int] = {}
    for entry in entries:
        category = entry["category"]
        orders[entry["id"]] = counters.get(category, 0)
        counters[category] = orders[entry["id"]] + 1
    return orders


def build_bundled_schema(entry: BundledEntrySpec, order: int) -> dict[str, Any]:
    """Build one bundled schema document: the model's JSON Schema plus its x-mimirheim envelope.

    Args:
        entry: One row of BUNDLED_ENTRIES.
        order: This entry's ``order`` within its category (SPEC.md §3).

    Returns:
        The full schema document, ready to be written to
        ``schemas/bundled/<id>.schema.json``.
    """
    model_cls = _import_model(entry["python_model"])
    schema = model_cls.model_json_schema()
    schema["x-mimirheim"] = {
        "file": entry["file"],
        "category": entry["category"],
        "order": order,
        "python_package": entry["python_package"],
        "python_model": entry["python_model"],
        "exclusive_group": entry["exclusive_group"],
        "required": entry["required"],
        "docs_url": None,
    }
    return schema


def main() -> None:
    """Regenerate mimirheim/config/schema.json and every bundled config-editor schema."""
    schema_json_path = REPO_ROOT / "mimirheim" / "config" / "schema.json"
    schema_json_path.write_text(json.dumps(MimirheimConfig.model_json_schema(), indent=2) + "\n")
    print(f"Written: {schema_json_path}")

    BUNDLED_SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    orders = _category_orders(BUNDLED_ENTRIES)
    for entry in BUNDLED_ENTRIES:
        document = build_bundled_schema(entry, orders[entry["id"]])
        outpath = BUNDLED_SCHEMA_DIR / f"{entry['id']}.schema.json"
        outpath.write_text(json.dumps(document, indent=2) + "\n")
        print(f"Written: {outpath}")


if __name__ == "__main__":
    main()
