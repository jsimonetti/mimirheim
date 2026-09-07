"""Schema discovery, validation, and save logic for the config editor.

Implements the parts of ``SPEC.md`` that do not depend on HTTP: discovery of
bundled and drop-in ``*.schema.json`` files (§2), the ``x-mimirheim`` root
envelope (§3), document composition (§5), the two-pass validation model (§7),
and validate-all-then-write-all save semantics (§8).

This module has no HTTP imports and no knowledge of ``ConfigEditorServer``.
Every function here is independently unit-testable against fixture
directories; wiring it onto HTTP endpoints is a separate module's job.

What this module does not do:
- It does not serve HTTP requests or know about request/response shapes
  beyond the plain dicts SPEC.md §12 describes.
- It does not implement the ``/api/preview`` dry-run behaviour; that is
  built on top of :func:`save_entries` by whichever module wires the HTTP
  API, by swapping the ``write_yaml`` callback for one that never touches
  disk.
- It does not cache anything across calls. Callers that want a stable view
  across a request (or that want to avoid re-reading files on every call)
  are responsible for that.
"""
from __future__ import annotations

import functools
import importlib
import json
import logging
import re
from collections.abc import Callable, Mapping, Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import ValidationError as PydanticValidationError

from config_editor.yaml_io import write_yaml_preserving_comments

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Limits (SPEC.md §13)
# ---------------------------------------------------------------------------

MAX_SCHEMA_FILE_BYTES = 256 * 1024
MAX_SCHEMA_NESTING_DEPTH = 20
MAX_SCHEMA_PROPERTY_COUNT = 2000

# ---------------------------------------------------------------------------
# x-mimirheim envelope constants (SPEC.md §3)
# ---------------------------------------------------------------------------

_JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

_VALID_CATEGORIES = frozenset(
    {"core", "prices", "pv", "baseload", "scheduling", "reporting", "other"}
)

_ENVELOPE_REQUIRED_KEYS = frozenset({"file", "category", "python_package"})
_ENVELOPE_KNOWN_KEYS = frozenset(
    {
        "file",
        "category",
        "order",
        "python_package",
        "python_model",
        "exclusive_group",
        "required",
        "docs_url",
    }
)

# x-mimirheim.file must never point at the editor's own live configuration
# (SPEC.md §3).
_CONFIG_EDITOR_OWN_FILE = "config-editor.yaml"

# SPEC.md §3's exact pattern for x-mimirheim.file.
_FILE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]*\.yaml$")

# Where the id (the filename stem) itself is used for.
_SCHEMA_FILE_SUFFIX = ".schema.json"

# Default locations, relative to this package. Discovery functions below take
# a directory argument rather than hardcoding these, so they stay
# independently unit-testable against fixture directories (Decision 1); these
# constants are for whichever module wires the real server up.
PACKAGE_DIR = Path(__file__).parent
BUNDLED_SCHEMA_DIR = PACKAGE_DIR / "schemas" / "bundled"
META_SCHEMA_PATH = PACKAGE_DIR / "schemas" / "_meta" / "mimirheim-helper-schema.meta.json"

# The YAML filename mimirheim.yaml's own entry uses. Its entry gets no
# injected `context` subtree (SPEC.md §5) and its file is the source of
# `context`, not a consumer of it.
MIMIRHEIM_YAML_FILE = "mimirheim.yaml"

# Matches MimirheimConfig.mqtt.topic_prefix's own default (mimirheim/config/
# schema.py). Used only when mimirheim.yaml is absent or omits mqtt entirely.
_DEFAULT_MQTT_TOPIC_PREFIX = "mimir"

