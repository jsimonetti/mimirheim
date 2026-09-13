"""Atomic, comment-preserving YAML writes shared by every Config Owner.

Every Config Owner's own ``validate_and_write`` implementation uses this
utility to persist a validated set of Candidate Values to its configuration
file. It is not reimplemented per Config Owner.

It merges nested mappings key by key so that comments on sibling keys
survive an update to one field, but it does not attempt the same for
list-valued fields: a list in the incoming values replaces the existing
list outright, rather than being merged item by item.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096


def write_yaml_preserving_comments(file_path: Path, values: dict[str, Any]) -> None:
    """Atomically overlay ``values`` onto the YAML file at ``file_path``.

    Loads the existing file, if any, with ruamel.yaml's round-trip loader so
    comments and key order survive, overlays ``values`` onto it (recursing
    into matching nested dicts, replacing everything else), then writes the
    result to a temp file in the same directory and renames it over the
    original. The rename is atomic on POSIX filesystems: a crash mid-write
    leaves the original file untouched rather than truncated.

    Args:
        file_path: The YAML file to update. Created if it does not exist.
        values: The values to overlay onto the existing document.

    Raises:
        OSError: If writing the temp file or renaming it over ``file_path``
            fails. The original file is left untouched and the temp file is
            removed.
    """
    if file_path.exists():
        with file_path.open("r") as fh:
            document = _yaml.load(fh)
        if document is None:
            document = {}
    else:
        document = {}

    _overlay(document, values)

    fd, tmp_name = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            _yaml.dump(document, fh)
        os.replace(tmp_path, file_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _overlay(document: Any, values: dict[str, Any]) -> None:
    """Recursively write ``values`` into ``document``, preserving comments/order.

    A key present as a dict in both ``document`` and ``values`` is merged
    recursively rather than replaced outright, so unrelated sibling keys (and
    their comments) under that key survive an update to one of its fields.
    """
    for key, value in values.items():
        if isinstance(document.get(key), dict) and isinstance(value, dict):
            _overlay(document[key], value)
        else:
            document[key] = value
