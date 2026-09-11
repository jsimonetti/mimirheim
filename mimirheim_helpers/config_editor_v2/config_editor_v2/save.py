"""Validation and atomic, comment-preserving save for config-editor-v2.

This module implements the save path's core guarantee, described in
mimirheim_helpers/config_editor_v2/IMPLEMENTATION_DETAILS.md under "Why
validation always goes through the real Pydantic model" and "Save
semantics": every registered configuration is validated against its real
Pydantic model before any file is written, validation failures are
attributed to the specific registry entry and field they came from, and
each file that does get written is written atomically with any existing
comments on disk preserved.

This module does not decide *when* to write. `validate_all` and `write_all`
are two independent steps; the caller (the HTTP server built in step 71_5)
is responsible for calling `write_all` only after confirming `validate_all`
returned no errors. This module also does not know about the adapter
(`adapter.py`) or any rendering library -- the data it validates has
already been run through the adapter's incoming-data transform by the
caller.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from .registry import RegistryEntry, resolve_model


class FieldError(BaseModel):
    """One validation failure, attributed to its source.

    Attributes:
        entry_name: The RegistryEntry.name this error came from.
        loc: The Pydantic error location tuple, as strings, identifying the
            specific field path within that entry's model.
        message: The human-readable error message from Pydantic.
    """

    model_config = ConfigDict(extra="forbid")

    entry_name: str
    loc: list[str]
    message: str


def validate_all(
    entries: list[RegistryEntry],
    submitted: dict[str, dict[str, Any]],
) -> tuple[dict[str, BaseModel], list[FieldError]]:
    """Validates every registered entry's submitted data against its model.

    `submitted` maps a RegistryEntry.name to that entry's data dict, already
    run through the adapter's incoming-data transform. An entry with no key
    in `submitted` is validated against its model's own defaults, per
    IMPLEMENTATION_DETAILS.md's requirement that an untouched configuration
    must still validate successfully.

    Every entry is validated independently, regardless of whether an
    earlier entry failed: this is what lets errors be reported for all
    invalid entries at once, rather than only the first one encountered.

    Args:
        entries: The registry entries to validate, in registration order.
        submitted: Submitted data dicts, keyed by RegistryEntry.name.

    Returns:
        A tuple of (validated models keyed by entry name, accumulated field
        errors). If the error list is non-empty, the validated-models dict
        must be treated as unusable for writing -- every entry is validated,
        but nothing is written, even the entries that individually passed.

    Raises:
        ImportError: Propagated from `resolve_model` if a registry entry's
            model cannot be imported. This is a configuration bug in the
            registry itself, not a user validation failure, and must not be
            caught and converted into a FieldError.
        AttributeError: Propagated from `resolve_model` if a registry
            entry's module has no class of the named name. Same reasoning
            as the ImportError case above.
        TypeError: Propagated from `resolve_model` if the resolved object is
            not a Pydantic BaseModel subclass. Same reasoning as above.
    """
    validated: dict[str, BaseModel] = {}
    errors: list[FieldError] = []

    for entry in entries:
        # Not caught here: a broken registry entry is this editor's own
        # configuration bug, not something a user's submission can cause.
        model_cls = resolve_model(entry)
        data = submitted.get(entry.name, {})
        try:
            validated[entry.name] = model_cls.model_validate(data)
        except ValidationError as exc:
            for error in exc.errors():
                errors.append(
                    FieldError(
                        entry_name=entry.name,
                        loc=[str(part) for part in error["loc"]],
                        message=error["msg"],
                    )
                )

    return validated, errors


def write_all(
    validated: dict[str, BaseModel],
    entries: list[RegistryEntry],
    config_dir: Path,
) -> None:
    """Writes every validated model to its registered YAML file, atomically.

    Each file is written independently: `model_dump()` to a dict, merged
    into the existing on-disk YAML via ruamel.yaml's round-trip loader (so
    existing comments and key order survive), written to a temp file in the
    same directory, then renamed over the target. A failure partway through
    writing one file must not leave that file's temp artefact behind, but
    per Save semantics, `write_all` is only ever called after `validate_all`
    has confirmed every entry passes -- this function does not itself decide
    whether to write; the caller (71_5's server) is responsible for calling
    it only when the error list from `validate_all` is empty.

    Args:
        validated: Validated models keyed by RegistryEntry.name, as returned
            by `validate_all`.
        entries: The registry entries naming the YAML filename each model in
            `validated` writes to.
        config_dir: Directory the registered filenames are relative to.
    """
    entries_by_name = {entry.name: entry for entry in entries}
    for name, model in validated.items():
        entry = entries_by_name[name]
        _write_yaml_preserving_comments(model.model_dump(), config_dir / entry.filename)


def _write_yaml_preserving_comments(data: dict[str, Any], file_path: Path) -> None:
    """Merges `data` into `file_path`'s existing YAML and writes it atomically.

    If `file_path` already exists and parses as valid YAML, its structure is
    loaded with ruamel.yaml's round-trip loader and updated in place with
    `data`, so comments and key order already on disk survive. If the file
    does not exist yet, `data` is written as a fresh document.

    A pre-existing file that fails to parse is treated as absent (a fresh
    document is written from `data`, discarding whatever malformed content
    was there). This is deliberate, not a swallowed bug: `write_all` is only
    ever called once `validate_all` has confirmed the *submitted* data is
    valid, but that guarantee says nothing about the state of a file already
    on disk, which could have been hand-edited into a broken state by an
    operator outside this editor entirely. `ruamel.yaml.error.YAMLError` is
    caught specifically -- not a bare `except Exception` -- so any other
    failure while reading the file (for example, a permissions error) still
    propagates.

    Args:
        data: The dict to write, from a validated model's `model_dump()`.
        file_path: Destination YAML file. Must live in a directory that
            already exists and is writable.
    """
    yaml = YAML()
    yaml.default_flow_style = False
    yaml.preserve_quotes = True

    existing: Any = None
    if file_path.exists():
        try:
            with file_path.open("r") as existing_file:
                existing = yaml.load(existing_file)
        except YAMLError:
            existing = None

    if isinstance(existing, dict):
        _deep_merge(existing, data)
        merged = existing
    else:
        merged = data

    file_descriptor, tmp_path_str = tempfile.mkstemp(
        dir=file_path.parent, suffix=".yaml.tmp"
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(file_descriptor, "w") as tmp_file:
            yaml.dump(merged, tmp_file)
        os.replace(tmp_path, file_path)
    except OSError:
        try:
            tmp_path.unlink()
        except OSError:
            # The temp file may already be gone (for example, if the
            # failure happened after os.replace partially completed on some
            # platforms). Either way, there is nothing left to clean up.
            pass
        raise


def _deep_merge(target: dict[str, Any], source: dict[str, Any]) -> None:
    """Recursively updates `target` in place with values from `source`.

    Keys present in `target` but absent from `source` are removed, so the
    written file reflects exactly the validated model's fields, not stale
    leftovers from a previous version of the file. Nested dicts are merged
    recursively rather than replaced outright, which is what lets ruamel's
    comment-carrying structure for unchanged nested keys survive the merge.

    Args:
        target: The ruamel round-trip-loaded mapping to update in place.
        source: The new data to merge in, from a validated model's
            `model_dump()`.
    """
    for key in [key for key in target if key not in source]:
        del target[key]

    for key, value in source.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value