# The read-only `context` subtree injected into every non-mimirheim.yaml
# entry's schema (SPEC.md §5).
_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "readOnly": True,
    "properties": {
        "mqtt_topic_prefix": {"type": "string"},
        "pv_arrays": {"type": "object", "additionalProperties": {"type": "object"}},
        "static_loads": {"type": "object", "additionalProperties": {"type": "object"}},
    },
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base class for every exception this module raises."""


class BundledSchemaCollisionError(RegistryError):
    """Two bundled schemas declare the same id.

    This cannot happen through user input -- every bundled schema is
    generated 1:1 from one Pydantic model with a deliberately chosen
    filename, and the generator's own drift test catches a duplicate before
    it is committed. If it happens anyway, it is a packaging bug, and the
    server fails loudly at startup rather than silently picking one
    (SPEC.md §2).
    """


class UnknownEntryError(RegistryError):
    """A save/preview request referenced an id the registry does not know."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class XMimirheim:
    """The parsed, validated ``x-mimirheim`` root envelope (SPEC.md §3)."""

    file: str
    category: str
    order: int
    python_package: str
    python_model: str | None
    exclusive_group: str | None
    required: bool
    docs_url: str | None


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One successfully discovered and validated schema entry.

    Attributes:
        id: The schema file's filename stem, e.g. ``"nordpool"``.
        envelope: The parsed ``x-mimirheim`` envelope.
        schema: The full raw schema document, exactly as parsed from disk
            (including ``x-mimirheim``). Does not include the injected
            ``context`` subtree -- see :func:`compose_entry_schema`.
        source_filename: The bare schema filename (no directory component),
            used only in rejection/collision messages -- never an absolute
            path (SPEC.md §10).
        is_bundled: Whether this entry came from the bundled schema
            directory (as opposed to a drop-in). Gates the second,
            Pydantic validation pass (SPEC.md §7) and the id-collision rule
            (SPEC.md §2).
    """

    id: str
    envelope: XMimirheim
    schema: Mapping[str, Any]
    source_filename: str
    is_bundled: bool


@dataclass(frozen=True, slots=True)
class RejectedSchema:
    """A schema file that failed to load, and why (SPEC.md §10).

    Attributes:
        source: The bare filename that was rejected -- never an absolute
            path.
        reason: A human-readable, actionable reason naming the offending
            key or file.
    """

    source: str
    reason: str


@dataclass(frozen=True, slots=True)
class Registry:
    """The result of discovery: every entry that loaded, and every problem.

    Attributes:
        entries: Mapping of id to :class:`RegistryEntry`, for every schema
            that passed discovery and validation.
        problems: Every schema that was rejected, bundled or drop-in,
            recorded rather than raised (SPEC.md §2), except for the
            bundled-vs-bundled id collision, which is a hard startup error
            (see :class:`BundledSchemaCollisionError`).
    """

    entries: Mapping[str, RegistryEntry]
    problems: tuple[RejectedSchema, ...]

    def get(self, entry_id: str) -> RegistryEntry | None:
        """Return the entry for ``entry_id``, or ``None`` if unknown."""
        return self.entries.get(entry_id)


@dataclass(frozen=True, slots=True)
class SaveResult:
    """The outcome of :func:`save_entries` (SPEC.md §8).

    Attributes:
        ok: Whether every dirty entry validated and was written/deleted.
        errors: Mapping of id to that entry's error list, for entries that
            failed validation. Empty when ``ok`` is True.
        written: The bare filenames that were written, in submission order.
            Empty when ``ok`` is False -- save is validate-all-then-write-all.
        deleted: The bare filenames that were deleted, in the order they
            were deleted. Includes both explicit ``enabled: false`` entries
            and exclusive-group siblings removed as a side effect.
    """

    ok: bool
    errors: Mapping[str, list[dict[str, Any]]] = field(default_factory=dict)
    written: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Meta-schema (SPEC.md §10 rule 3)
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _meta_validator() -> Draft202012Validator:
    """Return the (cached) validator for the committed meta-schema file."""
    meta_schema = json.loads(META_SCHEMA_PATH.read_text())
    return Draft202012Validator(meta_schema)


# ---------------------------------------------------------------------------
# x-mimirheim envelope parsing (SPEC.md §10 rules 4-9)
# ---------------------------------------------------------------------------


def _parse_envelope(doc: Mapping[str, Any]) -> XMimirheim | str:
    """Validate and parse ``doc["x-mimirheim"]``.

    Implements SPEC.md §10 rules 5 through 9 as explicit, individually
    testable checks with their own actionable message, rather than folding
    them into the generic meta-schema failure (rule 3).

    Args:
        doc: The full parsed schema document.

    Returns:
        An :class:`XMimirheim` on success, or a human-readable rejection
        reason string naming the offending key.
    """
    envelope = doc.get("x-mimirheim")
    if envelope is None:
        return (
            "x-mimirheim is missing; every mimirheim helper schema must "
            "declare an x-mimirheim envelope (SPEC.md §3)."
        )
    if not isinstance(envelope, dict):
        return f"x-mimirheim must be a JSON object, got {type(envelope).__name__}."

    missing = _ENVELOPE_REQUIRED_KEYS - envelope.keys()
    if missing:
        return f"x-mimirheim is missing required key(s): {', '.join(sorted(missing))}."

    unknown = set(envelope.keys()) - _ENVELOPE_KNOWN_KEYS
    if unknown:
        return f"x-mimirheim has unknown key(s): {', '.join(sorted(unknown))}."

    file_value = envelope["file"]
    if not isinstance(file_value, str):
        return "x-mimirheim.file must be a string."
    if "/" in file_value or "\\" in file_value:
        return (
            f"x-mimirheim.file {file_value!r} contains a path separator; "
            "it must be a bare filename with no directory component."
        )
    if not _FILE_PATTERN.match(file_value):
        return (
            f"x-mimirheim.file {file_value!r} does not match the required "
            f"pattern {_FILE_PATTERN.pattern!r}."
        )
    if file_value == _CONFIG_EDITOR_OWN_FILE:
        return (
            f"x-mimirheim.file must not equal {_CONFIG_EDITOR_OWN_FILE!r}; "
            "config-editor does not edit its own live configuration."
        )

    category_value = envelope["category"]
    if category_value not in _VALID_CATEGORIES:
        return (
            f"x-mimirheim.category {category_value!r} is not one of "
            f"{sorted(_VALID_CATEGORIES)}."
        )

    python_package = envelope["python_package"]
    if not isinstance(python_package, str) or not python_package:
        return "x-mimirheim.python_package must be a non-empty string."

    order_value = envelope.get("order", 0)
    if not isinstance(order_value, int) or isinstance(order_value, bool):
        return "x-mimirheim.order must be an integer."

    python_model = envelope.get("python_model")
    if python_model is not None and not isinstance(python_model, str):
        return "x-mimirheim.python_model must be a string or null."

    exclusive_group = envelope.get("exclusive_group")
    if exclusive_group is not None and not isinstance(exclusive_group, str):
        return "x-mimirheim.exclusive_group must be a string or null."

    required_value = envelope.get("required", False)
    if not isinstance(required_value, bool):
        return "x-mimirheim.required must be a boolean."

    docs_url = envelope.get("docs_url")
    if docs_url is not None and (not isinstance(docs_url, str) or not docs_url.startswith("https://")):
        return "x-mimirheim.docs_url must be an https:// URL, or null."

    return XMimirheim(
        file=file_value,
        category=category_value,
        order=order_value,
        python_package=python_package,
        python_model=python_model,
        exclusive_group=exclusive_group,
        required=required_value,
        docs_url=docs_url,
    )


# ---------------------------------------------------------------------------
# Structural checks (SPEC.md §10 rules 11-13)
# ---------------------------------------------------------------------------


def _find_non_local_refs(node: Any) -> list[str]:
    """Return every ``$ref`` value in ``node`` that does not start with ``#/``."""
    found: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#/"):
                found.append(ref)
            for child in value.values():
                _walk(child)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk(node)
    return found


