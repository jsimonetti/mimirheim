"""HTTP server for config-editor.

Provides `ConfigEditorServer`, a stdlib-only HTTP server that renders the
Config Editor's discovery page and each Config Owner's configuration page
directly from FormSpec, via Jinja2 templates (`templates/index.html`,
`templates/owner.html`), per ADR-0003 (no generic client-side
schema-to-form library):

    GET  /owners/<owner_id>   -- render one Config Owner's configuration
    POST /owners/<owner_id>   -- submit Candidate Values for validate_and_write
    GET  /static/<path>       -- serve a vendored static asset (e.g. Bootstrap5)

This module never imports a Config Owner's pydantic model, and never reads
or writes any Config Owner's configuration file itself: it only ever
consumes the generic `Descriptor` a Config Owner published (obtained from
`registry.py`), fetches that owner's current values via the injected
`ConfigServiceClient`'s `get_current_values` for a GET, and forwards a POST's
submitted values (parsed from their dotted/indexed form field names by
`config_editor.submission.parse_submission`) to the same client's
`submit_validate_and_write`. `ConfigServiceClient` is the only thing here
that touches MQTT.
"""

from __future__ import annotations

import http.server
import logging
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Any, Protocol

import jinja2

from mimirheim_shared.config_service import Descriptor, ValidateAndWriteResult
from mimirheim_shared.formspec import option_label

from config_editor.registry import ConfigOwnerRegistry
from config_editor.render import build_groups
from config_editor.submission import parse_submission

logger = logging.getLogger(__name__)

_TEMPLATES = jinja2.Environment(
    loader=jinja2.PackageLoader("config_editor", "templates"),
    autoescape=jinja2.select_autoescape(["html"]),
)
# ENUM_SELECT rendering (owner.html) resolves each <option>'s display label
# via this, the same per-value-label lookup a FieldSpec's option_labels
# describes; exposed as a Jinja global rather than duplicating the lookup in
# the template itself.
_TEMPLATES.globals["option_label"] = option_label

# Vendored static assets (Bootstrap5's CSS/JS; see ADR-0003 -- no CDN, no
# generic client-side form library). Bundled with this package so the editor
# renders identically with no internet access.
_STATIC_DIR = Path(__file__).parent / "static"
# Only these extensions are served from the static directory; e.g. the
# vendored Bootstrap LICENSE file is deliberately not servable.
_ALLOWED_STATIC_EXTENSIONS = {".css", ".js", ".html"}


