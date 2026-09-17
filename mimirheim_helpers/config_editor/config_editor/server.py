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
    GET  /reports                     -- proxy the reporter's report index
    GET  /reports/<file>              -- proxy a single file from the reporter's output directory
    GET  /reports/dumps/<file>        -- proxy a solve dump referenced by a report's download links

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

The `/reports` routes are the one exception to "never reads another Config
Owner's configuration file": they proxy static report files the reporter
writes to disk (not its configuration), and they still learn *where* that
directory is by asking the reporter for its current values over the same
`get_current_values` protocol, never by reading reporter.yaml directly.
"""

from __future__ import annotations

import http.cookies
import http.server
import logging
import mimetypes
import os
import time
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

# Only these extensions are served from the reporter's output directory.
_ALLOWED_REPORT_EXTENSIONS = {".html", ".js", ".css"}

# Only these suffixes are served from the reporter's dump directory, so a
# report's download links work through this proxy.
_ALLOWED_DUMP_SUFFIXES = ("_input.json", "_output.json")

# The reporter's well-known Config Owner ID (see reporter/daemon.py). Not
# imported from the reporter package: this module never imports another
# Config Owner's internals, per this module's own docstring.
_REPORTER_OWNER_ID = "reporter"

# mimirheim core's well-known Config Owner ID (see mimirheim/io/config_service.py).
# Not imported from mimirheim core itself, for the same reason as
# _REPORTER_OWNER_ID above: used only to split the index page's owner list
# into a "Mimirheim" section and a "Helpers" section.
_MIMIRHEIM_CORE_OWNER_ID = "mimirheim-core"

# How long a fetched reporting section is reused before asking the reporter
# again. A single /reports page load serves several files (index, css, js,
# dump downloads), each needing this section; without a cache, that is an
# MQTT round trip per file. output_dir/dump_dir only change when a user edits
# reporter.yaml and restarts it, so a short cache is a safe trade.
_REPORTER_REPORTING_CACHE_TTL_S = 30.0

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

        # Cache for _reporter_reporting_section(); see
        # _REPORTER_REPORTING_CACHE_TTL_S. None means "never fetched yet".
        self._reporter_reporting_cache: dict[str, Any] | None = None
        self._reporter_reporting_cache_time: float = 0.0

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
        if method == "GET" and path in ("/reports", "/reports/"):
            return self._serve_reports_index()
        if method == "GET" and path.startswith("/reports/dumps/"):
            return self._serve_dump_file(path[len("/reports/dumps/") :])
        if method == "GET" and path.startswith("/reports/"):
            return self._serve_report_file(path[len("/reports/") :])
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
        owners = self._registry.all()
        core_owners = [owner for owner in owners if owner.owner_id == _MIMIRHEIM_CORE_OWNER_ID]
        helper_owners = [owner for owner in owners if owner.owner_id != _MIMIRHEIM_CORE_OWNER_ID]
        html = template.render(
            core_owners=core_owners, helper_owners=helper_owners, theme=theme, current_path="/"
        )
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

    def _reporter_reporting_section(self) -> dict[str, Any]:
        """Fetches the reporter Config Owner's live `reporting` section, cached briefly.

        Asks the reporter for its current configuration over the same
        `get_current_values` Config Service call the owner pages use, rather
        than reading reporter.yaml off disk directly -- this module never
        reads another Config Owner's configuration file itself. The result is
        cached for `_REPORTER_REPORTING_CACHE_TTL_S` seconds, since a single
        /reports page load needs it once per file served.

        Returns:
            The reporter's `reporting` dict, or `{}` if the reporter is
            unreachable (and nothing was ever cached) or has no `reporting`
            section yet.
        """
        now = time.monotonic()
        if (
            self._reporter_reporting_cache is not None
            and now - self._reporter_reporting_cache_time < _REPORTER_REPORTING_CACHE_TTL_S
        ):
            return self._reporter_reporting_cache

        try:
            values = self._config_service_client.get_current_values(_REPORTER_OWNER_ID)
        except TimeoutError:
            logger.warning("Timed out fetching the reporter's configuration; reports proxy unavailable.")
            # Keep serving the last known-good section rather than blanking
            # out /reports for the rest of the TTL over one transient
            # timeout; cache_time is left stale so the next call retries.
            return self._reporter_reporting_cache or {}

        section = values.get("reporting") or {}
        self._reporter_reporting_cache = section
        self._reporter_reporting_cache_time = now
        return section

    def _reporter_reporting_path(self, key: str) -> Path | None:
        """Reads a path from the reporter's cached `reporting` section.

        Args:
            key: The key to read from the reporter's `reporting` section,
                e.g. `"output_dir"` or `"dump_dir"`.

        Returns:
            The configured path, or None if the reporter is unreachable, has
            no `reporting` section yet, or does not set `key`.
        """
        value = self._reporter_reporting_section().get(key)
        return Path(value) if value else None

    @property
    def _reports_dir(self) -> Path | None:
        """The reporter's current reporting.output_dir, or None if unset/unreachable."""
        return self._reporter_reporting_path("output_dir")

    @property
    def _dump_dir(self) -> Path | None:
        """The reporter's current reporting.dump_dir, or None if unset/unreachable."""
        return self._reporter_reporting_path("dump_dir")

    def _serve_reports_index(self) -> tuple[int, dict[str, str], bytes]:
        """Serves the reporter's report index, proxied from its output directory.

        Any `target="_blank"` attribute is stripped from the response so
        that report links navigate within this page instead of popping out
        to a new browser tab. This works regardless of which version of
        index.html the reporter has written.
        """
        reports_dir = self._reports_dir
        if reports_dir is None:
            return self._html_response(404, "<h1>Reports not configured</h1>")
        index = reports_dir / "index.html"
        if not index.exists():
            return self._html_response(404, "<h1>Report index not found</h1>")
        content = index.read_bytes().replace(b' target="_blank"', b"")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, content

    def _serve_report_file(self, filename: str) -> tuple[int, dict[str, str], bytes]:
        """Serves a single file from the reporter's output directory.

        Only flat filenames with an allowed extension are served; see
        `_ALLOWED_REPORT_EXTENSIONS`. Path traversal is rejected the same
        way `_serve_static` rejects it, via `_safe_join`.

        Args:
            filename: Bare filename extracted from the request path.

        Returns:
            A three-tuple of (status, headers, body).
        """
        reports_dir = self._reports_dir
        if reports_dir is None:
            return self._html_response(404, "<h1>Reports not configured</h1>")
        suffix = Path(filename).suffix
        if suffix not in _ALLOWED_REPORT_EXTENSIONS:
            return self._html_response(403, "<h1>Forbidden</h1>")
        resolved = _safe_join(reports_dir, filename)
        if resolved is None:
            return self._html_response(403, "<h1>Forbidden</h1>")
        if not resolved.exists():
            return self._html_response(404, "<h1>Not found</h1>")
        content_type = mimetypes.types_map.get(suffix, "application/octet-stream")
        return 200, {"Content-Type": content_type}, resolved.read_bytes()

    def _serve_dump_file(self, filename: str) -> tuple[int, dict[str, str], bytes]:
        """Serves a solve dump JSON file from the reporter's dump directory.

        Only flat filenames ending in `_input.json` or `_output.json` are
        served, so a report's own download links work through this proxy.

        Args:
            filename: Bare filename extracted from the request path.

        Returns:
            A three-tuple of (status, headers, body).
        """
        dump_dir = self._dump_dir
        if dump_dir is None:
            return self._html_response(404, "<h1>Dump directory not configured</h1>")
        if not any(filename.endswith(suffix) for suffix in _ALLOWED_DUMP_SUFFIXES):
            return self._html_response(403, "<h1>Forbidden</h1>")
        resolved = _safe_join(dump_dir, filename)
        if resolved is None:
            return self._html_response(403, "<h1>Forbidden</h1>")
        if not resolved.exists():
            return self._html_response(404, "<h1>Not found</h1>")
        headers = {
            "Content-Type": "application/json",
            # resolved.name is the final path component after symlink
            # resolution and containment verification -- safe for use in
            # the Content-Disposition header.
            "Content-Disposition": f'attachment; filename="{resolved.name}"',
        }
        return 200, headers, resolved.read_bytes()

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