def _max_nesting_depth(node: Any) -> int:
    """Return the maximum dict/list nesting depth of ``node``, root counted as 1."""
    if isinstance(node, dict):
        if not node:
            return 1
        return 1 + max(_max_nesting_depth(v) for v in node.values())
    if isinstance(node, list):
        if not node:
            return 1
        return 1 + max(_max_nesting_depth(v) for v in node)
    return 0


def _count_properties(doc: Mapping[str, Any]) -> int:
    """Return the total property count: root ``properties`` plus every ``$defs`` entry's."""
    total = len(doc.get("properties") or {})
    defs = doc.get("$defs") or {}
    for def_schema in defs.values():
        if isinstance(def_schema, dict):
            total += len(def_schema.get("properties") or {})
    return total


# ---------------------------------------------------------------------------
# Per-file loading (SPEC.md §10 rules 1-9, 11-13)
# ---------------------------------------------------------------------------


def _load_single_schema(path: Path, *, is_bundled: bool) -> RegistryEntry | RejectedSchema:
    """Load, parse, and validate one schema file against every per-file rejection rule.

    Does not handle id collisions (SPEC.md §10 rule 10) -- that requires
    cross-file context and is handled by :func:`discover_bundled` and
    :func:`discover_dropins`.

    Args:
        path: Path to the ``*.schema.json`` file.
        is_bundled: Whether this file came from the bundled schema directory.

    Returns:
        A :class:`RegistryEntry` on success, or a :class:`RejectedSchema`
        naming the offending key or file.
    """
    name = path.name

    try:
        size = path.stat().st_size
    except OSError as exc:
        return RejectedSchema(name, f"could not read schema file: {exc}")
    if size > MAX_SCHEMA_FILE_BYTES:
        return RejectedSchema(
            name,
            f"schema file is {size} bytes, exceeding the {MAX_SCHEMA_FILE_BYTES} "
            "byte limit (SPEC.md §13).",
        )

    try:
        raw_text = path.read_text()
    except OSError as exc:
        return RejectedSchema(name, f"could not read schema file: {exc}")

    try:
        doc = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return RejectedSchema(name, f"not valid JSON: {exc}")

    # A non-object root (e.g. a bare JSON list or string) fails the
    # meta-schema's own "type": "object" requirement on the instance below,
    # so no separate check is needed here.
    meta_errors = sorted(_meta_validator().iter_errors(doc), key=lambda e: list(e.absolute_path))
    if meta_errors:
        first = meta_errors[0]
        loc = "/".join(str(p) for p in first.absolute_path) or "<root>"
        return RejectedSchema(name, f"fails meta-schema validation at {loc!r}: {first.message}")

    if not isinstance(doc, dict):
        # Unreachable in practice (the meta-schema above already rejects a
        # non-object root), but keeps the type checker -- and any future
        # loosening of the meta-schema -- honest about what follows.
        return RejectedSchema(name, f"schema root must be a JSON object, got {type(doc).__name__}.")

    if doc.get("type") != "object":
        return RejectedSchema(name, f'root "type" must be "object", got {doc.get("type")!r}.')

    envelope_or_reason = _parse_envelope(doc)
    if isinstance(envelope_or_reason, str):
        return RejectedSchema(name, envelope_or_reason)
    envelope = envelope_or_reason

    non_local_refs = _find_non_local_refs(doc)
    if non_local_refs:
        return RejectedSchema(
            name,
            f"contains a non-local $ref: {non_local_refs[0]!r}; only \"#/...\" "
            "references are allowed (SPEC.md §11).",
        )

    depth = _max_nesting_depth(doc)
    if depth > MAX_SCHEMA_NESTING_DEPTH:
        return RejectedSchema(
            name,
            f"schema nesting depth {depth} exceeds the limit of "
            f"{MAX_SCHEMA_NESTING_DEPTH} (SPEC.md §13).",
        )

    prop_count = _count_properties(doc)
    if prop_count > MAX_SCHEMA_PROPERTY_COUNT:
        return RejectedSchema(
            name,
            f"schema declares {prop_count} properties, exceeding the limit of "
            f"{MAX_SCHEMA_PROPERTY_COUNT} (SPEC.md §13).",
        )

    entry_id = name.removesuffix(_SCHEMA_FILE_SUFFIX)
    return RegistryEntry(id=entry_id, envelope=envelope, schema=doc, source_filename=name, is_bundled=is_bundled)


