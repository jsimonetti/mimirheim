# 13. Development environment and dependency management

**Decision: uv with a committed lockfile**

mimirheim uses [uv](https://github.com/astral-sh/uv) for environment and dependency management. `pyproject.toml` declares all dependencies and their version constraints. `uv.lock` is committed to the repository and guarantees reproducible installs on all machines and in CI.

## Rationale

`requirements.txt` has no dependency resolution metadata and poor lockfile semantics. `pip-tools` improves on this but is a workaround for a weak foundation. uv supersedes both:

- Written in Rust — environment creation and dependency resolution are significantly faster than pip.
- `uv sync` creates `.venv` in the project root and installs all dependencies from the lockfile in one command. No manual venv creation step.
- Dependency groups (`[dependency-groups]`) separate production and development dependencies cleanly.
- `uv run` executes commands inside the managed environment without requiring explicit activation.
- `uv.lock` is cross-platform and safe to commit. It is the source of truth for the resolved dependency graph.

## Common commands

```bash
# First-time setup — creates .venv and installs core, dev and all helper dependencies
uv sync --all-extras

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

[`pyproject.toml`](../pyproject.toml) is the single source for dependency declarations and version constraints. This section describes its structure, not its contents; consult the file for the current list and floors.

- `[project] dependencies` holds the runtime dependencies of the core solver. `pip install mimirheim` installs only these.
- `[project.optional-dependencies]` holds an extra per helper (some with database-specific variants such as `-postgres` and `-mysql`), plus a `helpers` extra that combines them. A helper's third-party dependencies are never pulled in by the core install.
- `[dependency-groups] dev` holds test and lint tooling. `uv sync` installs it by default in a development checkout; `uv sync --no-dev` omits it, as the container build does.
- `mimirheim_shared` is a uv workspace member (`[tool.uv.workspace]` and `[tool.uv.sources]`), declared as a regular runtime dependency of core.
- `[tool.uv] constraint-dependencies` bounds transitive dependencies that are not imported directly, for example to keep the resolver off vulnerable releases.

## `.venv` management

uv creates `.venv` in the project root. This directory is listed in `.gitignore` and must never be committed. The lockfile (`uv.lock`) is committed and must be kept up to date — run `uv lock` after any manual edit to `pyproject.toml` dependency constraints.
