"""HTTP server for config-editor-v3.

Provides `ConfigEditorServer`, a stdlib-only HTTP server that renders the
Config Editor's discovery page and each Config Owner's read-only
configuration page directly from FormSpec, via Jinja2 templates
(`templates/index.html`, `templates/owner.html`), per ADR-0003 (no generic
client-side schema-to-form library):

    GET  /                    -- list every currently discovered Config Owner
    GET  /owners/<owner_id>   -- render one Config Owner's configuration

Submitting edits (`validate_and_write`) is not implemented yet; every field
is rendered as a read-only display of its FormSpec-described JSON Schema
default. This module never imports a Config Owner's pydantic model, and
never reads or writes any Config Owner's configuration file itself: it only
ever consumes the generic `Descriptor` a Config Owner published, obtained
from `registry.py`.
"""

from __future__ import annotations

import http.server
import logging
import urllib.parse
from typing import Any

import jinja2

from config_editor_v3.registry import ConfigOwnerRegistry
from config_editor_v3.render import build_groups

logger = logging.getLogger(__name__)

_TEMPLATES = jinja2.Environment(
    loader=jinja2.PackageLoader("config_editor_v3", "templates"),
    autoescape=jinja2.select_autoescape(["html"]),
)


class ConfigEditorServer:
    """Stdlib HTTP server exposing the Config Editor's discovery and rendering pages.

    All request handling is synchronous; the stdlib `ThreadingHTTPServer` is
    used so that concurrent browser requests do not block each other.

    Args:
        registry: The in-memory registry of discovered Config Owners this
            server renders from. Populated by `mqtt_client.py` in the running
            process; tests populate it directly.
        port: TCP port to listen on. Pass 0 to let the OS assign a free port
            (useful in tests).
    """

    def __init__(self, registry: ConfigOwnerRegistry, port: int = 0) -> None:
        self._registry = registry

        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                status, headers, body = server_self.handle_request("GET", self.path, body=b"")
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
            method: HTTP method. Only "GET" is currently supported.
            path: Request path, possibly with a query string (ignored).
            body: Raw request body bytes. Unused; no route currently accepts one.

        Returns:
            A three-tuple of (HTTP status code, response headers, body bytes).
        """
        path = path.split("?")[0]

        if method == "GET" and path == "/":
            return self._render_index()
        if method == "GET" and path.startswith("/owners/"):
            owner_id = urllib.parse.unquote(path[len("/owners/") :])
            return self._render_owner(owner_id)

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

        template = _TEMPLATES.get_template("owner.html")
        html = template.render(descriptor=descriptor, groups=build_groups(descriptor))
        return self._html_response(200, html)

    @staticmethod
    def _html_response(status: int, html: str) -> tuple[int, dict[str, str], bytes]:
        body = html.encode("utf-8")
        return status, {"Content-Type": "text/html; charset=utf-8"}, body
