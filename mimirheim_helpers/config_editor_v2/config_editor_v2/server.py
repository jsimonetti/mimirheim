"""HTTP server for config-editor-v2.

This module provides `ConfigEditorV2Server`, a stdlib-only HTTP server that
wires together `registry.py`, `adapter.py`, and `save.py` behind a small
JSON API, and serves the vendored Jedison/Bootstrap frontend from `static/`:

    GET  /                          -- serve static/index.html
    GET  /static/<path>             -- serve a vendored or first-party static asset
    GET  /api/entries               -- every registered entry's adapted schema
    GET  /api/entries/<name>/data   -- one entry's current on-disk data, validated
                                       and defaulted against its model, its model
                                       defaults if no file exists yet, or an empty
                                       dict if neither is available (see
                                       _api_get_entry_data)
    POST /api/save                  -- validate every entry together, write all or nothing

No external web framework is required; only `http.server`, `json`, and the
already-built config-editor-v2 modules.

No transform is currently registered with `adapter.py` (see its own module
docstring): a field only ever changes shape by naming a transform explicitly
via `x-mimir-adapter`, and no field in any registered model does that today.
Until one is registered and opted into, `adapter.transform_schema_document`
and `adapter.transform_value_document` are pass-throughs. A field that needs
rendering-library-specific treatment with no shape change (a category, a
masked password input, and so on) carries that hint directly in its own
`json_schema_extra`, using Jedison's native vocabulary.

What this module does not do:
- It does not authenticate users. Like v1's config-editor, this service is
  designed for trusted private networks only.
- It does not serve files outside the `static/` directory.
- It does not know what any specific registered field means. Schema and data
  transforms are entirely delegated to `adapter.py` and `jedison_mapping.py`.
- It does not decide whether a save is safe to perform; `save.validate_all`
  is the sole authority on that, per IMPLEMENTATION_DETAILS.md's "Why
  validation always goes through the real Pydantic model".
"""

from __future__ import annotations

import http.server
import json
import logging
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from . import adapter, jedison_mapping
from .registry import REGISTRY, RegistryEntry, resolve_model
from .save import FieldError, field_errors_from_validation_error, validate_all, write_all

logger = logging.getLogger(__name__)

# Only these extensions are served from the static directory. `.map` is
# required beyond v1's own allowlist ({".js", ".css", ".html"}) because this
# editor vendors Bootstrap's source maps (`.css.map`, `.js.map`) alongside
# its minified assets; the vendored license and version files are provenance
# records, not runtime assets, and are deliberately not reachable over HTTP.
_ALLOWED_STATIC_EXTENSIONS = {".js", ".css", ".html", ".map"}

# Largest request body accepted on a POST. The only POST this API accepts is
# a full submission for every registered entry as JSON, which is at most a
# few tens of kilobytes even for a config with many device instances; 1 MiB
# leaves a wide margin while keeping a bad or hostile Content-Length from
# exhausting the process. Matches v1's own MAX_REQUEST_BODY_BYTES.
MAX_REQUEST_BODY_BYTES = 1024 * 1024

# Path to the static files bundled with this package: index.html, app.js,
# and the vendored Jedison/Bootstrap assets under static/vendor/.
_STATIC_DIR = Path(__file__).parent / "static"


def _safe_join(base: Path, filename: str) -> Path | None:
    """Resolves `filename` relative to `base` and verifies containment.

    Joins `filename` onto the resolved `base` directory, normalises the
    result, and confirms that the final path still starts with `base`. This
    prevents path traversal regardless of how many `..` segments, or an
    absolute path that would make `os.path.join` discard `base` entirely,
    are embedded in `filename`.

    The `os.sep` suffix on the prefix check avoids a false pass when a
    sibling directory shares the same prefix (e.g. `/data` vs `/data2`).

    Args:
        base: The directory that the result must stay inside.
        filename: A filename taken from an HTTP request path.

    Returns:
        Resolved `Path` inside `base`, or `None` if validation fails.
    """
    base_path = os.path.realpath(str(base))
    fullpath = os.path.normpath(os.path.join(base_path, filename))
    if not fullpath.startswith(base_path + os.sep):
        return None
    return Path(fullpath)


