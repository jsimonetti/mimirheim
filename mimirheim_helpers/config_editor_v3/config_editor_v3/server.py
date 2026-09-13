"""HTTP server for config-editor-v3.

Provides `ConfigEditorServer`, a stdlib-only HTTP server that renders the
Config Editor's discovery page and each Config Owner's configuration page
directly from FormSpec, via Jinja2 templates (`templates/index.html`,
`templates/owner.html`), per ADR-0003 (no generic client-side
schema-to-form library):

    GET  /owners/<owner_id>   -- render one Config Owner's configuration
    POST /owners/<owner_id>   -- submit Candidate Values for validate_and_write

This module never imports a Config Owner's pydantic model, and never reads
or writes any Config Owner's configuration file itself: it only ever
consumes the generic `Descriptor` a Config Owner published (obtained from
`registry.py`), fetches that owner's current values via the injected
`ConfigServiceClient`'s `get_current_values` for a GET, and forwards a POST's
submitted values (parsed from their dotted/indexed form field names by
`config_editor_v3.submission.parse_submission`) to the same client's
`submit_validate_and_write`. `ConfigServiceClient` is the only thing here
that touches MQTT.
"""

from __future__ import annotations

import http.server
import logging
import urllib.parse
from typing import Any, Protocol

import jinja2

from mimirheim_shared.config_service import Descriptor, ValidateAndWriteResult
from mimirheim_shared.formspec import option_label

from config_editor_v3.registry import ConfigOwnerRegistry
from config_editor_v3.render import build_groups
from config_editor_v3.submission import parse_submission

logger = logging.getLogger(__name__)

_TEMPLATES = jinja2.Environment(
    loader=jinja2.PackageLoader("config_editor_v3", "templates"),
    autoescape=jinja2.select_autoescape(["html"]),
)
# ENUM_SELECT rendering (owner.html) resolves each <option>'s display label
# via this, the same per-value-label lookup a FieldSpec's option_labels
# describes; exposed as a Jinja global rather than duplicating the lookup in
# the template itself.
_TEMPLATES.globals["option_label"] = option_label


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
    """

    def __init__(
        self,
        registry: ConfigOwnerRegistry,
        config_service_client: ConfigServiceClient,
        port: int = 0,
    ) -> None:
        self._registry = registry
        self._config_service_client = config_service_client

        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, headers, body = server_self.handle_request("GET", self.path, body=b"")
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                request_body = self.rfile.read(length) if length else b""
                status, headers, body = server_self.handle_request("POST", self.path, body=request_body)
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
        logger.info("config-editor-v3 listening on port %d", self.server_port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        """Stops the server cleanly."""
        self._httpd.shutdown()

    # ------------------------------------------------------------------
    # Request dispatch
    # ------------------------------------------------------------------

    def handle_request(self, method: str, path: str, body: bytes) -> tuple[int, dict[str, str], bytes]:
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

        Returns:
            A three-tuple of (HTTP status code, response headers, body bytes).
        """
        path = path.split("?")[0]

        if method == "GET" and path == "/":
            return self._render_index()
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
