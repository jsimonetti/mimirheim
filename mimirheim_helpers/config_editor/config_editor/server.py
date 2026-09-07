"""HTTP server for the mimirheim config editor.

This module provides ConfigEditorServer, a lightweight HTTP server that
serves the static frontend files and a JSON API on top of
``config_editor.registry`` (schema discovery, validation, and save
semantics -- SPEC.md §2-§9):

    GET  /api/registry                 -- every discovered entry + problems
    GET  /api/entry/<id>                -- one entry's schema, value, enabled
    POST /api/save                      -- validate-all-then-write-all
    POST /api/preview                   -- same shape as /api/save, no writes
    POST /api/reload                    -- re-run discovery, no restart

The tool's original, hardcoded-helper-list endpoints (``/api/schema``,
``/api/config``, ``/api/helper-configs``, ``/api/helper-schemas``,
``/api/helper-config/<filename>``) have been removed. ``static/app.js``
renders every entry through the vendored Jedison library against the
registry-backed API above (plan 69).

The server uses only Python stdlib (http.server, threading, json, yaml) plus
``config_editor.registry`` and ``config_editor.yaml_io``. No external web
framework is required.

What this module does not do:
- It does not authenticate users. The editor is designed for trusted private
  networks only.
- It does not serve files outside the static/ directory.
- It does not parse MQTT messages or interact with the solver.
- It does not implement schema discovery, validation, or save semantics
  itself -- that is entirely ``config_editor.registry``'s job. This module
  only translates between HTTP requests/responses and calls into it.
"""
from __future__ import annotations

import dataclasses
import difflib
import http.server
import importlib
import json
import logging
import mimetypes
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from config_editor import registry
from config_editor.yaml_io import render_yaml_preserving_comments, write_yaml_preserving_comments
from helper_common.config import mqtt_env_overrides

logger = logging.getLogger(__name__)

# Only these extensions are served from the static directory.
_ALLOWED_STATIC_EXTENSIONS = {".js", ".css", ".html"}

# Only these extensions are served from the reports directory.
_ALLOWED_REPORT_EXTENSIONS = {".html", ".js", ".css"}

# Only these suffixes are served from the dump directory.
_ALLOWED_DUMP_SUFFIXES = ("_input.json", "_output.json")

# Substituted for credential values in an entry's returned value and in
# GET /api/config's mqtt_env.
#
# The editor needs to know which mqtt fields the Supervisor supplies, and
# needs a value it can compare the form field against to decide whether the
# user overrode it. It does not need the secret itself, and this server does
# not authenticate: anything that can reach the port could read a real
# password out of the response. The POST handlers strip this sentinel back
# out, so it never reaches a YAML file even though the form posts it
# straight back.
MQTT_ENV_REDACTED = "__supervisor_provided__"

# mqtt fields whose value is replaced by MQTT_ENV_REDACTED on the way out.
# host, port and tls are not secrets and the form needs to display them.
_REDACTED_MQTT_FIELDS = ("password",)

# Largest request body accepted on a POST.
#
# self.rfile.read(length) previously read whatever the client declared straight
# into memory, with no cap. The largest thing this API legitimately receives is
# a full mimirheim.yaml as JSON, which is a few tens of kilobytes; 1 MiB leaves
# a wide margin while keeping a bad or hostile Content-Length from exhausting a
# Home Assistant add-on box.
MAX_REQUEST_BODY_BYTES = 1024 * 1024

# Content-Security-Policy applied to every response (SPEC.md §11).
#
# script-src carries no 'unsafe-inline' or 'unsafe-eval': the vendored Jedison
# build (checked against its source) never calls eval()/new Function() and
# never sets inline script.
#
# style-src carries 'unsafe-inline': the vendored Jedison build injects two
# small, fixed CSS blocks at runtime via createElement("style") + textContent
# ("jedi-nav-styles", "jedi-accordion-button-style") to lay out nav/accordion
# widgets, which style-src 'self' alone blocks (a runtime <style> element is
# always inline, regardless of the "style" attribute vs. CSSOM .style
# property distinction). SPEC.md §11 only requires blocking inline/remote
# *script* execution, not style, so this does not reopen that guarantee --
# it does mean a hostile schema's CSS could reach an inline style context,
# which is a lower-severity concern than script execution.
#
# Every directive is otherwise scoped to 'self' -- no response this server
# sends should ever cause the browser to reach off-origin, which is what the
# offline-devtools acceptance check (SPEC.md §11) verifies.
_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

