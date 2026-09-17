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

from pydantic import BaseModel
from ruamel.yaml import YAML

from mimirheim_shared.field_shape import FieldShape, derive_field_shape, nested_model_of

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096


def write_yaml_preserving_comments(
    file_path: Path, values: dict[str, Any], model: type[BaseModel] | None = None
) -> None:
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
        model: The Config Owner's validation model, passed through to
            ``overlay_values`` so a Named Collection field is replaced
            wholesale rather than merged key by key. See ``overlay_values``.

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

    overlay_values(document, values, model)

    fd, tmp_name = tempfile.mkstemp(dir=file_path.parent, suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            _yaml.dump(document, fh)
        os.replace(tmp_path, file_path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def overlay_values(document: Any, values: dict[str, Any], model: type[BaseModel] | None = None) -> None:
    """Recursively write ``values`` into ``document`` in place.

    A key present as a dict (or ``dict``-like mapping, e.g. a ruamel
    ``CommentedMap``) in both ``document`` and ``values`` is merged
    recursively rather than replaced outright, so unrelated sibling keys (and,
    for a ruamel document, their comments) under that key survive an update to
    one of its fields. Public so a Config Owner's ``validate_and_write`` can
    also merge Candidate Values onto its current on-disk configuration before
    validating: Candidate Values may be a partial update (see ``values``
    above), and validating them in isolation would reject an update to one
    field of an otherwise-required nested section.

    A Named Collection field (a ``Dict[str, Model]`` field on ``model``) gets
    one further rule on top of that merge: Candidate Values for it always
    carry the complete surviving set of entries (ADR-0008), so any on-disk
    entry key missing from ``values`` is dropped from ``document`` -- an
    entry removed or renamed in the Config Editor must actually disappear on
    write, not be left behind forever by an ordinary key-by-key merge. An
    entry key present in both is still merged recursively rather than
    replaced outright, so an untouched sibling field (and its comment) on a
    surviving entry is preserved exactly like any other nested object's
    field would be; only a genuinely stale key is dropped. This distinction
    needs ``model``: a plain dict cannot tell a Named Collection's own
    entries apart from a nested object's own fields on structure alone.

    Args:
        document: The mapping to update in place. Typically either a plain
            ``dict`` (validation) or a ruamel round-trip-loaded document
            (writing).
        values: The values to overlay onto ``document``.
        model: The pydantic model ``document``/``values`` are shaped like, at
            this recursion depth. None (the default) preserves the original,
            shape-unaware merge-every-dict behaviour, e.g. for a caller with
            no model context.
    """
    for key, value in values.items():
        model_field = model.model_fields.get(key) if model is not None else None
        shape = derive_field_shape(model_field.annotation) if model_field is not None else None
        nested_model = nested_model_of(model_field.annotation) if model_field is not None else None

        if not isinstance(document.get(key), dict) or not isinstance(value, dict):
            document[key] = value
            continue

        existing = document[key]
        if shape is FieldShape.NAMED_COLLECTION:
            _overlay_named_collection_entries(existing, value, nested_model)
            continue

        overlay_values(existing, value, nested_model)


def _overlay_named_collection_entries(
    existing: Any, entries: dict[str, Any], entry_model: type[BaseModel] | None
) -> None:
    """Overlay a Named Collection's own entries dict onto ``existing`` in place.

    An entry key is not a field name of any pydantic model (it is the
    Named Collection's own dict key, e.g. a battery's name), so it cannot be
    dispatched through ``overlay_values``'s own model-field lookup the way an
    ordinary nested object's fields are: this is a dedicated helper rather
    than a recursive ``overlay_values`` call on ``entries`` itself.

    Args:
        existing: The Named Collection's current on-disk entries dict.
        entries: The complete surviving set of entries from Candidate Values
            (ADR-0008).
        entry_model: The Named Collection's own item model (e.g. the battery
            model), used so a surviving entry's own nested structure is
            still merged correctly, not just shallowly.
    """
    for stale_key in [entry_key for entry_key in existing if entry_key not in entries]:
        del existing[stale_key]

    for entry_key, entry_value in entries.items():
        if isinstance(existing.get(entry_key), dict) and isinstance(entry_value, dict):
            overlay_values(existing[entry_key], entry_value, entry_model)
        else:
            existing[entry_key] = entry_value

