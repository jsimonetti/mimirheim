# 13. Development environment and dependency management

**Decision: uv with a committed lockfile**

mimirheim uses [uv](https://github.com/astral-sh/uv) for environment and dependency management. `pyproject.toml` declares all dependencies and their version constraints. `uv.lock` is committed to the repository and guarantees reproducible installs on all machines and in CI.

## Rationale

`requirements.txt` has no dependency resolution metadata and poor lockfile semantics. `pip-tools` improves on this but is a workaround for a weak foundation. uv supersedes both:

- Written in Rust — environment creation and dependency resolution are significantly faster than pip.
- `uv sync` creates `.venv` in the project root and installs all dependencies from the lockfile in one command. No manual venv creation step.
- Dependency groups (`dev-dependencies`) separate production and development dependencies cleanly.
- `uv run` executes commands inside the managed environment without requiring explicit activation.
- `uv.lock` is cross-platform and safe to commit. It is the source of truth for the resolved dependency graph.

## Common commands

```bash
# First-time setup — creates .venv and installs all dependencies
uv sync

# Add a production dependency (updates pyproject.toml and uv.lock)
uv add mip

# Add a development-only dependency
uv add --dev pytest amqtt pytest-asyncio

# Run the test suite
uv run pytest

# Run the application
uv run python -m mimirheim --config config.yaml
```

## Dependency groups

`pyproject.toml` separates runtime and development dependencies:

```toml
[project]
dependencies = [
    "mip>=1.14",
    "pydantic>=2.13.4",
    "paho-mqtt>=2.0",
    "pyyaml>=6.0",
]

[tool.uv]
dev-dependencies = [
    "pytest>=8.0",
    "amqtt>=0.11",
    "pytest-asyncio>=0.23",
]
```

The `dev-dependencies` group is installed by `uv sync` by default in a development checkout. In a production deployment, `uv sync --no-dev` installs only the runtime dependencies.

## `.venv` management

uv creates `.venv` in the project root. This directory is listed in `.gitignore` and must never be committed. The lockfile (`uv.lock`) is committed and must be kept up to date — run `uv lock` after any manual edit to `pyproject.toml` dependency constraints.