class ConfigEditorV2Server:
    """Stdlib HTTP server exposing the config-editor-v2 API and frontend.

    All request handling is synchronous; the stdlib `ThreadingHTTPServer` is
    used so that concurrent browser requests do not block each other.

    Each registered entry's raw JSON Schema (`model_json_schema()`, before
    any adapter transform) is computed once at construction and cached. It
    is used on both directions of the API: `GET /api/entries` runs it
    through `adapter.transform_schema_document` to build the outgoing
    schema, and `POST /api/save` reads it back, resolving each submitted
    field's transform (only ever an explicit `x-mimir-adapter` hint, at
    every level of `$defs`) via `adapter.transform_value_document`. Both
    directions need the same raw schema for this resolution. No transform is
    currently registered, so both calls are pass-throughs today; see
    `adapter.py`'s own module docstring.

    Args:
        config_dir: Directory where registered YAML files are read from and
            written to.
        port: TCP port to listen on. Pass 0 to let the OS assign a free port
            (useful in tests).
        entries: The registry entries this server exposes. Defaults to
            `registry.REGISTRY`, the real, importable configuration sources
            in this environment. Tests may pass a smaller list.
    """

    def __init__(
        self,
        config_dir: Path,
        port: int,
        entries: list[RegistryEntry] | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._entries: list[RegistryEntry] = (
            list(REGISTRY) if entries is None else list(entries)
        )

        # Precomputed once per entry: the untransformed model_json_schema(),
        # keyed by entry name. See the class docstring for why both API
        # directions need this same raw schema.
        self._raw_schemas: dict[str, dict[str, Any]] = {
            entry.name: resolve_model(entry).model_json_schema() for entry in self._entries
        }

        # Build the actual HTTP server. handler_factory creates a closure
        # over self so the handler can call handle_request without global
        # state.
        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, headers, body = server_self.handle_request("GET", self.path, body=b"")
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    logger.warning("Rejecting POST: bad Content-Length %r.", raw_length)
                    self._reject(400, "bad Content-Length")
                    return
                if length < 0:
                    logger.warning("Rejecting POST: negative Content-Length %r.", raw_length)
                    self._reject(400, "bad Content-Length")
                    return
                if length > MAX_REQUEST_BODY_BYTES:
                    logger.warning(
                        "Rejecting POST: body of %d bytes exceeds the %d byte limit.",
                        length,
                        MAX_REQUEST_BODY_BYTES,
                    )
                    self._reject(413, "request body too large")
                    return
                raw = self.rfile.read(length)
                status, headers, body = server_self.handle_request("POST", self.path, body=raw)
                self._send(status, headers, body)

            def _reject(self, status: int, message: str) -> None:
                """Sends a JSON error response without touching the request body."""
                payload = json.dumps({"ok": False, "error": message}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                def _sanitize_header_component(component: str) -> str:
                    # Prevent HTTP response splitting by stripping CR and LF
                    # characters from header names and values before they
                    # are written to the wire.
                    return component.replace("\r", "").replace("\n", "")

                self.send_response(status)
                for key, value in headers.items():
                    safe_key = _sanitize_header_component(str(key))
                    safe_value = _sanitize_header_component(str(value))
                    self.send_header(safe_key, safe_value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug(format, *args)

        self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)

    @property
    def server_port(self) -> int:
        """Returns the actual TCP port the server is bound to."""
        return self._httpd.server_address[1]

    def serve_forever(self) -> None:
        """Starts serving requests. Blocks until shutdown() is called."""
        logger.info("config-editor-v2 listening on port %d", self.server_port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stops the server cleanly."""
        self._httpd.shutdown()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    def handle_request(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatches a request and returns (status_code, headers, body_bytes).

        Called both by the real HTTP handler (do_GET / do_POST) and directly
        by unit tests, which avoids the need for a live socket in unit tests.

        Args:
            method: HTTP method ("GET" or "POST").
            path: Request path, possibly with a query string (ignored).
            body: Raw request body bytes.

        Returns:
            A three-tuple of (HTTP status code, response headers, body bytes).
        """
        path = path.split("?")[0]

        # Fast rejection of obvious path traversal attempts before routing.
        # _safe_join performs the authoritative containment check downstream
        # for static files; this only catches the literal ".." / NUL case
        # early, before any routing work happens.
        if ".." in path or "\x00" in path:
            return self._json_response(400, {"error": "bad request"})

        if method == "GET" and path == "/":
            return self._serve_index()
        if method == "GET" and path.startswith("/static/"):
            return self._serve_static(path)
        if method == "GET" and path == "/api/entries":
            return self._api_get_entries()
        if method == "GET" and path.startswith("/api/entries/") and path.endswith("/data"):
            encoded_name = path[len("/api/entries/") : -len("/data")]
            return self._api_get_entry_data(urllib.parse.unquote(encoded_name))
        if method == "POST" and path == "/api/save":
            return self._api_post_save(body)

        return self._json_response(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def _serve_index(self) -> tuple[int, dict[str, str], bytes]:
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            return self._json_response(404, {"error": "index.html not found"})
        return 200, {"Content-Type": "text/html; charset=utf-8"}, index.read_bytes()

    def _serve_static(self, path: str) -> tuple[int, dict[str, str], bytes]:
        """Serves a file from the static directory with path traversal protection.

        Only files with allowed extensions (`.js`, `.css`, `.html`, `.map`)
        are served. Anything that does not resolve to a path inside
        `static/` -- via `..` segments, a NUL byte, or an absolute path that
        would make `os.path.join` discard the static directory entirely --
        is rejected with 403.

        Args:
            path: The raw request path (e.g. '/static/app.js' or
                '/static/vendor/bootstrap/bootstrap.min.css.map').

        Returns:
            A three-tuple of (status, headers, body).
        """
        relative = path[len("/static/") :]

        suffix = Path(relative).suffix
        if suffix not in _ALLOWED_STATIC_EXTENSIONS:
            return self._json_response(403, {"error": "forbidden"})

        resolved = _safe_join(_STATIC_DIR, relative)
        if resolved is None:
            return self._json_response(403, {"error": "forbidden"})

        if not resolved.exists():
            return self._json_response(404, {"error": "not found"})

        content_type = mimetypes.types_map.get(suffix, "application/octet-stream")
        return 200, {"Content-Type": content_type}, resolved.read_bytes()

    # ------------------------------------------------------------------
    # API endpoints
    # ------------------------------------------------------------------

    def _api_get_entries(self) -> tuple[int, dict[str, str], bytes]:
        """Returns every registered entry's name, filename, and adapted schema.

        For each entry, the cached raw `model_json_schema()` is run through
        `adapter.transform_schema_document` (dispatching every field, at
        every level of `$defs`, into whichever transform it explicitly
        names via `x-mimir-adapter`), then through
        `jedison_mapping.to_jedison_object_schema` (setting Jedison's native
        `x-objectAdd: True` on every field that names an add-button label
        via `x-addPropertyContent` -- see that module's own docstring for
        why this one derivation is not gated behind `x-mimir-adapter`).

        Returns:
            HTTP 200 with a JSON list of
            `{"name": str, "filename": str, "schema": <adapted schema>}`,
            one per registered entry, in registration order.
        """
        result = [
            {
                "name": entry.name,
                "filename": entry.filename,
                "schema": jedison_mapping.to_jedison_object_schema(
                    adapter.transform_schema_document(self._raw_schemas[entry.name])
                ),
            }
            for entry in self._entries
        ]
        return self._json_response(200, result)

    def _api_get_entry_data(self, name: str) -> tuple[int, dict[str, str], bytes]:
        """Returns one entry's current on-disk data, validated and defaulted.

        The raw on-disk YAML is validated against the entry's own model and
        the *validated* model is dumped back out, not the raw dict. This
        matters for any field using `default_factory` (every device-map and
        config-section field in MimirheimConfig, for example): Pydantic
        never emits a literal `default` for such a field in its JSON Schema,
        so a partial file that omits the key would otherwise reach the
        frontend with that key simply absent -- Jedison has no schema
        default and no data to build that section from, and silently fails
        to render it. Validating and re-dumping fills every such field in
        with its real default before the response is sent.

        A file that fails validation is a data problem this editor does not
        try to paper over: it is rejected outright rather than passed
        through as-is or silently emptied. Whether the failure is a
        hand-edited mistake or a config that predates a model change, fixing
        it is the user's job (for a hand edit) or the owning helper's/
        mimirheim's job (to migrate the file first), not this editor's.

        Args:
            name: The `RegistryEntry.name` to fetch data for, already
                URL-decoded by the caller.

        Returns:
            HTTP 200 with the validated, defaulted data dict as JSON.
            HTTP 422 with `{"errors": {<entry_name>: [<FieldError dict>,
            ...]}}` if an on-disk file exists but fails validation.
            HTTP 200 with an empty dict if no file exists yet and the model
            cannot be instantiated with no arguments -- because it has at
            least one required field with no default, true of every
            registered production model -- so a never-configured entry can
            still be opened and edited in the frontend rather than crashing
            the request handler.
            HTTP 404 if `name` does not match any registered entry.
        """
        entry = self._find_entry(name)
        if entry is None:
            return self._json_response(404, {"error": "unknown entry"})

        model_cls = resolve_model(entry)
        file_path = self._config_dir / entry.filename
        if file_path.exists():
            raw = yaml.safe_load(file_path.read_text()) or {}
            try:
                validated = model_cls.model_validate(raw)
            except ValidationError as exc:
                errors = field_errors_from_validation_error(entry.name, exc)
                return self._json_response(
                    422, {"errors": self._group_errors_by_entry(errors)}
                )
            return self._json_response(200, validated.model_dump(mode="json"))

        try:
            defaults = model_cls().model_dump(mode="json")
        except ValidationError:
            logger.info(
                "Entry %r has no on-disk file and its model has no valid "
                "no-argument default; returning an empty dict.",
                entry.name,
            )
            defaults = {}
        return self._json_response(200, defaults)

    def _api_post_save(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Validates every submitted entry together, then writes all or nothing.

        The request body is a JSON object mapping `RegistryEntry.name` to
        that entry's submitted field values, in the same shape
        `GET /api/entries/{name}/data` returns. Every top-level field's
        value, and everything nested inside it, is run through
        `adapter.transform_value_document`, using that entry's raw
        (untransformed) schema to resolve which transform (if any) applies
        at every level, before the merged per-entry dicts are handed to
        `save.validate_all`. No transform is currently registered, so this
        is a pass-through today.

        An entry the frontend has not loaded (and therefore did not include
        in the body) is handled per `save.validate_all`'s contract: it is
        validated against its model's own defaults on a best-effort basis
        and included if that succeeds, or silently excluded from the save
        -- not written, not a blocking error -- if it does not. This is
        what makes an untouched helper's config not block saving the
        entries a user did edit.

        Returns:
            HTTP 200 `{"ok": true}` on success, after `save.write_all` has
            written every registered file.
            HTTP 422 `{"errors": {<entry_name>: [<FieldError-shaped dict>, ...]}}`
            if any entry fails validation. Every error dict retains its own
            `entry_name` key even though it is also the outer grouping key,
            so a client can flatten the structure if it prefers. No file is
            written in this case, including for entries that individually
            validated.
            HTTP 400 on a malformed JSON body.
        """
        try:
            submitted_raw = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json_response(400, {"error": str(exc)})

        if not isinstance(submitted_raw, dict):
            return self._json_response(400, {"error": "request body must be a JSON object"})

        transformed_submitted: dict[str, dict[str, Any]] = {}
        for entry in self._entries:
            entry_data = submitted_raw.get(entry.name)
            if entry_data is None:
                continue
            raw_schema = self._raw_schemas[entry.name]
            properties = raw_schema.get("properties", {})
            defs = raw_schema.get("$defs", {})
            transformed_submitted[entry.name] = {
                field_name: adapter.transform_value_document(
                    properties.get(field_name, {}), value, defs
                )
                for field_name, value in entry_data.items()
            }

        validated, errors = validate_all(self._entries, transformed_submitted)

        if errors:
            return self._json_response(422, {"errors": self._group_errors_by_entry(errors)})

        write_all(validated, self._entries, self._config_dir)
        return self._json_response(200, {"ok": True})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_entry(self, name: str) -> RegistryEntry | None:
        """Returns the registered entry with the given name, or None."""
        for entry in self._entries:
            if entry.name == name:
                return entry
        return None

    @staticmethod
    def _group_errors_by_entry(errors: list[FieldError]) -> dict[str, list[dict[str, Any]]]:
        """Groups a flat FieldError list into a dict keyed by entry_name.

        Each error dict retains its own `entry_name` key in addition to
        being grouped under it, so a client can work from either the
        grouping or the flattened list.

        Args:
            errors: The errors returned by `save.validate_all`.

        Returns:
            A dict mapping entry name to the list of that entry's errors, in
            the order `validate_all` produced them.
        """
        grouped: dict[str, list[dict[str, Any]]] = {}
        for error in errors:
            grouped.setdefault(error.entry_name, []).append(error.model_dump())
        return grouped

    @staticmethod
    def _json_response(status: int, data: Any) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(data).encode()
        return status, {"Content-Type": "application/json"}, body
