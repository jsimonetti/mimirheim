# Step 01 — Project scaffold

## References

- IMPLEMENTATION_DETAILS §11 (uv, pyproject.toml structure, dependency groups)
- AGENTS.md (canonical project structure)

---

## Files to create

- `pyproject.toml`
- `.gitignore`
- `mimirheim/__init__.py`
- `mimirheim/config/__init__.py`
- `mimirheim/core/__init__.py`
- `mimirheim/devices/__init__.py`
- `mimirheim/io/__init__.py`
- `tests/__init__.py`
- `tests/unit/__init__.py`
- `tests/integration/__init__.py`
- `tests/scenarios/.gitkeep`
- `tests/conftest.py`

---

## Tests first

There are no unit tests for the scaffold itself. The acceptance criterion is structural:
`uv run pytest` must collect with no errors and exit 0. Zero tests collected is acceptable at this stage.

---

## Implementation

### pyproject.toml

```toml
[project]
name = "mimirheim"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "highspy>=1.7",
    "pydantic>=2.7",
    "paho-mqtt>=2.0",
    "pyyaml>=6.0",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "amqtt>=0.11",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

### __init__.py files

All `__init__.py` files may be empty or contain a one-line module docstring. Do not add imports.

### tests/conftest.py

Create as an empty file. It will be populated in later steps.

### .gitignore

Include at minimum:

```
.venv/
__pycache__/
*.pyc
dist/
.pytest_cache/
*.egg-info/
mimirheim_dumps/
```

---

## Acceptance criteria

```bash
uv sync
uv run pytest
uv run python -c "import mimirheim"
```

All three commands exit 0 with no errors.

---

## Done

```bash
mv plans/01_project_scaffold.md plans/done/
```
