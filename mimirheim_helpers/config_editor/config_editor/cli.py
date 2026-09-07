"""``--validate-schemas`` CLI subcommand (plan 70 decision 1).

Validates every discovered schema -- bundled and drop-in -- via the exact
same ``registry.build_registry`` discovery function the running server uses,
so the CLI and the server can never disagree about what is valid. Lets a
third-party schema author check their drop-in without starting the server.

What this module does not do:
- It does not reimplement any discovery, meta-schema, or rejection-rule
  logic -- that is entirely ``config_editor.registry``'s job.
"""
from __future__ import annotations

from pathlib import Path

from config_editor import registry


def validate_schemas(config_dir: Path) -> int:
    """Run discovery against ``config_dir`` and print one line per schema file.

    Args:
        config_dir: Directory whose ``schemas/`` subdirectory is scanned for
            drop-in schema files (SPEC.md §2). Bundled schemas are always
            included, regardless of ``config_dir``.

    Returns:
        0 if every discovered schema file loaded successfully, 1 if any file
        was rejected.
    """
    try:
        result = registry.build_registry(
            bundled_dir=registry.BUNDLED_SCHEMA_DIR,
            dropin_dir=config_dir / "schemas",
        )
    except registry.BundledSchemaCollisionError as exc:
        print(f"REJECTED <bundled schemas>: {exc}")
        return 1

    for entry_id in sorted(result.entries):
        print(f"OK {entry_id}")

    ok = True
    for problem in sorted(result.problems, key=lambda p: p.source):
        print(f"REJECTED {problem.source}: {problem.reason}")
        ok = False

    return 0 if ok else 1