# ---------------------------------------------------------------------------
# Discovery (SPEC.md §2)
# ---------------------------------------------------------------------------


def discover_bundled(directory: Path) -> tuple[dict[str, RegistryEntry], list[RejectedSchema]]:
    """Discover and validate every bundled schema file.

    Files are processed in sorted filename order for determinism. A file
    that fails any per-file rejection rule (SPEC.md §10 rules 1-9, 11-13) is
    recorded as a problem and never prevents another file from loading.

    Args:
        directory: The bundled schema directory (``schemas/bundled/``).

    Returns:
        A tuple of (entries by id, rejected schemas).

    Raises:
        BundledSchemaCollisionError: If two bundled schemas declare the same
            id (SPEC.md §2, §10 rule 10) -- a packaging bug, not user input.
    """
    entries: dict[str, RegistryEntry] = {}
    problems: list[RejectedSchema] = []
    if not directory.exists():
        return entries, problems

    for path in sorted(directory.glob(f"*{_SCHEMA_FILE_SUFFIX}")):
        result = _load_single_schema(path, is_bundled=True)
        if isinstance(result, RejectedSchema):
            problems.append(result)
            logger.warning("Rejected bundled schema %s: %s", result.source, result.reason)
            continue
        if result.id in entries:
            raise BundledSchemaCollisionError(
                f"bundled schemas {entries[result.id].source_filename!r} and "
                f"{result.source_filename!r} both declare id {result.id!r}; "
                "this is a packaging bug, not user input."
            )
        entries[result.id] = result

    return entries, problems