def _safe_join(base: Path, filename: str) -> Path | None:
    """Resolves `filename` relative to `base` and verifies containment.

    Joins `filename` onto the resolved `base` directory, normalises the
    result, and confirms that the final path still starts with `base`. This
    prevents path traversal regardless of how many `..` segments or other
    tricks are embedded in `filename`.

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


class ConfigServiceClient(Protocol):
    """The one piece of MQTT behaviour `ConfigEditorServer` depends on.

    Implemented by `mqtt_client.ConfigEditorMqttClient`; declared here as a
    narrow Protocol so this module keeps depending on a behaviour, not a
    concrete MQTT implementation, and so tests can supply a fake with no
    paho involvement at all.
    """

    def submit_validate_and_write(
        self, owner_id: str, values: dict[str, Any], timeout: float = 10.0
    ) -> ValidateAndWriteResult:
        """Submits Candidate Values to a Config Owner and awaits its result.

        Args:
            owner_id: The Config Owner's stable identifier.
            values: Candidate Values to validate and, on success, write.
            timeout: Seconds to wait for a response before giving up.

        Returns:
            The Config Owner's `ValidateAndWriteResult`.

        Raises:
            TimeoutError: If no response arrives within `timeout` seconds.
        """
        ...

    def get_current_values(self, owner_id: str, timeout: float = 10.0) -> dict[str, Any]:
        """Fetches a Config Owner's current on-disk configuration values.

        Args:
            owner_id: The Config Owner's stable identifier.
            timeout: Seconds to wait for a response before giving up.

        Returns:
            The Config Owner's current configuration values.

        Raises:
            TimeoutError: If no response arrives within `timeout` seconds.
        """
        ...


class ConfigEditorServer:
    """Stdlib HTTP server exposing the Config Editor's discovery, rendering, and submit pages.

    All request handling is synchronous; the stdlib `ThreadingHTTPServer` is
    used so that concurrent browser requests do not block each other.

    Args:
        registry: The in-memory registry of discovered Config Owners this
            server renders from. Populated by `mqtt_client.py` in the running
            process; tests populate it directly.
        config_service_client: Forwards a submitted Config Owner's Candidate
            Values to `validate_and_write` and awaits the result. The running
            process passes its `ConfigEditorMqttClient`; tests pass a fake.
        port: TCP port to listen on. Pass 0 to let the OS assign a free port
            (useful in tests).
        allowed_ip: If set, requests from any other source IP are rejected
            with 403 Forbidden. Used by the HA add-on to restrict access to
            the ingress proxy's gateway address; None (the default) accepts
            every source IP.
    """

    def __init__(
        self,
        registry: ConfigOwnerRegistry,
        config_service_client: ConfigServiceClient,
        port: int = 0,
        allowed_ip: str | None = None,
    ) -> None:
        self._registry = registry
        self._config_service_client = config_service_client
        self._allowed_ip = allowed_ip

        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, headers, body = server_self.handle_request(
                    "GET", self.path, body=b"", client_ip=self.client_address[0]
                )
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                request_body = self.rfile.read(length) if length else b""
                status, headers, body = server_self.handle_request(
                    "POST", self.path, body=request_body, client_ip=self.client_address[0]
                )
                self._send(status, headers, body)

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug(format, *args)

        self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)

    @property
    def server_port(self) -> int:
        """Returns the actual TCP port the server is bound to."""
        return self._httpd.server_address[1]

    def serve_forever(self) -> None:
        """Starts serving requests. Blocks until shutdown() is called."""
        logger.info("config-editor listening on port %d", self.server_port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stops the server cleanly."""
        self._httpd.shutdown()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    def handle_request(
        self, method: str, path: str, body: bytes, client_ip: str | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatches a request and returns (status_code, headers, body_bytes).

        Called both by the real HTTP handler (`do_GET`) and directly by unit
        tests, which avoids the need for a live socket in unit tests.

        Args:
            method: HTTP method. "GET" and "POST" are supported.
            path: Request path, possibly with a query string (ignored).
            body: Raw request body bytes. Used only for POST, as a
                `application/x-www-form-urlencoded` body (the `<form>` in
                `templates/owner.html` submits with no explicit `enctype`,
                which defaults to this).
            client_ip: The requesting client's source IP, as supplied by the
                real HTTP handler's `client_address`. Checked against
                `allowed_ip` only when both are set; a caller (e.g. an
                existing unit test) that omits it is never restricted.

        Returns:
            A three-tuple of (HTTP status code, response headers, body bytes).
        """
        if self._allowed_ip and client_ip is not None and client_ip != self._allowed_ip:
            return self._html_response(403, "<h1>Forbidden</h1>")

        path = path.split("?")[0]

        # Fast rejection of obvious path traversal attempts before routing.
        # _safe_join performs the authoritative containment check downstream,
        # but catching these early avoids unnecessary routing work and makes
        # the intent clear to static analysis tools.
        if ".." in path or "\x00" in path:
            return self._html_response(403, "<h1>Forbidden</h1>")

        if method == "GET" and path == "/":
            return self._render_index()
        if method == "GET" and path.startswith("/static/"):
            return self._serve_static(path[len("/static/") :])
        if method == "GET" and path.startswith("/owners/"):
            owner_id = urllib.parse.unquote(path[len("/owners/") :])
            return self._render_owner(owner_id)
        if method == "POST" and path.startswith("/owners/"):
            owner_id = urllib.parse.unquote(path[len("/owners/") :])
            return self._submit_owner(owner_id, body)

        return self._html_response(404, "<h1>Not found</h1>")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _render_index(self) -> tuple[int, dict[str, str], bytes]:
        template = _TEMPLATES.get_template("index.html")
        html = template.render(owners=self._registry.all())
        return self._html_response(200, html)

    def _render_owner(self, owner_id: str) -> tuple[int, dict[str, str], bytes]:
        descriptor = self._registry.get(owner_id)
        if descriptor is None:
            return self._html_response(404, "<h1>Unknown Config Owner</h1>")

        try:
            current_values = self._config_service_client.get_current_values(owner_id)
        except TimeoutError:
            logger.warning("Timed out fetching current values from %r; showing schema defaults.", owner_id)
            current_values = None
        return self._render_owner_page(descriptor, values=current_values)

    def _submit_owner(self, owner_id: str, body: bytes) -> tuple[int, dict[str, str], bytes]:
        descriptor = self._registry.get(owner_id)
        if descriptor is None:
            return self._html_response(404, "<h1>Unknown Config Owner</h1>")

        raw = _parse_form_body(body)
        values = parse_submission(descriptor.form_spec, raw)

        try:
            result = self._config_service_client.submit_validate_and_write(owner_id, values)
        except TimeoutError:
            logger.warning("Timed out awaiting validate_and_write response from %r.", owner_id)
            errors = ["Timed out waiting for a response from this Config Owner."]
            return self._render_owner_page(descriptor, values=values, errors=errors)

        if result.success:
            return self._render_owner_page(descriptor, values=values, success=True)
        return self._render_owner_page(descriptor, values=values, errors=result.errors)

    def _render_owner_page(
        self,
        descriptor: Descriptor,
        *,
        values: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        success: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        template = _TEMPLATES.get_template("owner.html")
        html = template.render(
            descriptor=descriptor,
            groups=build_groups(descriptor, values=values),
            errors=errors or [],
            success=success,
        )
        return self._html_response(200, html)

    def _serve_static(self, relative_path: str) -> tuple[int, dict[str, str], bytes]:
        """Serves a vendored static asset (e.g. Bootstrap5's CSS/JS) with path traversal protection.

        Only files with an allowed extension (`.css`, `.js`, `.html`) are
        served; every other extension, and any path that resolves outside
        `_STATIC_DIR`, is rejected with 403.

        Args:
            relative_path: The request path with the `/static/` prefix
                already stripped, e.g. `vendor/bootstrap/bootstrap.min.css`.

        Returns:
            A three-tuple of (status, headers, body).
        """
        suffix = Path(relative_path).suffix
        if suffix not in _ALLOWED_STATIC_EXTENSIONS:
            return self._html_response(403, "<h1>Forbidden</h1>")

        resolved = _safe_join(_STATIC_DIR, relative_path)
        if resolved is None:
            return self._html_response(403, "<h1>Forbidden</h1>")

        if not resolved.exists():
            return self._html_response(404, "<h1>Not found</h1>")

        content_type = mimetypes.types_map.get(suffix, "application/octet-stream")
        return 200, {"Content-Type": content_type}, resolved.read_bytes()

    @staticmethod
    def _html_response(status: int, html: str) -> tuple[int, dict[str, str], bytes]:
        body = html.encode("utf-8")
        return status, {"Content-Type": "text/html; charset=utf-8"}, body


def _parse_form_body(body: bytes) -> dict[str, str]:
    """Parses an `application/x-www-form-urlencoded` POST body into a flat dict.

    Args:
        body: The raw request body.

    Returns:
        Each field name mapped to its (first, if repeated) submitted value.
        Every value is a string: the submitted form has no way to convey a
        field's real JSON Schema type, so type coercion is left to the
        Config Owner's own pydantic validation in `validate_and_write`.
    """
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items()}
