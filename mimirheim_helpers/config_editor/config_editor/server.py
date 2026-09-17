"""HTTP server for config-editor.

Provides `ConfigEditorServer`, a stdlib-only HTTP server that renders the
Config Editor's discovery page and each Config Owner's configuration page
directly from FormSpec, via Jinja2 templates (`templates/index.html`,
`templates/owner.html`), per ADR-0003 (no generic client-side
schema-to-form library):

    GET  /owners/<owner_id>           -- render one Config Owner's configuration
    POST /owners/<owner_id>           -- submit Candidate Values for validate_and_write
    POST /owners/<owner_id>/restart   -- request that a Config Owner exit for its supervisor to restart it
    GET  /static/<path>               -- serve a vendored static asset (e.g. Bootstrap5)
    GET  /theme/<dark|light>          -- record the visitor's theme choice in a cookie

This module never imports a Config Owner's pydantic model, and never reads
or writes any Config Owner's configuration file itself: it only ever
consumes the generic `Descriptor` a Config Owner published (obtained from
`registry.py`), fetches that owner's current values via the injected
`ConfigServiceClient`'s `get_current_values` for a GET, and forwards a POST's
submitted values (parsed from their dotted/indexed form field names by
`config_editor.submission.parse_submission`) to the same client's
`submit_validate_and_write`. A successful Save never restarts a Config
Owner itself (ADR-0012's restart is always a conscious, separate action):
it only renders a message that a restart is required, and a POST to the
`/restart` route -- reached only via the owner page's own confirmation
modal, never automatically -- forwards to `submit_restart_request` instead.
`ConfigServiceClient` is the only thing here that touches MQTT.
"""

from __future__ import annotations

import http.cookies
import http.server
import logging
import mimetypes
import os
import urllib.parse
from pathlib import Path
from typing import Any, Protocol

import jinja2

from mimirheim_shared.config_service import Descriptor, RestartResponse, ValidateAndWriteResult
from mimirheim_shared.formspec import option_label

from config_editor.registry import ConfigOwnerRegistry
from config_editor.render import UNGROUPED_LABEL, build_tabs, visible_if_json
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
# render_groups (owner.html) compares against this to suppress a section's
# heading when it is the single implicit group, the same fallback build_groups
# uses for a field whose FieldSpec.group is unset.
_TEMPLATES.globals["UNGROUPED_LABEL"] = UNGROUPED_LABEL
# render_conditional_field (owner.html) embeds this in a data-visible-if
# attribute for visibility.js to parse and re-evaluate live on change.
_TEMPLATES.globals["visible_if_json"] = visible_if_json

# Vendored static assets (Bootstrap5's CSS/JS; see ADR-0003 -- no CDN, no
# generic client-side form library). Bundled with this package so the editor
# renders identically with no internet access.
_STATIC_DIR = Path(__file__).parent / "static"
# Only these extensions are served from the static directory; e.g. the
# vendored Bootstrap LICENSE file is deliberately not servable.
_ALLOWED_STATIC_EXTENSIONS = {".css", ".js", ".html"}

# The two theme names a visitor can pick via the toggle control and record in
# the "theme" cookie. Any other cookie value (stale, tampered, or from a
# future version) is treated the same as no cookie at all.
_VALID_THEMES = {"dark", "light"}