def discover_dropins(
    directory: Path, *, bundled_ids: AbstractSet[str]
) -> tuple[dict[str, RegistryEntry], list[RejectedSchema]]:
    """Discover and validate every drop-in schema file.

    Files are processed in sorted filename order, which is what makes id
    collisions between two drop-ins deterministic across restarts: the
    first file to claim an id wins, and every later one with the same id is
    rejected, naming the winning file. An id colliding with a bundled id is
    always rejected -- the bundled schema always wins (SPEC.md §2).

    A malformed drop-in never prevents a valid sibling from loading.

    Args:
        directory: The drop-in schema directory (``<config_dir>/schemas/``).
        bundled_ids: The set of ids already claimed by bundled schemas.

    Returns:
        A tuple of (entries by id, rejected schemas).
    """
    entries: dict[str, RegistryEntry] = {}
    problems: list[RejectedSchema] = []
    if not directory.exists():
        return entries, problems

    for path in sorted(directory.glob(f"*{_SCHEMA_FILE_SUFFIX}")):
        result = _load_single_schema(path, is_bundled=False)
        if isinstance(result, RejectedSchema):
            problems.append(result)
            logger.warning("Rejected drop-in schema %s: %s", result.source, result.reason)
            continue
        if result.id in bundled_ids:
            problems.append(
                RejectedSchema(
                    result.source_filename,
                    f"id {result.id!r} collides with a bundled schema; the bundled "
                    "schema always wins. Rename this file's stem to a different id.",
                )
            )
            logger.warning(
                "Rejected drop-in schema %s: id %r collides with a bundled schema.",
                result.source_filename,
                result.id,
            )
            continue
        if result.id in entries:
            winning = entries[result.id].source_filename
            problems.append(
                RejectedSchema(
                    result.source_filename,
                    f"id {result.id!r} is already claimed by {winning!r} (processed "
                    "first, in sorted filename order); rename this file's stem to a "
                    "different id.",
                )
            )
            logger.warning(
                "Rejected drop-in schema %s: id %r already claimed by %s.",
                result.source_filename,
                result.id,
                winning,
            )
            continue
        entries[result.id] = result

    return entries, problems


