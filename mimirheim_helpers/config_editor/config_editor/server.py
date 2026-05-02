"""HTTP server for the mimirheim config editor.

This module provides ConfigEditorServer, a lightweight HTTP server that serves
the static frontend files and JSON API endpoints:

    GET  /api/schema                   — MimirheimConfig JSON Schema (cached)
    GET  /api/config                   — current mimirheim.yaml as parsed dict
    POST /api/config                   — validate via Pydantic, write YAML
    GET  /api/helper-configs           — enabled/config for all known helpers
    GET  /api/helper-schemas           — JSON Schema for every helper config
    POST /api/helper-config/<filename> — enable (write) or disable (delete) a helper

The server uses only Python stdlib (http.server, threading, json, yaml).
No external web framework is required.

What this module does not do:
- It does not authenticate users. The editor is designed for trusted private
  networks only.
- It does not serve files outside the static/ directory.
- It does not parse MQTT messages or interact with the solver.
"""
from __future__ import annotations

import http.server
import json
import logging
import mimetypes
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError as PydanticValidationError
from ruamel.yaml import YAML

from mimirheim.config.schema import MimirheimConfig

logger = logging.getLogger(__name__)

# Only these extensions are served from the static directory.
_ALLOWED_STATIC_EXTENSIONS = {".js", ".css", ".html"}

# Only these extensions are served from the reports directory.
_ALLOWED_REPORT_EXTENSIONS = {".html", ".js", ".css"}

# Only these suffixes are served from the dump directory.
_ALLOWED_DUMP_SUFFIXES = ("_input.json", "_output.json")

# Path to the static files bundled with this package.
_STATIC_DIR = Path(__file__).parent / "static"


def _safe_join(base: Path, filename: str) -> Path | None:
    """Resolve ``filename`` relative to ``base`` and verify containment.

    Validates that ``filename`` is a plain filename with no directory components,
    then resolves the joined path and calls ``relative_to`` to confirm it stays
    inside ``base``. Returns ``None`` on any failure.

    The individual checks are written as explicit conditionals rather than a
    helper function so that CodeQL's taint-tracking engine can follow each
    condition and recognise ``filename`` as sanitized before it reaches any
    path construction or I/O.

    Args:
        base: The directory that the result must stay inside.
        filename: A bare filename from an HTTP request or config key.

    Returns:
        Resolved ``Path`` inside ``base``, or ``None`` if validation fails.
    """
    # Reject empty strings.
    if not filename:
        return None
    # Reject forward slashes (Unix path separator).
    if "/" in filename:
        return None
    # Reject backslashes (Windows path separator).
    if "\\" in filename:
        return None
    # Reject anything that is not already its own final path component.
    # Path(filename).name strips leading directory parts on every platform;
    # if the result differs from the input, the filename contained a directory
    # prefix. CodeQL recognises this comparison as a sanitizer because after
    # this guard the analyser can infer filename == Path(filename).name.
    if Path(filename).name != filename:
        return None
    base_dir = base.resolve()
    fpath = (base_dir / Path(filename).name).resolve()
    try:
        fpath.relative_to(base_dir)
    except ValueError:
        return None
    return fpath