def _theme_from_cookie(cookie_header: str | None) -> str | None:
    """Extracts the "theme" cookie's value, if present and recognised.

    Args:
        cookie_header: The raw `Cookie` request header, or None if absent.

    Returns:
        "dark" or "light" if that is what the cookie carries, otherwise None
        (no cookie, no "theme" entry, or an unrecognised value) -- meaning
        the page should fall back to the OS/browser color-scheme preference.
    """
    if not cookie_header:
        return None
    cookies: http.cookies.SimpleCookie = http.cookies.SimpleCookie()
    cookies.load(cookie_header)
    morsel = cookies.get("theme")
    if morsel is None or morsel.value not in _VALID_THEMES:
        return None
    return morsel.value


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

    def submit_restart_request(self, owner_id: str, timeout: float = 10.0) -> RestartResponse:
        """Requests that a Config Owner exit for its supervisor to restart it.

        Args:
            owner_id: The Config Owner's stable identifier.
            timeout: Seconds to wait for an acknowledgement before giving up.

        Returns:
            The Config Owner's `RestartResponse`.

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
                    "GET",
                    self.path,
                    body=b"",
                    client_ip=self.client_address[0],
                    cookie=self.headers.get("Cookie"),
                )
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0))
                request_body = self.rfile.read(length) if length else b""
                status, headers, body = server_self.handle_request(
                    "POST",
                    self.path,
                    body=request_body,
                    client_ip=self.client_address[0],
                    cookie=self.headers.get("Cookie"),
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
        self,
        method: str,
        path: str,
        body: bytes,
        client_ip: str | None = None,
        cookie: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """Dispatches a request and returns (status_code, headers, body_bytes).

        Called both by the real HTTP handler (`do_GET`) and directly by unit
        tests, which avoids the need for a live socket in unit tests.

        Args:
            method: HTTP method. "GET" and "POST" are supported.
            path: Request path, optionally with a query string. The query
                string is ignored by every route except `/theme/<name>`,
                which reads a `next` parameter from it.
            body: Raw request body bytes. Used only for POST, as a
                `application/x-www-form-urlencoded` body (the `<form>` in
                `templates/owner.html` submits with no explicit `enctype`,
                which defaults to this).
            client_ip: The requesting client's source IP, as supplied by the
                real HTTP handler's `client_address`. Checked against
                `allowed_ip` only when both are set; a caller (e.g. an
                existing unit test) that omits it is never restricted.
            cookie: The raw `Cookie` request header, as supplied by the real
                HTTP handler. Used to read the visitor's "theme" override; a
                caller that omits it renders as if no cookie were sent.

        Returns:
            A three-tuple of (HTTP status code, response headers, body bytes).
        """
        if self._allowed_ip and client_ip is not None and client_ip != self._allowed_ip:
            return self._html_response(403, "<h1>Forbidden</h1>")

        path, _, query = path.partition("?")

        # Fast rejection of obvious path traversal attempts before routing.
        # _safe_join performs the authoritative containment check downstream,
        # but catching these early avoids unnecessary routing work and makes
        # the intent clear to static analysis tools.
        if ".." in path or "\x00" in path:
            return self._html_response(403, "<h1>Forbidden</h1>")

        theme = _theme_from_cookie(cookie)

        if method == "GET" and path == "/":
            return self._render_index(theme=theme)
        if method == "GET" and path.startswith("/static/"):
            return self._serve_static(path[len("/static/") :])
        if method == "GET" and path.startswith("/theme/"):
            return self._set_theme(path[len("/theme/") :], query)
        if method == "GET" and path.startswith("/owners/"):
            owner_id = urllib.parse.unquote(path[len("/owners/") :])
            return self._render_owner(owner_id, theme=theme)
        if method == "POST" and path.startswith("/owners/") and path.endswith("/restart"):
            owner_id = urllib.parse.unquote(path[len("/owners/") : -len("/restart")])
            return self._request_restart(owner_id, theme=theme)
        if method == "POST" and path.startswith("/owners/"):
            owner_id = urllib.parse.unquote(path[len("/owners/") :])
            return self._submit_owner(owner_id, body, theme=theme)

        return self._html_response(404, "<h1>Not found</h1>")

    # ------------------------------------------------------------------
    # Pages
    # ------------------------------------------------------------------

    def _render_index(self, *, theme: str | None) -> tuple[int, dict[str, str], bytes]:
        template = _TEMPLATES.get_template("index.html")
        html = template.render(owners=self._registry.all(), theme=theme, current_path="/")
        return self._html_response(200, html)

    def _render_owner(self, owner_id: str, *, theme: str | None) -> tuple[int, dict[str, str], bytes]:
        descriptor = self._registry.get(owner_id)
        if descriptor is None:
            return self._html_response(404, "<h1>Unknown Config Owner</h1>")

        try:
            current_values = self._config_service_client.get_current_values(owner_id)
        except TimeoutError:
            logger.warning("Timed out fetching current values from %r; showing schema defaults.", owner_id)
            current_values = None
        return self._render_owner_page(descriptor, values=current_values, theme=theme)

    def _submit_owner(
        self, owner_id: str, body: bytes, *, theme: str | None
    ) -> tuple[int, dict[str, str], bytes]:
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
            return self._render_owner_page(descriptor, values=values, errors=errors, theme=theme)

        if result.success:
            return self._render_owner_page(descriptor, values=values, success=True, theme=theme)
        return self._render_owner_page(descriptor, values=values, errors=result.errors, theme=theme)

    def _request_restart(self, owner_id: str, *, theme: str | None) -> tuple[int, dict[str, str], bytes]:
        """Requests that a Config Owner restart, reached only via the owner page's own confirmation modal.

        Restarting is never triggered by a successful Save (`_submit_owner`
        only ever renders a "restart required" message): it is always a
        separate, conscious action, per ADR-0012.

        Args:
            owner_id: The Config Owner's stable identifier.
            theme: The visitor's theme cookie override, forwarded to the
                re-rendered owner page.

        Returns:
            A three-tuple of (status, headers, body): the owner page,
            re-rendered with either a restart-requested confirmation or a
            timeout error.
        """
        descriptor = self._registry.get(owner_id)
        if descriptor is None:
            return self._html_response(404, "<h1>Unknown Config Owner</h1>")

        try:
            self._config_service_client.submit_restart_request(owner_id)
        except TimeoutError:
            logger.warning("Timed out awaiting restart acknowledgement from %r.", owner_id)
            errors = ["Timed out waiting for a restart acknowledgement from this Config Owner."]
            return self._render_owner_page(descriptor, theme=theme, errors=errors)

        # The owner is about to exit, so its own current values may no
        # longer be reachable; fall back to schema defaults rather than
        # surfacing a second, unrelated timeout error alongside the restart
        # confirmation.
        try:
            current_values = self._config_service_client.get_current_values(owner_id)
        except TimeoutError:
            current_values = None
        return self._render_owner_page(
            descriptor, values=current_values, restart_requested=True, theme=theme
        )

    def _render_owner_page(
        self,
        descriptor: Descriptor,
        *,
        values: dict[str, Any] | None = None,
        errors: list[str] | None = None,
        success: bool = False,
        restart_requested: bool = False,
        theme: str | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        template = _TEMPLATES.get_template("owner.html")
        html = template.render(
            descriptor=descriptor,
            tabs=build_tabs(descriptor, values=values),
            errors=errors or [],
            success=success,
            restart_requested=restart_requested,
            theme=theme,
            current_path=f"/owners/{urllib.parse.quote(descriptor.owner_id)}",
        )
        return self._html_response(200, html)


    def _set_theme(self, theme: str, query: str) -> tuple[int, dict[str, str], bytes]:
        """Records the visitor's theme choice in a cookie and redirects back.

        Args:
            theme: The path segment after `/theme/`, e.g. "dark" or "light".
            query: The request's raw query string, read for a `next`
                parameter naming the page to redirect back to.

        Returns:
            A 302 redirect to `next` (or "/" if absent or not same-origin),
            with a `Set-Cookie` header recording the chosen theme. 404 if
            `theme` is not a recognised theme name.
        """
        if theme not in _VALID_THEMES:
            return self._html_response(404, "<h1>Not found</h1>")

        next_path = urllib.parse.parse_qs(query).get("next", ["/"])[0]
        # Guards against an off-site open redirect: a leading "//" is parsed
        # by browsers as a protocol-relative URL (e.g. "//evil.example"), not
        # a same-origin path.
        if not next_path.startswith("/") or next_path.startswith("//"):
            next_path = "/"

        headers = {
            "Location": next_path,
            "Set-Cookie": f"theme={theme}; Path=/; Max-Age=31536000",
        }
        return 302, headers, b""

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