def build_registry(bundled_dir: Path, dropin_dir: Path | None = None) -> Registry:
    """Run full discovery (SPEC.md §2) and return the combined result.

    Args:
        bundled_dir: The bundled schema directory.
        dropin_dir: The drop-in schema directory, or ``None`` if there is
            none to scan (e.g. ``config_dir`` has no ``schemas/`` subdirectory
            yet).

    Returns:
        A :class:`Registry` combining bundled and drop-in entries, with
        every rejection recorded in ``problems``.

    Raises:
        BundledSchemaCollisionError: See :func:`discover_bundled`.
    """
    bundled_entries, bundled_problems = discover_bundled(bundled_dir)
    dropin_entries: dict[str, RegistryEntry] = {}
    dropin_problems: list[RejectedSchema] = []
    if dropin_dir is not None:
        dropin_entries, dropin_problems = discover_dropins(
            dropin_dir, bundled_ids=frozenset(bundled_entries)
        )
    merged = {**bundled_entries, **dropin_entries}
    return Registry(entries=merged, problems=tuple(bundled_problems) + tuple(dropin_problems))


# ---------------------------------------------------------------------------
# Document composition (SPEC.md §5)
# ---------------------------------------------------------------------------


def build_context(mimirheim_config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the read-only ``context`` data from mimirheim.yaml's parsed content.

    Args:
        mimirheim_config: The parsed content of mimirheim.yaml, or ``{}`` if
            the file is absent.

    Returns:
        A dict with ``mqtt_topic_prefix``, ``pv_arrays``, and
        ``static_loads``, always present -- ``pv_arrays``/``static_loads``
        default to an empty object rather than being omitted when
        mimirheim.yaml has none.
    """
    mqtt = mimirheim_config.get("mqtt")
    topic_prefix = (
        mqtt.get("topic_prefix", _DEFAULT_MQTT_TOPIC_PREFIX)
        if isinstance(mqtt, dict)
        else _DEFAULT_MQTT_TOPIC_PREFIX
    )
    pv_arrays = mimirheim_config.get("pv_arrays")
    static_loads = mimirheim_config.get("static_loads")
    return {
        "mqtt_topic_prefix": topic_prefix,
        "pv_arrays": pv_arrays if isinstance(pv_arrays, dict) else {},
        "static_loads": static_loads if isinstance(static_loads, dict) else {},
    }


def compose_entry_schema(entry: RegistryEntry) -> dict[str, Any]:
    """Return ``entry``'s schema with the read-only ``context`` subtree injected.

    ``mimirheim.yaml``'s own entry gets no ``context`` key back: it is the
    source, not a consumer, of this data (SPEC.md §5).

    Args:
        entry: The entry whose schema is being composed for rendering.

    Returns:
        A deep copy of ``entry.schema``, with a ``context`` property added
        under ``properties`` for every entry except ``mimirheim.yaml``'s own.
    """
    schema = json.loads(json.dumps(dict(entry.schema)))
    if entry.envelope.file == MIMIRHEIM_YAML_FILE:
        return schema
    properties = schema.setdefault("properties", {})
    properties["context"] = json.loads(json.dumps(_CONTEXT_SCHEMA))
    return schema


# ---------------------------------------------------------------------------
# Validation model (SPEC.md §7)
# ---------------------------------------------------------------------------


def _run_jsonschema_pass(schema: Mapping[str, Any], data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Validate ``data`` against ``schema`` with ``jsonschema`` and normalise errors."""
    validator = Draft202012Validator(dict(schema))
    errors = sorted(validator.iter_errors(dict(data)), key=lambda e: list(e.absolute_path))
    return [{"loc": list(error.absolute_path), "msg": error.message} for error in errors]


def _run_python_model_pass(dotted_path: str, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Construct the Pydantic model named by ``dotted_path`` from ``data``.

    Args:
        dotted_path: ``"module.path:ClassName"``.
        data: The submitted config dict.

    Returns:
        An empty list if the model constructs cleanly, otherwise the
        Pydantic errors mapped into the same ``{"loc": ..., "msg": ...}``
        shape the jsonschema pass produces (SPEC.md §7, Decision 7).
    """
    module_name, _, class_name = dotted_path.partition(":")
    if not module_name or not class_name:
        # A malformed python_model dotted path is a bundled-schema authoring
        # bug, not user input -- but it must not crash the request.
        logger.error("Malformed x-mimirheim.python_model dotted path: %r", dotted_path)
        return [{"loc": [], "msg": f"internal error: malformed python_model path {dotted_path!r}"}]

    try:
        module = importlib.import_module(module_name)
        model_cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        logger.error("Could not import python_model %r: %s", dotted_path, exc)
        return [{"loc": [], "msg": f"internal error: could not load validation model {dotted_path!r}"}]

    try:
        model_cls.model_validate(dict(data))
    except PydanticValidationError as exc:
        return [{"loc": list(item["loc"]), "msg": item["msg"]} for item in exc.errors()]
    return []


def validate_entry(entry: RegistryEntry, data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Run the SPEC.md §7 two-pass server-side validation model for one entry.

    The ``jsonschema`` pass always runs, for both bundled and drop-in
    entries. The second, Pydantic pass runs only when the entry is bundled
    *and* declares ``x-mimirheim.python_model`` -- a drop-in's
    ``python_model`` is ignored with a logged warning; drop-ins get
    structural validation only (SPEC.md §3, §7, §10, §11).

    Args:
        entry: The entry being validated.
        data: The submitted config dict for that entry (never includes
            ``context`` -- that key is stripped client-side before submit).

    Returns:
        An empty list if ``data`` is valid, otherwise a list of
        ``{"loc": [...], "msg": "..."}`` dicts. Both passes produce the same
        shape, so a caller never needs to know which one raised.
    """
    jsonschema_errors = _run_jsonschema_pass(entry.schema, data)
    if jsonschema_errors:
        return jsonschema_errors

    if not entry.envelope.python_model:
        return []

    if not entry.is_bundled:
        logger.warning(
            "Ignoring x-mimirheim.python_model %r on drop-in entry %r; drop-in "
            "schemas get structural (jsonschema) validation only (SPEC.md §7).",
            entry.envelope.python_model,
            entry.id,
        )
        return []

    return _run_python_model_pass(entry.envelope.python_model, data)


# ---------------------------------------------------------------------------
# Save semantics (SPEC.md §8)
# ---------------------------------------------------------------------------


def validate_save(
    registry: Registry, entries: Mapping[str, Mapping[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Run the validation model (SPEC.md §7) over every entry in a save/preview request.

    Args:
        registry: The assembled registry to validate against.
        entries: ``{"<id>": {"enabled": bool, "config": {...}}}``, the shape
            ``POST /api/save`` and ``POST /api/preview`` accept (SPEC.md
            §12). An entry with ``enabled: false`` needs no config and is
            never validated -- it is a deletion.

    Returns:
        Mapping of id to error list, for entries that failed validation.
        Empty when every entry is valid.

    Raises:
        UnknownEntryError: If ``entries`` references an id not present in
            ``registry``.
    """
    errors: dict[str, list[dict[str, Any]]] = {}
    for entry_id, spec in entries.items():
        entry = registry.get(entry_id)
        if entry is None:
            raise UnknownEntryError(entry_id)
        if not spec.get("enabled", True):
            continue
        config = spec.get("config") or {}
        entry_errors = validate_entry(entry, config)
        if entry_errors:
            errors[entry_id] = entry_errors
    return errors


def _default_delete_yaml(path: Path) -> None:
    """Delete ``path``. The default ``delete_yaml`` callback for :func:`write_entries`."""
    path.unlink()


def write_entries(
    registry: Registry,
    config_dir: Path,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    write_yaml: Callable[[dict[str, Any], Path], str] = write_yaml_preserving_comments,
    delete_yaml: Callable[[Path], None] = _default_delete_yaml,
) -> SaveResult:
    """Write (or delete) every entry in ``entries``, with no validation of its own.

    This is the write phase :func:`save_entries` runs after validation passes.
    It is exposed separately so a caller that must validate against a
    different view of the data than what gets written -- e.g. a
    Supervisor-supplied MQTT field merged in for validation only, never
    persisted to disk -- can call :func:`validate_save` itself against the
    merged view, then call this function with the unmerged one, without
    duplicating the write/exclusive-group logic below.

    Both ``write_yaml`` and ``delete_yaml`` are swappable so a caller can
    implement a dry run (e.g. ``POST /api/preview``, SPEC.md §12) that shares
    this exact merge/write/delete logic while never touching disk, instead of
    reimplementing it with drift risk against the real save path.

    An entry whose ``x-mimirheim.exclusive_group`` is set additionally
    deletes every other group member's YAML file that exists on disk, as
    part of the same call -- driven by the registry's own
    ``exclusive_group`` field, not a hardcoded list (Decision 6).

    True atomicity across multiple files is not attempted: the real
    ``write_yaml``'s own ``os.replace`` is atomic per file, but a crash
    between two files in the same call can leave a partial write. This is an
    accepted limitation (SPEC.md §8).

    Args:
        registry: The assembled registry ``entries`` is checked against.
        config_dir: The directory mimirheim's YAML files live in.
        entries: Same shape as :func:`validate_save`. Assumed already
            validated by the caller -- this function does not validate.
        write_yaml: The YAML-writing function to use for an ``enabled: true``
            entry. Defaults to the real, comment-preserving, atomic
            implementation.
        delete_yaml: The function to call to remove a file for an
            ``enabled: false`` entry, or for an exclusive-group sibling.
            Defaults to :func:`Path.unlink`.

    Returns:
        A :class:`SaveResult` with ``ok=True``, ``written``, and ``deleted``
        populated. ``errors`` is always empty -- this function does not
        validate.

    Raises:
        UnknownEntryError: If ``entries`` references an id not present in
            ``registry``.
    """
    written: list[str] = []
    deleted: list[str] = []

    for entry_id, spec in entries.items():
        entry = registry.get(entry_id)
        if entry is None:
            raise UnknownEntryError(entry_id)

        target = config_dir / entry.envelope.file
        if not spec.get("enabled", True):
            if target.exists():
                delete_yaml(target)
                deleted.append(entry.envelope.file)
            continue

        config = dict(spec.get("config") or {})
        write_yaml(config, target)
        written.append(entry.envelope.file)

        group = entry.envelope.exclusive_group
        if group is None:
            continue
        for other in registry.entries.values():
            if other.id == entry.id or other.envelope.exclusive_group != group:
                continue
            other_path = config_dir / other.envelope.file
            if other_path.exists():
                delete_yaml(other_path)
                deleted.append(other.envelope.file)

    return SaveResult(ok=True, written=tuple(written), deleted=tuple(deleted))


def save_entries(
    registry: Registry,
    config_dir: Path,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    write_yaml: Callable[[dict[str, Any], Path], str] = write_yaml_preserving_comments,
    delete_yaml: Callable[[Path], None] = _default_delete_yaml,
) -> SaveResult:
    """Validate-all-then-write-all the submitted dirty set (SPEC.md §8).

    If any dirty entry fails validation, nothing is written and the full
    per-entry error mapping is returned. Otherwise every entry is written
    (or, for ``enabled: false``, deleted) via :func:`write_entries`.

    Args:
        registry: The assembled registry to validate and save against.
        config_dir: The directory mimirheim's YAML files live in.
        entries: Same shape as :func:`validate_save`.
        write_yaml: The YAML-writing function to use. Defaults to the real,
            comment-preserving, atomic implementation; tests may substitute
            a fake to observe writes without touching disk.
        delete_yaml: The function to call to remove a file. Defaults to
            :func:`Path.unlink`; tests may substitute a fake.

    Returns:
        A :class:`SaveResult`. ``ok`` is False, with ``errors`` populated
        and nothing written, if any dirty entry failed validation.

    Raises:
        UnknownEntryError: See :func:`validate_save`.
    """
    errors = validate_save(registry, entries)
    if errors:
        return SaveResult(ok=False, errors=errors)

    return write_entries(registry, config_dir, entries, write_yaml=write_yaml, delete_yaml=delete_yaml)
