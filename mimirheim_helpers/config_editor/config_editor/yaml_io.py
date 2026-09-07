"""Comment-preserving, atomic YAML file writing.

This module has no HTTP dependencies and no knowledge of the schema
registry. It exists so that both ``server.py`` (HTTP handlers) and
``registry.py`` (pure discovery/validation/save logic, SPEC.md §8) can write
a YAML file the same way without either one importing the other.

The implementation originates from the private ``_write_yaml_preserving_
comments`` helper ``server.py`` carried since the config editor's original
HTTP-only implementation; it was moved here so ``registry.py`` can depend on
it without pulling in ``http.server`` and the rest of ``server.py``'s
HTTP-handling code. ``server.py`` now imports both functions from here
instead of carrying its own copy.

What this module does not do:
- It does not decide *what* to write or *whether* a write is safe (path
  containment, request validation). Callers are responsible for that.
- It does not perform any HTTP or network I/O.
"""
from __future__ import annotations

import io
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML


def render_yaml_preserving_comments(data: dict[str, Any], file_path: Path) -> str:
    """Compute the comment-preserving YAML text for ``data``, without touching disk.

    This is the pure merge step :func:`write_yaml_preserving_comments` performs
    before its atomic write. It is factored out so a dry-run caller (e.g.
    ``POST /api/preview``, SPEC.md §12) can compute exactly what a save would
    write -- to diff it against the current file -- without duplicating the
    comment-preserving merge logic and risking drift between the two.

    If ``file_path`` exists, its content is loaded with ruamel.yaml to
    preserve comments, updated in-place from ``data``, and re-serialised. If
    it does not exist, ``data`` is serialised fresh.

    Args:
        data: Dictionary to render as YAML.
        file_path: Path the YAML would be written to. Read (not written) to
            recover comments and formatting from the existing file, if any.

    Returns:
        The rendered YAML string.
    """
    yaml_handler = YAML()
    yaml_handler.default_flow_style = False
    yaml_handler.preserve_quotes = True
    yaml_handler.width = 4096  # Prevent line wrapping

    if file_path.exists():
        # Load existing file to preserve comments and structure.
        try:
            with file_path.open("r") as f:
                existing = yaml_handler.load(f)
        except Exception:
            # ruamel can raise a variety of parser/scanner exceptions for a
            # malformed or unreadable file; any of them means "treat as
            # absent" here, not a crash. File is malformed or unreadable:
            # write fresh.
            existing = None

        if existing is not None:
            # Deep merge: update existing structure with new values.
            def deep_merge(target: Any, source: dict) -> None:
                """Recursively update target dict with values from source.

                Updates values, adds new keys, and removes keys not in source.
                """
                if not isinstance(target, dict) or not isinstance(source, dict):
                    return

                # Remove keys that are in target but not in source.
                keys_to_remove = [k for k in target.keys() if k not in source]
                for key in keys_to_remove:
                    del target[key]

                # Update or add keys from source.
                for key, value in source.items():
                    if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                        deep_merge(target[key], value)
                    else:
                        target[key] = value

            deep_merge(existing, data)
            merged = existing
        else:
            # File was empty or malformed.
            merged = data
    else:
        # New file: just use the provided data.
        merged = data

    # Write to string first to get the output for logging.
    stream = io.StringIO()
    yaml_handler.dump(merged, stream)
    return stream.getvalue()


def write_yaml_preserving_comments(data: dict[str, Any], file_path: Path) -> str:
    """Write YAML file while preserving existing comments and formatting.

    Computes the merged content via :func:`render_yaml_preserving_comments`,
    then writes it atomically: a temp file is written in the same directory,
    then ``os.replace`` moves it into place.

    Args:
        data: Dictionary to write as YAML.
        file_path: Path where the YAML file will be written.

    Returns:
        The YAML string that was written.

    Raises:
        OSError: If the atomic replace step fails. The temporary file is
            cleaned up before the exception propagates.
    """
    yaml_str = render_yaml_preserving_comments(data, file_path)

    fd, tmp_path = tempfile.mkstemp(dir=file_path.parent, suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(yaml_str)
        os.replace(tmp_path, file_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return yaml_str