def _write_yaml_preserving_comments(
    data: dict[str, Any], file_path: Path
) -> str:
    """Write YAML file while preserving existing comments and formatting.

    If the file exists, loads it with ruamel.yaml to preserve comments,
    updates values in-place, then writes back. If the file doesn't exist,
    creates a new formatted YAML file.

    Args:
        data: Dictionary to write as YAML.
        file_path: Path where the YAML file will be written.

    Returns:
        The YAML string that was written.
    """
    yaml_handler = YAML()
    yaml_handler.default_flow_style = False
    yaml_handler.preserve_quotes = True
    yaml_handler.width = 4096  # Prevent line wrapping

    if file_path.exists():
        # Load existing file to preserve comments and structure
        try:
            with file_path.open("r") as f:
                existing = yaml_handler.load(f)
        except Exception:
            # File is malformed or unreadable: write fresh
            existing = None
        
        if existing is not None:
            # Deep merge: update existing structure with new values
            def deep_merge(target: Any, source: dict) -> None:
                """Recursively update target dict with values from source.
                
                Updates values, adds new keys, and removes keys not in source.
                """
                if not isinstance(target, dict) or not isinstance(source, dict):
                    return
                
                # Remove keys that are in target but not in source
                keys_to_remove = [k for k in target.keys() if k not in source]
                for key in keys_to_remove:
                    del target[key]
                
                # Update or add keys from source
                for key, value in source.items():
                    if key in target and isinstance(target[key], dict) and isinstance(value, dict):
                        deep_merge(target[key], value)
                    else:
                        target[key] = value
            
            deep_merge(existing, data)
            merged = existing
        else:
            # File was empty or malformed
            merged = data
    else:
        # New file: just use the provided data
        merged = data

    # Write to string first to get the output for logging
    import io
    stream = io.StringIO()
    yaml_handler.dump(merged, stream)
    yaml_str = stream.getvalue()
    
    # Now write atomically to disk
    fd, tmp_path = tempfile.mkstemp(
        dir=file_path.parent, suffix=".yaml.tmp"
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(yaml_str)
        os.replace(tmp_path, file_path)
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    
    return yaml_str


# ---------------------------------------------------------------------------
# Helper config registry
#
# Maps each known helper config filename to (Pydantic model, competing_files).
# Competing files are the other baseload variants; enabling one deletes them.
# Imports are deferred to avoid loading optional heavy dependencies at startup.
# Any filename not in this dict is rejected as 400 on POST.
# ---------------------------------------------------------------------------

def _load_helper_models() -> dict[str, tuple[Any, list[str]]]:
    """Import and return all known helper Pydantic config classes.

    Returns a dict mapping config filename to (model_class, competing_filenames).
    Competing filenames apply only to the mutually-exclusive baseload variants:
    enabling one deletes the others.

    Helper packages that are not installed are omitted silently so that the
    server starts in minimal environments.
    """
    result: dict[str, tuple[Any, list[str]]] = {}
    _baseload_variants = ["baseload-static.yaml", "baseload-ha.yaml", "baseload-ha-db.yaml"]

    try:
        from nordpool.config import NordpoolConfig
        result["nordpool.yaml"] = (NordpoolConfig, [])
    except ImportError:
        pass

    try:
        from pv_fetcher.config import PvFetcherConfig
        result["pv-fetcher.yaml"] = (PvFetcherConfig, [])
    except ImportError:
        pass

    try:
        from pv_ml_learner.config import PvLearnerConfig
        result["pv-ml-learner.yaml"] = (PvLearnerConfig, [])
    except ImportError:
        pass

    try:
        from baseload_static.config import BaseloadConfig as BaseloadStaticConfig
        result["baseload-static.yaml"] = (
            BaseloadStaticConfig,
            [f for f in _baseload_variants if f != "baseload-static.yaml"],
        )
    except ImportError:
        pass

    try:
        from baseload_ha.config import BaseloadConfig as BaseloadHaConfig
        result["baseload-ha.yaml"] = (
            BaseloadHaConfig,
            [f for f in _baseload_variants if f != "baseload-ha.yaml"],
        )
    except ImportError:
        pass

    try:
        from baseload_ha_db.config import BaseloadConfig as BaseloadHaDbConfig
        result["baseload-ha-db.yaml"] = (
            BaseloadHaDbConfig,
            [f for f in _baseload_variants if f != "baseload-ha-db.yaml"],
        )
    except ImportError:
        pass

    try:
        from reporter.config import ReporterConfig
        result["reporter.yaml"] = (ReporterConfig, [])
    except ImportError:
        pass

    try:
        from scheduler.config import SchedulerConfig
        result["scheduler.yaml"] = (SchedulerConfig, [])
    except ImportError:
        pass

    return result


class ConfigEditorServer:
    """Lightweight HTTP server for the mimirheim config editor.

    Serves static files and three JSON API endpoints. All request handling
    is synchronous; the stdlib ThreadingHTTPServer is used so that concurrent
    browser requests do not block each other.

    The schema is computed once at construction time and cached for the
    lifetime of the server instance.

    Args:
        config_dir: Directory where mimirheim.yaml is read from and written to.
        port: TCP port to listen on. Pass 0 to let the OS assign a free port
            (useful in tests).
    """

    def __init__(
        self,
        config_dir: Path,
        port: int,
        allowed_ip: str | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._allowed_ip = allowed_ip
        self._reports_dir: Path | None = self._detect_reports_dir()
        self._dump_dir: Path | None = self._detect_dump_dir()
        self._schema: dict[str, Any] = MimirheimConfig.model_json_schema()

        # Load helper model registry. Dict maps filename → (model_cls, competitors).
        self._helper_models: dict[str, tuple[Any, list[str]]] = _load_helper_models()

        # Pre-compute helper schemas once — these are expensive for some models
        # (pv_ml_learner has many nested $defs).
        self._helper_schemas: dict[str, Any] = {
            fname: model_cls.model_json_schema()
            for fname, (model_cls, _) in self._helper_models.items()
        }

        # Build the actual HTTP server. handler_factory creates a closure over
        # self so the handler can call _dispatch without global state.
        server_self = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if server_self._allowed_ip and self.client_address[0] != server_self._allowed_ip:
                    self.send_response(403)
                    self.end_headers()
                    return
                status, headers, body = server_self.handle_request("GET", self.path, body=b"")
                self._send(status, headers, body)

            def do_POST(self) -> None:  # noqa: N802
                if server_self._allowed_ip and self.client_address[0] != server_self._allowed_ip:
                    self.send_response(403)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                status, headers, body = server_self.handle_request("POST", self.path, body=raw)
                self._send(status, headers, body)

            def _send(self, status: int, headers: dict[str, str], body: bytes) -> None:
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                logger.debug(format, *args)

        self._httpd = http.server.ThreadingHTTPServer(("0.0.0.0", port), _Handler)

    @property
    def server_port(self) -> int:
        """Return the actual TCP port the server is bound to."""
        return self._httpd.server_address[1]

    def _read_reporter_yaml(self) -> dict:
        """Read and parse reporter.yaml from the config directory.

        Returns an empty dict if the file is absent or cannot be parsed.
        """
        reporter_yaml = self._config_dir / "reporter.yaml"
        if not reporter_yaml.exists():
            return {}
        try:
            return yaml.safe_load(reporter_yaml.read_text()) or {}
        except yaml.YAMLError:
            return {}

    def _detect_reports_dir(self) -> Path | None:
        """Return reporting.output_dir from reporter.yaml, or None if absent."""
        output_dir = (self._read_reporter_yaml().get("reporting") or {}).get("output_dir")
        return Path(output_dir) if output_dir else None

    def _detect_dump_dir(self) -> Path | None:
        """Return reporting.dump_dir from reporter.yaml, or None if absent."""
        dump_dir = (self._read_reporter_yaml().get("reporting") or {}).get("dump_dir")
        return Path(dump_dir) if dump_dir else None

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
        unit tests.

        Args:
            method: HTTP method ("GET" or "POST").
            path: Request path, possibly with query string (query string is
                ignored).
            body: Raw request body bytes.

        Returns:
            A three-tuple of (HTTP status code, response headers dict, body bytes).
        """
        # Strip query string.
        path = path.split("?")[0]

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
        if method == "GET" and path == "/api/schema":
            return self._api_get_schema()
        if method == "GET" and path == "/api/config":
            return self._api_get_config()
        if method == "POST" and path == "/api/config":
            return self._api_post_config(body)
        if method == "GET" and path == "/api/helper-configs":
            return self._api_get_helper_configs()
        if method == "GET" and path == "/api/helper-schemas":
            return self._api_get_helper_schemas()
        if method == "POST" and path.startswith("/api/helper-config/"):
            filename = path[len("/api/helper-config/"):]
            return self._api_post_helper_config(filename, body)

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

        Only flat filenames are accepted — no path separators or traversal
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
        directly from the filesystem, these links will 404 — users who need
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
                # resolution and containment verification — safe for use in
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
    # API endpoints
    # ------------------------------------------------------------------

    def _api_get_schema(self) -> tuple[int, dict[str, str], bytes]:
        """Return the cached MimirheimConfig JSON Schema."""
        return self._json_response(200, self._schema)

    def _api_get_config(self) -> tuple[int, dict[str, str], bytes]:
        """Read mimirheim.yaml and return its parsed contents.

        Does not validate via Pydantic so that partially-complete configs
        written by the user are returned as-is for display in the frontend.

        The ``mqtt_env`` key in the response contains MQTT broker settings
        currently set via environment variables (injected by the HA Supervisor).
        The frontend uses these to show which fields are Supervisor-controlled
        and to strip them from the saved YAML when the user has not overridden
        them.

        Returns:
            ``{"exists": false, "config": {}, "mqtt_env": {...}}`` when the
            file is absent.
            ``{"exists": true, "config": <dict>, "mqtt_env": {...}}`` when the
            file is present.
        """
        mqtt_env = self._mqtt_env()
        reports_available = (
            self._reports_dir is not None
            and (self._reports_dir / "index.html").exists()
        )
        yaml_path = self._config_dir / "mimirheim.yaml"
        if not yaml_path.exists():
            return self._json_response(
                200,
                {"exists": False, "config": {}, "mqtt_env": mqtt_env, "reports_available": reports_available},
            )

        try:
            raw = yaml.safe_load(yaml_path.read_text()) or {}
        except yaml.YAMLError as exc:
            logger.warning("Failed to parse mimirheim.yaml: %s", exc)
            return self._json_response(
                200,
                {"exists": True, "config": {}, "mqtt_env": mqtt_env, "reports_available": reports_available},
            )

        return self._json_response(
            200,
            {"exists": True, "config": raw, "mqtt_env": mqtt_env, "reports_available": reports_available},
        )

    def _api_post_config(self, body: bytes) -> tuple[int, dict[str, str], bytes]:
        """Validate a JSON config body and write it to mimirheim.yaml.

        Validation is performed via MimirheimConfig.model_validate. If
        validation fails, HTTP 422 is returned with a list of Pydantic error
        dicts; the file is not written.

        When MQTT env vars are set (HA Supervisor context), the submitted config
        may omit mqtt fields that are provided by the Supervisor at runtime.
        The server merges env-supplied mqtt fields into a validation-only copy
        before calling Pydantic; only the original submitted data is written to
        disk, keeping Supervisor credentials out of the YAML file.

        On success, the config is serialised to YAML and written atomically:
        a temp file is written in the same directory, then os.replace() moves
        it into place. This prevents a partial file being visible to the
        solver if the container is restarted mid-write.

        Args:
            body: Raw JSON bytes.

        Returns:
            HTTP 200 {"ok": true} on success.
            HTTP 422 {"ok": false, "errors": [...]} on validation failure.
            HTTP 400 on malformed JSON.
        """
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json_response(400, {"ok": False, "errors": str(exc)})

        # Merge env-supplied MQTT fields into a validation-only copy. The user
        # may have excluded mqtt fields that the Supervisor provides at runtime;
        # without the merge, Pydantic would reject the config as incomplete.
        mqtt_env = self._mqtt_env()
        if mqtt_env:
            validate_data: dict = dict(data)
            validate_data["mqtt"] = {**mqtt_env, **dict(data.get("mqtt") or {})}
        else:
            validate_data = data

        try:
            MimirheimConfig.model_validate(validate_data)
        except PydanticValidationError as exc:
            return self._json_response(422, {"ok": False, "errors": exc.errors()})

        yaml_path = self._config_dir / "mimirheim.yaml"
        yaml_str = _write_yaml_preserving_comments(data, yaml_path)

        logger.info("Wrote mimirheim.yaml (%d bytes)", len(yaml_str))
        return self._json_response(200, {"ok": True})

    # ------------------------------------------------------------------
    # Helper config endpoints
    # ------------------------------------------------------------------

    def _api_get_helper_configs(self) -> tuple[int, dict[str, str], bytes]:
        """Return enabled status and parsed config for every known helper.

        A helper is enabled when its config file exists in config_dir. The
        config contents are returned as-is (no Pydantic validation on GET)
        so that partially-complete files are still displayed in the frontend.
        """
        result: dict[str, Any] = {}
        for fname in self._helper_models:
            fpath = self._config_dir / fname
            if fpath.exists():
                try:
                    raw = yaml.safe_load(fpath.read_text()) or {}
                except yaml.YAMLError:
                    raw = {}
                result[fname] = {"enabled": True, "config": raw}
            else:
                result[fname] = {"enabled": False, "config": {}}
        return self._json_response(200, result)

    def _api_get_helper_schemas(self) -> tuple[int, dict[str, str], bytes]:
        """Return the pre-computed JSON Schema for every known helper config."""
        return self._json_response(200, self._helper_schemas)

    def _api_post_helper_config(
        self, filename: str, body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """Enable (write) or disable (delete) a helper config file.

        The filename must be present in the hardcoded helper allowlist. Any
        other value returns 400 to prevent path traversal or arbitrary file
        writes.

        Request body schema:
            {"enabled": false}                          — delete the config file
            {"enabled": true, "config": { ... }}        — validate and write

        For baseload variants, enabling one automatically deletes the other
        two variants to enforce the mutual-exclusion rule.

        Args:
            filename: Config filename extracted from the URL path.
            body: Raw JSON bytes.

        Returns:
            HTTP 200 {"ok": true} on success.
            HTTP 400 if filename is not in the allowlist.
            HTTP 422 {"ok": false, "errors": [...]} on Pydantic validation failure.
        """
        if filename not in self._helper_models:
            return self._json_response(400, {"ok": False, "error": "unknown helper filename"})

        try:
            data = json.loads(body)
        except (json.JSONDecodeError, ValueError) as exc:
            return self._json_response(400, {"ok": False, "errors": str(exc)})

        enabled = data.get("enabled", True)
        fpath = _safe_join(self._config_dir, filename)
        if fpath is None:
            return self._json_response(400, {"ok": False, "error": "invalid filename"})

        if not enabled:
            # Disable: delete the file if it exists.
            if fpath.exists():
                try:
                    fpath.unlink()
                    logger.info("Deleted %s", filename)
                except OSError as exc:
                    return self._json_response(500, {"ok": False, "error": str(exc)})
            return self._json_response(200, {"ok": True})

        # Enable: validate then write atomically.
        config_dict = data.get("config", {})
        model_cls, competitors = self._helper_models[filename]

        # Merge env-supplied MQTT fields for validation only. Helper configs may
        # omit mqtt fields that the Supervisor provides at runtime.
        mqtt_env = self._mqtt_env()
        if mqtt_env:
            validate_dict: dict = dict(config_dict)
            validate_dict["mqtt"] = {**mqtt_env, **dict(config_dict.get("mqtt") or {})}
        else:
            validate_dict = config_dict

        try:
            model_cls.model_validate(validate_dict)
        except PydanticValidationError as exc:
            return self._json_response(422, {"ok": False, "errors": exc.errors()})

        yaml_str = _write_yaml_preserving_comments(config_dict, fpath)

        # Delete mutually-exclusive variants (baseload only).
        for competing_fname in competitors:
            competing_path = _safe_join(self._config_dir, competing_fname)
            if competing_path is None:
                logger.warning("Skipping invalid competing filename %s", competing_fname)
                continue
            if competing_path.exists():
                try:
                    competing_path.unlink()
                    logger.info("Deleted competing baseload variant %s", competing_fname)
                except OSError as exc:
                    logger.warning("Failed to delete %s: %s", competing_fname, exc)

        logger.info("Wrote %s (%d bytes)", filename, len(yaml_str))
        return self._json_response(200, {"ok": True})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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
        env: dict[str, Any] = {}
        if host := os.environ.get("MQTT_HOST"):
            env["host"] = host
        if port_str := os.environ.get("MQTT_PORT"):
            try:
                env["port"] = int(port_str)
            except ValueError:
                pass
        if username := os.environ.get("MQTT_USERNAME"):
            env["username"] = username
        if password := os.environ.get("MQTT_PASSWORD"):
            env["password"] = password
        if ssl_str := os.environ.get("MQTT_SSL"):
            env["tls"] = ssl_str.lower() == "true"
        return env
