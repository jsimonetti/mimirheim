"""Generate mimirheim/config/schema.json from the live Pydantic schema.

Run this script whenever MimirheimConfig or any of its sub-models change.
The output is committed to the repository and validated by
tests/unit/test_schema_ui_annotations.py::test_schema_json_is_up_to_date.

Usage:
    uv run python scripts/generate_schema_json.py
"""

import json
from pathlib import Path

from mimirheim.config.schema import MimirheimConfig

outpath = Path(__file__).parents[1] / "mimirheim" / "config" / "schema.json"
outpath.write_text(json.dumps(MimirheimConfig.model_json_schema(), indent=2) + "\n")
print(f"Written: {outpath}")