# Path to the static files bundled with this package.
_STATIC_DIR = Path(__file__).parent / "static"


def _safe_join(base: Path, filename: str) -> Path | None:
    """Resolve ``filename`` relative to ``base`` and verify containment.

    Joins ``filename`` onto the resolved ``base`` directory, normalises the
    result, and confirms that the final path still starts with ``base``. This
    prevents path traversal regardless of how many ``..`` segments or other
    tricks are embedded in ``filename``.

    The ``os.sep`` suffix on the prefix check avoids a false pass when a
    sibling directory shares the same prefix (e.g. ``/data`` vs ``/data2``).

    Args:
        base: The directory that the result must stay inside.
        filename: A filename from an HTTP request or config key.

    Returns:
        Resolved ``Path`` inside ``base``, or ``None`` if validation fails.
    """
    base_path = os.path.realpath(str(base))
    fullpath = os.path.normpath(os.path.join(base_path, filename))
    if not fullpath.startswith(base_path + os.sep):
        return None
    return Path(fullpath)


class _BadRequest(Exception):
    """Raised internally when a POST body fails to parse or is malformed shape.

    Caught at the HTTP-handler boundary and turned into a 400 response;
    never propagates past ``handle_request``.
    """


class ConfigEditorServer:
    """Lightweight HTTP server for the mimirheim config editor.

    Serves static files and the registry-backed JSON API. All request
    handling is synchronous; the stdlib ThreadingHTTPServer is used so that
    concurrent browser requests do not block each other.

    Schema discovery (``config_editor.registry.build_registry``) runs once at
    construction time and is cached in ``self._registry`` for the lifetime of
    the server instance; ``POST /api/reload`` re-runs it without restarting
    the process. An entry's ``context`` and current file content are never
    cached -- they are rebuilt from disk on every ``GET /api/entry/<id>``
    (SPEC.md §5 Decision 5).

    Args:
        config_dir: Directory where mimirheim YAML files are read from and
            written to. Its ``schemas/`` subdirectory, if present, is scanned
            for drop-in schema files (SPEC.md §2).
        port: TCP port to listen on. Pass 0 to let the OS assign a free port
            (useful in tests).
        allowed_ip: If set, only requests from this source IP are accepted;
            all others receive 403.
    """

    def __init__(
        self,
        config_dir: Path,
        port: int,
        allowed_ip: str | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._allowed_ip = allowed_ip
        self._registry: registry.Registry = self._discover_registry()

        # Build the actual HTTP server. handler_factory creates a closure over
        # self so the handler can call _dispatch without global state.
        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if server_self._allowed_ip and self.client_address[0] != server_self._allowed_ip:
                    self._send(403, {"Content-Security-Policy": _CSP_HEADER_VALUE}, b"")
                    return
                status, headers, body = server_self.handle_request("GET", self.path, body=b"")
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                if server_self._allowed_ip and self.client_address[0] != server_self._allowed_ip:
                    self._send(403, {"Content-Security-Policy": _CSP_HEADER_VALUE}, b"")
                    return
                raw_length = self.headers.get("Content-Length", "0")
                try:
                    length = int(raw_length)
                except ValueError:
                    # A non-numeric header used to raise ValueError here, which
                    # took out the handler thread and reset the connection with
                    # no response at all.
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
                """Send a JSON error response without touching the request body."""
                payload = json.dumps({"ok": False, "error": message}).encode()
                self._send(
                    status,
                    {"Content-Type": "application/json", "Content-Security-Policy": _CSP_HEADER_VALUE},
                    payload,
                )

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                def _sanitize_header_component(component: str) -> str:
                    # Prevent HTTP response splitting by stripping CR and LF
                    # characters from header names and values before they are
                    # written to the wire.
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
        """Return the actual TCP port the server is bound to."""
        return self._httpd.server_address[1]

    def _discover_registry(self) -> registry.Registry:
        """Run schema discovery (SPEC.md §2) against this server's directories.

        Raises:
            registry.BundledSchemaCollisionError: See
                ``registry.discover_bundled`` -- a packaging bug, not user
                input; the server fails loudly at startup or reload rather
                than silently picking one.
        """
        return registry.build_registry(
            bundled_dir=registry.BUNDLED_SCHEMA_DIR,
            dropin_dir=self._config_dir / "schemas",
        )

    def _read_reporter_yaml(self) -> dict[str, Any]:
        """Read and parse reporter.yaml from the config directory.

        Returns an empty dict if the file is absent, unreadable, or cannot be
        parsed.
        """
        return self._read_yaml_file(self._config_dir / "reporter.yaml")

    def _reporting_path(self, key: str) -> Path | None:
        """Return a path from the ``reporting`` section of reporter.yaml.

        Read on each access rather than cached at construction. The editor
        writes reporter.yaml itself, so a cached value meant that enabling the
        reporter, or moving its output directory, had no effect until the
        container restarted -- with nothing in the UI to explain why. This is a
        low-traffic admin interface and the file is small.

        Args:
            key: The key to read from the ``reporting`` section.

        Returns:
            The configured path, or None when unset.
        """
        value = (self._read_reporter_yaml().get("reporting") or {}).get(key)
        return Path(value) if value else None

    @property
    def _reports_dir(self) -> Path | None:
        """Current reporting.output_dir, or None if not configured."""
        return self._reporting_path("output_dir")

    @property
    def _dump_dir(self) -> Path | None:
        """Current reporting.dump_dir, or None if not configured."""
        return self._reporting_path("dump_dir")

    def serve_forever(self) -> None:
        """Start serving requests. Blocks until shutdown() is called."""
        logger.info("Config editor listening on port %d", self.server_port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stop the server cleanly."""
        self._httpd.shutdown()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    def handle_request(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatch a request and return (status_code, headers, body_bytes).

        This method is called both by the real HTTP handler (do_GET / do_POST)
        and directly by unit tests, which avoids the need for a live socket in
        unit tests. Every response is given a Content-Security-Policy header
        here, in this one place, so no route can be added later that forgets
        it (SPEC.md §11).

        Args:
            method: HTTP method ("GET" or "POST").
            path: Request path, possibly with query string (query string is
                ignored).
            body: Raw request body bytes.

        Returns:
            A three-tuple of (HTTP status code, response headers dict, body bytes).
        """
        status, headers, resp_body = self._route(method, path, body)
        return status, {**headers, "Content-Security-Policy": _CSP_HEADER_VALUE}, resp_body

    def _route(
        self, method: str, path: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """The actual request routing table; see :meth:`handle_request`."""
        # Strip query string.
        path = path.split("?")[0]

        # Fast rejection of obvious path traversal attempts before routing.
        # _safe_join performs the authoritative containment check downstream,
        # but catching these early avoids unnecessary routing work and makes
        # the intent clear to static analysis tools.
        if ".." in path or "\x00" in path:
            return self._json_response(400, {"error": "bad request"})

        if method == "GET" and path == "/":
            return self._serve_index()
        if method == "GET" and path.startswith("/static/"):
            return self._serve_static(path)
        if method == "GET" and path in ("/reports", "/reports/"):
            return self._serve_reports_index()
        if method == "GET" and path.startswith("/reports/dumps/"):
            return self._serve_dump_file(path[len("/reports/dumps/"):])
        if method == "GET" and path.startswith("/reports/"):
            return self._serve_report_file(path[len("/reports/"):])

        # -- Current registry-backed API (SPEC.md §12) --
        if method == "GET" and path == "/api/registry":
            return self._api_get_registry()
        if method == "GET" and path.startswith("/api/entry/"):
            return self._api_get_entry(path[len("/api/entry/"):])
        if method == "POST" and path == "/api/save":
            return self._api_post_save(body)
        if method == "POST" and path == "/api/preview":
            return self._api_post_preview(body)
        if method == "POST" and path == "/api/reload":
            return self._api_post_reload()

        return self._json_response(404, {"error": "not found"})

    # ------------------------------------------------------------------
    # Static file serving
    # ------------------------------------------------------------------

    def _serve_index(self) -> tuple[int, dict[str, str], bytes]:
        index = _STATIC_DIR / "index.html"
        if not index.exists():
            return self._json_response(404, {"error": "index.html not found"})
        return 200, {"Content-Type": "text/html; charset=utf-8"}, index.read_bytes()

    def _serve_reports_index(self) -> tuple[int, dict[str, str], bytes]:
        """Serve the reports index.html from the configured reports directory.

        Any ``target="_blank"`` attributes are stripped from the response so
        that report links navigate within the iframe instead of popping out to
        a new browser tab. This works regardless of which version of index.html
        the reporter has written to the output directory.
        """
        if self._reports_dir is None:
            return self._json_response(404, {"error": "reports directory not configured"})
        index = self._reports_dir / "index.html"
        if not index.exists():
            return self._json_response(404, {"error": "reports index not found"})
        content = index.read_bytes().replace(b' target="_blank"', b"")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, content

    def _serve_report_file(self, filename: str) -> tuple[int, dict[str, str], bytes]:
        """Serve a single file from the reports directory.

        Only flat filenames are accepted -- no path separators or traversal
        components. Allowed extensions: .html, .js.

        Args:
            filename: Bare filename extracted from the request path.

        Returns:
            A three-tuple of (status, headers, body).
        """
        if self._reports_dir is None:
            return self._json_response(404, {"error": "reports directory not configured"})
        suffix = Path(filename).suffix
        if suffix not in _ALLOWED_REPORT_EXTENSIONS:
            return self._json_response(403, {"error": "forbidden"})
        resolved = _safe_join(self._reports_dir, filename)
        if resolved is None:
            return self._json_response(403, {"error": "forbidden"})
        if not resolved.exists():
            return self._json_response(404, {"error": "not found"})
        content_type = mimetypes.types_map.get(suffix, "application/octet-stream")
        return 200, {"Content-Type": content_type}, resolved.read_bytes()

    def _serve_dump_file(self, filename: str) -> tuple[int, dict[str, str], bytes]:
        """Serve a dump JSON file from the reporter's dump directory.

        Only flat filenames ending in ``_input.json`` or ``_output.json`` are
        served. Path separators and traversal components are rejected with 403.

        Download links in the report index use the relative path ``dumps/<filename>``
        so they work through the config editor proxy. When the report index is opened
        directly from the filesystem, these links will 404 -- users who need
        direct-file access can add a web server alias or symlink themselves.

        Args:
            filename: Bare filename extracted from the request path.

        Returns:
            A three-tuple of (status, headers, body).
        """
        if self._dump_dir is None:
            return self._json_response(404, {"error": "dump directory not configured"})
        if not any(filename.endswith(s) for s in _ALLOWED_DUMP_SUFFIXES):
            return self._json_response(403, {"error": "forbidden"})
        resolved = _safe_join(self._dump_dir, filename)
        if resolved is None:
            return self._json_response(403, {"error": "forbidden"})
        if not resolved.exists():
            return self._json_response(404, {"error": "not found"})
        return (
            200,
            {
                "Content-Type": "application/json",
                # resolved.name is the final path component after symlink
                # resolution and containment verification -- safe for use in
                # the Content-Disposition header.
                "Content-Disposition": f'attachment; filename="{resolved.name}"',
            },
            resolved.read_bytes(),
        )

    def _serve_static(self, path: str) -> tuple[int, dict[str, str], bytes]:
        """Serve a file from the static directory with path traversal protection.

        Only files with allowed extensions (.js, .css, .html) are served.
        Any path component containing '..' is rejected with 403.

        Args:
            path: The raw request path (e.g. '/static/app.js').

        Returns:
            A three-tuple of (status, headers, body).
        """
        # Strip the /static/ prefix.
        relative = path[len("/static/"):]

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
    # Registry-backed API endpoints (SPEC.md §12)
    # ------------------------------------------------------------------

    def _api_get_registry(self) -> tuple[int, dict[str, str], bytes]:
        """Return every discovered entry's id, x-mimirheim fields, enabled state, and problems."""
        return self._json_response(200, self._registry_payload())

    def _api_post_reload(self) -> tuple[int, dict[str, str], bytes]:
        """Re-run discovery without restarting the process; return the new registry state."""
        self._registry = self._discover_registry()
        return self._json_response(200, self._registry_payload())

    def _registry_payload(self) -> dict[str, Any]:
        """Build the response body shared by GET /api/registry and POST /api/reload."""
        entries: dict[str, Any] = {}
        for entry_id, entry in self._registry.entries.items():
            entries[entry_id] = {
                "x-mimirheim": self._envelope_dict(entry.envelope),
                "enabled": (self._config_dir / entry.envelope.file).exists(),
            }
        problems = [{"source": p.source, "reason": p.reason} for p in self._registry.problems]
        return {"entries": entries, "problems": problems}

    def _api_get_entry(self, entry_id: str) -> tuple[int, dict[str, str], bytes]:
        """Return one entry's dereferenceable schema, current value, and enabled state.

        Both ``context`` (for every entry but mimirheim.yaml's own) and the
        current file content are rebuilt from disk on every call -- no
        in-memory cache is kept across requests (SPEC.md §5 Decision 5).

        The response also carries ``mqtt_env``: the env-supplied mqtt fields
        with credentials redacted (:meth:`_mqtt_env_for_client`), for every
        entry, not only mimirheim.yaml's own -- every entry's schema may carry
        its own ``mqtt`` section. Without this, the frontend has no way to
        render a Supervisor-provided-password placeholder or to know which
        mqtt fields it should treat as read-only-unless-overridden.
        """
        entry = self._registry.get(entry_id)
        if entry is None:
            return self._json_response(404, {"error": f"unknown entry id {entry_id!r}"})

        schema = registry.compose_entry_schema(entry)
        target = self._config_dir / entry.envelope.file
        enabled = target.exists()
        value = (
            self._read_yaml_file(target)
            if enabled
            else self._model_defaults(entry.envelope.python_model)
        )
        if entry.envelope.file != registry.MIMIRHEIM_YAML_FILE:
            mimirheim_config = self._read_yaml_file(self._config_dir / registry.MIMIRHEIM_YAML_FILE)
            value = {**value, "context": registry.build_context(mimirheim_config)}

        return self._json_response(
            200,
            {
                "schema": schema,
                "value": value,
                "enabled": enabled,
                "mqtt_env": self._mqtt_env_for_client(),
            },
        )

    def _api_post_save(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Validate-all-then-write-all the submitted entries (SPEC.md §8)."""
        try:
            entries = self._extract_entries(body)
        except _BadRequest as exc:
            return self._json_response(400, {"ok": False, "errors": str(exc)})

        to_write, to_validate = self._redact_and_merge_entries(entries)
        try:
            errors = registry.validate_save(self._registry, to_validate)
        except registry.UnknownEntryError as exc:
            return self._json_response(404, {"error": f"unknown entry id: {exc}"})
        if errors:
            return self._json_response(422, {"ok": False, "errors": errors})

        registry.write_entries(
            self._registry, self._config_dir, to_write, write_yaml=write_yaml_preserving_comments
        )
        return self._json_response(200, {"ok": True})

    def _api_post_preview(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Compute the YAML diff a save would produce, without writing anything.

        Shares ``registry.write_entries`` -- the exact merge/write
        implementation ``/api/save`` uses -- via swapped ``write_yaml`` and
        ``delete_yaml`` callbacks that compute a unified diff instead of
        touching disk (SPEC.md §12).
        """
        try:
            entries = self._extract_entries(body)
        except _BadRequest as exc:
            return self._json_response(400, {"ok": False, "errors": str(exc)})

        to_write, to_validate = self._redact_and_merge_entries(entries)
        try:
            errors = registry.validate_save(self._registry, to_validate)
        except registry.UnknownEntryError as exc:
            return self._json_response(404, {"error": f"unknown entry id: {exc}"})
        if errors:
            return self._json_response(422, {"ok": False, "errors": errors})

        diffs: dict[str, str] = {}

        def _preview_write(data: dict[str, Any], path: Path) -> str:
            before = path.read_text() if path.exists() else ""
            after = render_yaml_preserving_comments(data, path)
            diffs[path.name] = self._unified_diff(before, after, path.name)
            return after

        def _preview_delete(path: Path) -> None:
            before = path.read_text() if path.exists() else ""
            diffs[path.name] = self._unified_diff(before, "", path.name)

        registry.write_entries(
            self._registry,
            self._config_dir,
            to_write,
            write_yaml=_preview_write,
            delete_yaml=_preview_delete,
        )
        return self._json_response(200, {"ok": True, "diffs": diffs})

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_entries(body: bytes) -> dict[str, dict[str, Any]]:
        """Parse and structurally validate a /api/save or /api/preview request body.

        Args:
            body: Raw JSON bytes, expected to decode to
                ``{"entries": {"<id>": {...}, ...}}``.

        Returns:
            The parsed ``entries`` mapping.

        Raises:
            _BadRequest: If the body is not valid JSON, or is not shaped
                like ``{"entries": {...}}``.
        """
        try:
            raw = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            raise _BadRequest(str(exc)) from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("entries"), dict):
            raise _BadRequest("request body must be {'entries': {'<id>': {...}, ...}}")
        return raw["entries"]

    def _redact_and_merge_entries(
        self, entries: Mapping[str, Mapping[str, Any]]
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        """Split a raw request's entries into (what to write, what to validate against).

        Every enabled entry's config has the MQTT_ENV_REDACTED sentinel
        stripped before either step (Decision 8), so a form posting the
        placeholder back for an untouched password field never validates or
        writes it. The validation-only copy additionally has env-supplied
        MQTT fields merged in, so a config that is only complete once the
        Supervisor's environment variables are considered still validates;
        the merged-in fields are never written to disk.

        Args:
            entries: The raw ``{"<id>": {"enabled": ..., "config": {...}}}``
                mapping from the request body.

        Returns:
            A ``(to_write, to_validate)`` pair, each shaped like ``entries``
            and suitable for ``registry.validate_save`` /
            ``registry.write_entries``.
        """
        mqtt_env = self._mqtt_env()
        to_write: dict[str, dict[str, Any]] = {}
        to_validate: dict[str, dict[str, Any]] = {}
        for entry_id, spec in entries.items():
            if not spec.get("enabled", True):
                to_write[entry_id] = {"enabled": False}
                to_validate[entry_id] = {"enabled": False}
                continue

            config = spec.get("config") or {}
            if isinstance(config, dict):
                config = self._strip_redacted_mqtt(config)
            to_write[entry_id] = {"enabled": True, "config": config}

            if mqtt_env and isinstance(config, dict):
                merged_config: dict[str, Any] = dict(config)
                merged_config["mqtt"] = {**mqtt_env, **dict(config.get("mqtt") or {})}
            else:
                merged_config = dict(config) if isinstance(config, dict) else {}
            to_validate[entry_id] = {"enabled": True, "config": merged_config}
        return to_write, to_validate

    @staticmethod
    def _unified_diff(before: str, after: str, filename: str) -> str:
        """Return a unified diff string between ``before`` and ``after``."""
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=filename,
                tofile=filename,
            )
        )

    @staticmethod
    def _envelope_dict(envelope: registry.XMimirheim) -> dict[str, Any]:
        """Return an ``x-mimirheim`` envelope as a plain JSON-shaped dict."""
        return dataclasses.asdict(envelope)

    @staticmethod
    def _read_yaml_file(path: Path) -> dict[str, Any]:
        """Parse a YAML file into a dict, or {} if absent, unreadable, or malformed.

        Shared by every endpoint that reads a config file's current raw
        content: an unreadable or malformed file degrades to "empty" rather
        than raising out of a request handler.
        """
        try:
            raw = yaml.safe_load(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("Could not read %s: %s", path, exc)
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _model_defaults(dotted_path: str | None) -> dict[str, Any]:
        """Build a not-yet-enabled entry's starting value from its model's own defaults.

        Uses ``model_construct()`` rather than a validating constructor:
        fields with a declared default are populated; fields with none
        (e.g. a helper's required ``mqtt`` section) are simply absent from
        the result, exactly as a fresh, not-yet-filled-in form should start.

        Args:
            dotted_path: ``"module.path:ClassName"``, or ``None`` for a
                drop-in or any entry with no ``python_model``.

        Returns:
            The model's default field values as a plain JSON-shaped dict, or
            ``{}`` if ``dotted_path`` is ``None`` or cannot be imported.
        """
        if dotted_path is None:
            return {}
        module_name, _, class_name = dotted_path.partition(":")
        if not module_name or not class_name:
            logger.error("Malformed x-mimirheim.python_model dotted path: %r", dotted_path)
            return {}
        try:
            module = importlib.import_module(module_name)
            model_cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            logger.error("Could not import python_model %r: %s", dotted_path, exc)
            return {}
        dumped = model_cls.model_construct().model_dump(mode="json")
        return dumped if isinstance(dumped, dict) else {}

    @classmethod
    def _mqtt_env_for_client(cls) -> dict[str, Any]:
        """Return the env-supplied mqtt fields with credentials redacted.

        Same keys as :meth:`_mqtt_env`, so the frontend still learns which
        fields the Supervisor controls, but with the secret values replaced by
        ``MQTT_ENV_REDACTED``.

        Returns:
            Dict mapping mqtt field names to their value, or to
            ``MQTT_ENV_REDACTED`` for the fields listed in
            ``_REDACTED_MQTT_FIELDS``.
        """
        env = cls._mqtt_env()
        for field in _REDACTED_MQTT_FIELDS:
            if field in env:
                env[field] = MQTT_ENV_REDACTED
        return env

    @staticmethod
    def _strip_redacted_mqtt(config: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of ``config`` with redacted mqtt values removed.

        The editor pre-fills its form from the ``mqtt_env`` in the GET
        response, so an untouched password field posts ``MQTT_ENV_REDACTED``
        back. Persisting that would leave a config whose broker password is a
        literal sentinel string.

        The sentinel is stripped whether or not the environment still supplies
        the field, so a config saved inside the add-on and re-saved outside it
        does not turn the placeholder into a real password.

        Args:
            config: The submitted configuration dict.

        Returns:
            A shallow copy with any sentinel-valued mqtt field removed. The
            ``mqtt`` key itself is dropped if nothing is left in it.
        """
        mqtt = config.get("mqtt")
        if not isinstance(mqtt, dict):
            return config
        cleaned = {k: v for k, v in mqtt.items() if v != MQTT_ENV_REDACTED}
        if cleaned == mqtt:
            return config
        result = dict(config)
        if cleaned:
            result["mqtt"] = cleaned
        else:
            del result["mqtt"]
        return result

    @staticmethod
    def _json_response(
        status: int, data: Any
    ) -> tuple[int, dict[str, str], bytes]:
        body = json.dumps(data).encode()
        return status, {"Content-Type": "application/json"}, body

    @staticmethod
    def _mqtt_env() -> dict[str, Any]:
        """Read MQTT broker settings from environment variables set by the HA Supervisor.

        Returns only keys that are actually present in the environment. The
        returned dict can be merged into an ``mqtt:`` section to fill in fields
        that the user has not explicitly set in their YAML config.

        The mapping is:

        =============== ========================
        Env var         mqtt field
        =============== ========================
        MQTT_HOST       host
        MQTT_PORT       port
        MQTT_USERNAME   username
        MQTT_PASSWORD   password
        MQTT_SSL        tls (true/false string)
        =============== ========================

        Returns:
            Dict mapping mqtt field names to their env-supplied values. Empty
            when no MQTT env vars are set (plain Docker, no Supervisor).
        """
        try:
            return mqtt_env_overrides()
        except ValueError as exc:
            # A helper daemon exits on this, and should: it cannot connect to a
            # broker on an invalid port. The editor is the tool an operator
            # reaches for to fix configuration, so it degrades instead --
            # reporting no Supervisor-supplied MQTT settings, which makes the
            # form show the mqtt section for manual entry.
            logger.warning("Ignoring MQTT environment overrides: %s", exc)
            return {}
