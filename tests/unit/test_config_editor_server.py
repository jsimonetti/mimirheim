"""Unit tests for the config editor HTTP server.

Tests instantiate ConfigEditorServer directly using a temp directory as
config_dir. No subprocess is spawned; all requests are dispatched in-process
via the handler's internal dispatch logic.

What these tests do not cover:
- Full HTTP round-trips with a live socket (see test_config_editor_crud_generic.py)
- Frontend JavaScript rendering
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from config_editor.server import MQTT_ENV_REDACTED, ConfigEditorServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_server(tmp_path: Path) -> ConfigEditorServer:
    """Return a ConfigEditorServer wired to a temp config directory."""
    return ConfigEditorServer(config_dir=tmp_path, port=0)


def _dispatch_get(server: ConfigEditorServer, path: str) -> tuple[int, dict[str, str], bytes]:
    """Simulate a GET request and return (status_code, headers, body)."""
    return server.handle_request("GET", path, body=b"")


def _dispatch_post(
    server: ConfigEditorServer, path: str, body: Any
) -> tuple[int, dict[str, str], bytes]:
    """Simulate a POST request with a JSON body."""
    raw = json.dumps(body).encode()
    return server.handle_request("POST", path, body=raw)


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------


def test_static_path_traversal_returns_400(tmp_path: Path) -> None:
    """GET /static/../config.py must return 400 (rejected before routing)."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/static/../config.py")
    assert status == 400


# ---------------------------------------------------------------------------
# Content-Security-Policy (SPEC.md §11, plan 69)
#
# Every response -- success, 404, 403 IP-mismatch, 400 malformed request --
# must carry the header, since a route added later that forgets it would
# otherwise ship unnoticed. The full "does Jedison still render under this
# policy" check is manual (plan 69 Step 6); this only checks the header's
# presence and that it actually blocks inline/remote script execution by
# construction (no 'unsafe-inline'/'unsafe-eval', no non-'self' host).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/", b""),
        ("GET", "/api/registry", b""),
        ("GET", "/api/entry/mimirheim", b""),
        ("GET", "/does-not-exist", b""),
        ("POST", "/api/save", b"not json"),
    ],
)
def test_every_response_carries_csp_header(
    tmp_path: Path, method: str, path: str, body: bytes
) -> None:
    """Every response, success or error, carries a Content-Security-Policy header."""
    server = _make_server(tmp_path)
    status, headers, _ = server.handle_request(method, path, body=body)
    assert "Content-Security-Policy" in headers


def test_csp_header_blocks_inline_and_remote_script() -> None:
    """The shipped policy has no unsafe-inline/unsafe-eval and no non-'self' host."""
    from config_editor.server import _CSP_HEADER_VALUE

    assert "'unsafe-inline'" not in _CSP_HEADER_VALUE
    assert "'unsafe-eval'" not in _CSP_HEADER_VALUE
    directives = [d.strip() for d in _CSP_HEADER_VALUE.split(";") if d.strip()]
    for directive in directives:
        parts = directive.split()
        name, sources = parts[0], parts[1:]
        if name in ("object-src", "base-uri"):
            assert sources == ["'none'"], directive
        else:
            assert sources == ["'self'"], directive


def test_ip_mismatch_403_carries_csp_header(tmp_path: Path) -> None:
    """The IP-allowlist short-circuit in do_GET/do_POST also gets the header.

    That path bypasses handle_request entirely, so it needs its own coverage
    -- see server.py's _Handler.do_GET/do_POST.
    """
    server = ConfigEditorServer(config_dir=tmp_path, port=0, allowed_ip="10.0.0.1")
    handler_cls = server._httpd.RequestHandlerClass

    class _FakeHandler(handler_cls):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            self.client_address = ("10.0.0.2", 12345)
            self.sent_headers: dict[str, str] = {}
            self.status: int | None = None

        def send_response(self, status: int, message: str | None = None) -> None:
            self.status = status

        def send_header(self, key: str, value: str) -> None:
            self.sent_headers[key] = value

        def end_headers(self) -> None:
            pass

        class _NullWfile:
            def write(self, _data: bytes) -> None:
                pass

        wfile = _NullWfile()

    fake = _FakeHandler()
    fake.do_GET()
    assert fake.status == 403
    assert "Content-Security-Policy" in fake.sent_headers


# ---------------------------------------------------------------------------
# Shared fixture data (used by the /api/save, /api/preview, and /api/entry
# tests below).
# ---------------------------------------------------------------------------

_MINIMAL_NORDPOOL = {
    "mqtt": {"host": "localhost", "client_id": "np"},
    "trigger_topic": "mimirheim/trigger",
    "nordpool": {"area": "NL"},
}


# ---------------------------------------------------------------------------
# MQTT env var handling not already covered by the /api/save redaction tests
# below (test_post_save_strips_placeholder_and_merges_env_for_validation_only,
# test_post_save_keeps_a_password_the_user_actually_typed).
# ---------------------------------------------------------------------------


def test_post_save_env_does_not_override_explicit_mqtt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both env and submitted config contain mqtt.host, the submitted value is saved.

    _redact_and_merge_entries only ever merges env-supplied fields into the
    validation-only copy, never into what gets written -- this holds for any
    mqtt field, not just the password-specific cases covered elsewhere.
    """
    monkeypatch.setenv("MQTT_HOST", "env-broker")
    server = _make_server(tmp_path)
    config_with_mqtt = {
        "mqtt": {"host": "my-broker", "client_id": "np"},
        "trigger_topic": "mimirheim/trigger",
        "nordpool": {"area": "NL"},
    }
    status, _headers, body = _dispatch_post(
        server,
        "/api/save",
        {"entries": {"nordpool": {"enabled": True, "config": config_with_mqtt}}},
    )
    assert status == 200, json.loads(body)
    loaded = yaml.safe_load((tmp_path / "nordpool.yaml").read_text())
    # User's explicit value must be written, not the env value.
    assert loaded["mqtt"]["host"] == "my-broker"


# ---------------------------------------------------------------------------
# x-enumSource metadata in helper schemas (formerly ui_source, migrated by
# plan 68 Decision 10 -- see tests/unit/test_schema_json_drift.py and
# mimirheim_helpers/config_editor/tests/unit/test_schema_drift.py for the
# corresponding schema.json / bundled-schema drift coverage).
# ---------------------------------------------------------------------------


def _get_entry_schema(tmp_path: Path, entry_id: str) -> dict[str, Any]:
    """Return the parsed schema for one entry via GET /api/entry/<id>."""
    server = _make_server(tmp_path)
    status, _, body = _dispatch_get(server, f"/api/entry/{entry_id}")
    assert status == 200
    return json.loads(body)["schema"]


def test_every_helper_schema_has_a_title(tmp_path: Path) -> None:
    """Every discovered helper entry's schema declares a 'title' (excludes mimirheim itself,
    which is covered by test_get_entry_mimirheim_schema_title_matches_the_model below).
    """
    for entry_id in _ALL_ENTRY_IDS - {"mimirheim"}:
        schema = _get_entry_schema(tmp_path, entry_id)
        assert "title" in schema, f"{entry_id} schema is missing 'title'"


def test_baseload_static_mimir_static_load_name_has_enum_source(tmp_path: Path) -> None:
    """baseload-static schema points mimir_static_load_name at #/context/static_loads."""
    schema = _get_entry_schema(tmp_path, "baseload-static")
    field = schema["properties"]["mimir_static_load_name"]
    assert field.get("x-enumSource") == "#/context/static_loads"


def test_baseload_ha_mimir_static_load_name_has_enum_source(tmp_path: Path) -> None:
    """baseload-ha schema points mimir_static_load_name at #/context/static_loads."""
    schema = _get_entry_schema(tmp_path, "baseload-ha")
    field = schema["properties"]["mimir_static_load_name"]
    assert field.get("x-enumSource") == "#/context/static_loads"


def test_baseload_ha_db_mimir_static_load_name_has_enum_source(tmp_path: Path) -> None:
    """baseload-ha-db schema points mimir_static_load_name at #/context/static_loads."""
    schema = _get_entry_schema(tmp_path, "baseload-ha-db")
    field = schema["properties"]["mimir_static_load_name"]
    assert field.get("x-enumSource") == "#/context/static_loads"


def test_pv_fetcher_array_output_topic_has_enum_source(tmp_path: Path) -> None:
    """pv-fetcher ArrayConfig.output_topic points at #/context/pv_arrays."""
    schema = _get_entry_schema(tmp_path, "pv-fetcher")
    field = schema["$defs"]["ArrayConfig"]["properties"]["output_topic"]
    assert field.get("x-enumSource") == "#/context/pv_arrays"


def test_pv_ml_learner_array_output_topic_has_enum_source(tmp_path: Path) -> None:
    """pv-ml-learner ArrayConfig.output_topic points at #/context/pv_arrays."""
    schema = _get_entry_schema(tmp_path, "pv-ml-learner")
    field = schema["$defs"]["ArrayConfig"]["properties"]["output_topic"]
    assert field.get("x-enumSource") == "#/context/pv_arrays"


# ---------------------------------------------------------------------------
# The Supervisor's broker password must not cross the wire
#
# GET /api/entry/<id> carries a redacted "mqtt_env" key (plan 69) alongside
# {"schema", "value", "enabled"} so the frontend can render a
# Supervisor-provided-password placeholder for any entry -- not only
# mimirheim.yaml's, since every helper's schema may declare its own "mqtt"
# section too. Every actual credential value is still replaced by
# MQTT_ENV_REDACTED before it ever reaches this response.
# ---------------------------------------------------------------------------


def test_get_entry_mqtt_env_reports_redacted_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/entry/<id>'s mqtt_env carries the redaction sentinel, not the real password."""
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PASSWORD", "SuperSecret123")
    server = _make_server(tmp_path)

    status, _, body = _dispatch_get(server, "/api/entry/mimirheim")

    assert status == 200
    mqtt_env = json.loads(body)["mqtt_env"]
    assert mqtt_env["host"] == "core-mosquitto"
    assert mqtt_env["password"] == MQTT_ENV_REDACTED


def test_get_entry_mqtt_env_empty_when_no_env_vars_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """mqtt_env is an empty dict, not omitted, when the Supervisor sets nothing (plain Docker)."""
    for var in ("MQTT_HOST", "MQTT_PORT", "MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_SSL"):
        monkeypatch.delenv(var, raising=False)
    server = _make_server(tmp_path)

    status, _, body = _dispatch_get(server, "/api/entry/nordpool")

    assert status == 200
    assert json.loads(body)["mqtt_env"] == {}


def test_get_entry_does_not_return_the_broker_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/entry/<id> must not contain the value of MQTT_PASSWORD.

    This server does not authenticate, by design. Returning the Supervisor's
    broker password in a plaintext response hands it to anything that can
    reach the port.
    """
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PASSWORD", "SuperSecret123")
    server = _make_server(tmp_path)

    status, headers, body = _dispatch_get(server, "/api/entry/nordpool")

    assert status == 200
    assert b"SuperSecret123" not in body


def test_post_save_never_writes_the_placeholder_to_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A form round-trip submits the placeholder back; it must not be persisted.

    The editor pre-fills the form from mqtt_env, so an untouched password field
    posts the placeholder. Writing that string into mimirheim.yaml would leave a
    config whose broker password is a literal sentinel. Stronger than
    test_post_save_strips_placeholder_and_merges_env_for_validation_only: this
    checks the "password" key is structurally absent, not just that the
    sentinel string doesn't appear anywhere in the file.
    """
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PASSWORD", "SuperSecret123")
    server = _make_server(tmp_path)
    submitted = {
        "mqtt": {"client_id": "mimir", "password": MQTT_ENV_REDACTED},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
    }

    status, headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"mimirheim": {"enabled": True, "config": submitted}}}
    )

    assert status == 200, json.loads(body)
    written = (tmp_path / "mimirheim.yaml").read_text()
    assert MQTT_ENV_REDACTED not in written
    loaded = yaml.safe_load(written)
    assert "password" not in loaded.get("mqtt", {})


def test_post_save_placeholder_submitted_when_env_absent_is_rejected_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The placeholder is stripped regardless of whether the env still supplies it.

    Otherwise a config saved inside the add-on and re-saved outside it would
    persist the sentinel as a real password.
    """
    monkeypatch.delenv("MQTT_PASSWORD", raising=False)
    monkeypatch.delenv("MQTT_HOST", raising=False)
    server = _make_server(tmp_path)
    submitted = {
        "mqtt": {"host": "broker", "client_id": "mimir", "password": MQTT_ENV_REDACTED},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
    }

    status, headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"mimirheim": {"enabled": True, "config": submitted}}}
    )

    assert status == 200, json.loads(body)
    written = (tmp_path / "mimirheim.yaml").read_text()
    assert MQTT_ENV_REDACTED not in written


# ---------------------------------------------------------------------------
# mimirheim.yaml-specific /api/save behaviour not covered by the generic
# nordpool-entry save tests below (comment preservation, atomic write, list-
# item removal).
# ---------------------------------------------------------------------------


def test_post_save_atomic_write(tmp_path: Path) -> None:
    """If the final rename fails, the original mimirheim.yaml is unchanged."""
    original = {
        "mqtt": {"host": "original", "client_id": "mimir"},
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0},
    }
    yaml_path = tmp_path / "mimirheim.yaml"
    yaml_path.write_text(yaml.dump(original))

    server = _make_server(tmp_path)

    new_config = {
        "mqtt": {"host": "new-host", "client_id": "mimir"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
    }

    with patch("os.replace", side_effect=OSError("disk full")):
        try:
            _dispatch_post(
                server,
                "/api/save",
                {"entries": {"mimirheim": {"enabled": True, "config": new_config}}},
            )
        except OSError:
            pass

    # The original must be intact.
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded["mqtt"]["host"] == "original"


def test_post_save_preserves_yaml_comments(tmp_path: Path) -> None:
    """POST /api/save preserves YAML comments in the existing file.

    This test verifies the round-trip comment preservation behavior:
    1. Create a config file with inline comments
    2. Edit a field value via the API
    3. Verify comments are still present in the written file
    """
    yaml_path = tmp_path / "mimirheim.yaml"

    # Write initial config with comments
    initial_yaml = """# Main grid connection
mqtt:
  host: localhost
  client_id: mimir

grid:
  import_limit_kw: 25.0  # Utility meter limit
  export_limit_kw: 10.0  # Contract restriction

batteries:
  home_battery:
    capacity_kwh: 10.0  # Tesla Powerwall 2
    # Charge efficiency degrades above 0.8 SOC
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
    discharge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95

objectives: {}
outputs:
  schedule: mimir/schedule
  current: mimir/current
  last_solve: mimir/last_solve
  availability: mimir/availability
"""
    yaml_path.write_text(initial_yaml)

    server = _make_server(tmp_path)

    # Edit via API: change only capacity_kwh
    edited_config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 10.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 12.0,  # Changed from 10.0
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            }
        },
        "objectives": {},
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/last_solve",
            "availability": "mimir/availability",
        },
    }

    status, headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"mimirheim": {"enabled": True, "config": edited_config}}}
    )
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True

    # Read back and verify comments are preserved
    result = yaml_path.read_text()
    assert "# Main grid connection" in result
    assert "# Utility meter limit" in result
    assert "# Contract restriction" in result
    assert "# Tesla Powerwall 2" in result
    assert "# Charge efficiency degrades above 0.8 SOC" in result

    # Verify the value was updated
    assert "capacity_kwh: 12.0" in result or "capacity_kwh: 12" in result


def test_post_save_removes_deleted_list_items(tmp_path: Path) -> None:
    """POST /api/save removes items deleted from lists.

    When the GUI sends a config with fewer items in a list (e.g., removed
    a battery, removed a charge segment), those items should be removed
    from the file.
    """
    yaml_path = tmp_path / "mimirheim.yaml"

    # Initial config with 2 batteries
    initial_yaml = """mqtt:
  host: localhost
  client_id: mimir

grid:
  import_limit_kw: 25.0
  export_limit_kw: 10.0

batteries:
  home_battery:  # Keep this one
    capacity_kwh: 10.0
    charge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
    discharge_segments:
      - power_max_kw: 5.0
        efficiency: 0.95
  garage_battery:  # Remove this one
    capacity_kwh: 5.0
    charge_segments:
      - power_max_kw: 2.0
        efficiency: 0.90
    discharge_segments:
      - power_max_kw: 2.0
        efficiency: 0.90

objectives: {}
outputs:
  schedule: mimir/schedule
  current: mimir/current
  last_solve: mimir/last_solve
  availability: mimir/availability
"""
    yaml_path.write_text(initial_yaml)

    server = _make_server(tmp_path)

    # Edit via API: remove garage_battery by not including it
    edited_config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 10.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 10.0,
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
            }
            # garage_battery intentionally omitted
        },
        "objectives": {},
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/last_solve",
            "availability": "mimir/availability",
        },
    }

    status, headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"mimirheim": {"enabled": True, "config": edited_config}}}
    )
    assert status == 200

    # Read back and verify garage_battery is gone
    result = yaml_path.read_text()
    assert "home_battery" in result
    assert "garage_battery" not in result


# ---------------------------------------------------------------------------
# The report and dump directories track reporter.yaml
# ---------------------------------------------------------------------------


def _write_reporter_yaml(config_dir: Path, output_dir: Path, dump_dir: Path) -> None:
    (config_dir / "reporter.yaml").write_text(
        yaml.safe_dump({"reporting": {"output_dir": str(output_dir), "dump_dir": str(dump_dir)}})
    )


def test_reports_index_follows_a_reporter_yaml_written_after_startup(
    tmp_path: Path,
) -> None:
    """Enabling the reporter through the editor must not need a restart.

    The directories were resolved once in __init__, so a reporter.yaml written
    or edited through this very editor had no effect until the process
    restarted, with nothing in the UI to say why.
    """
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "index.html").write_text("<html>reports</html>")
    server = _make_server(tmp_path)
    # Before: no reporter.yaml at all.
    status, _headers, _body = _dispatch_get(server, "/reports")
    assert status == 404

    _write_reporter_yaml(tmp_path, reports, tmp_path / "dumps")

    status, _headers, body = _dispatch_get(server, "/reports")
    assert status == 200
    assert b"reports" in body


def test_reports_index_follows_a_changed_output_dir(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for d in (first, second):
        d.mkdir()
    (first / "index.html").write_text("<html>first</html>")
    (second / "index.html").write_text("<html>second</html>")
    _write_reporter_yaml(tmp_path, first, tmp_path / "dumps")
    server = _make_server(tmp_path)
    assert b"first" in _dispatch_get(server, "/reports")[2]

    _write_reporter_yaml(tmp_path, second, tmp_path / "dumps")

    assert b"second" in _dispatch_get(server, "/reports")[2]


def test_dump_file_follows_a_changed_dump_dir(tmp_path: Path) -> None:
    dumps = tmp_path / "late-dumps"
    dumps.mkdir()
    (dumps / "2026-01-01T00-00-00Z_input.json").write_text('{"ok": true}')
    server = _make_server(tmp_path)
    assert _dispatch_get(server, "/reports/dumps/2026-01-01T00-00-00Z_input.json")[0] == 404

    _write_reporter_yaml(tmp_path, tmp_path / "reports", dumps)

    status, _headers, body = _dispatch_get(server, "/reports/dumps/2026-01-01T00-00-00Z_input.json")
    assert status == 200
    assert b'"ok"' in body


# NOTE: the deleted GET /api/config used to report a "reports_available"
# boolean (self._reports_dir is not None and (self._reports_dir /
# "index.html").exists()), so the frontend could decide whether to show a
# reports tab. No current endpoint (/api/registry, /api/entry/<id>) exposes
# an equivalent signal, and SPEC.md's §12 API table has no field for it
# either. This is a genuine, unresolved gap -- flagged in this task's final
# report rather than resolved here, since it's product surface (a new
# /api/registry field, a dedicated endpoint, or deferred to plan 69), not a
# mechanical test port. _reports_dir/_serve_reports_index themselves are
# untouched and still work for actually serving the reports UI.


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a 0000 file")
def test_unreadable_reporter_yaml_degrades_to_not_configured(tmp_path: Path) -> None:
    """An existing but unreadable reporter.yaml must not raise out of a handler."""
    reporter_yaml = tmp_path / "reporter.yaml"
    reporter_yaml.write_text("reporting:\n  output_dir: /tmp/x\n")
    reporter_yaml.chmod(0o000)
    server = _make_server(tmp_path)
    try:
        status, _headers, body = _dispatch_get(server, "/reports")
    finally:
        reporter_yaml.chmod(0o644)

    assert status == 404
    assert b"not configured" in body


def test_malformed_reporter_yaml_degrades_to_not_configured(tmp_path: Path) -> None:
    (tmp_path / "reporter.yaml").write_text("reporting: [unclosed\n")
    server = _make_server(tmp_path)

    status, _headers, body = _dispatch_get(server, "/reports")

    assert status == 404
    assert b"not configured" in body


# ---------------------------------------------------------------------------
# The env mapping is shared with helper_common
# ---------------------------------------------------------------------------


def test_mqtt_env_matches_helper_common(monkeypatch: pytest.MonkeyPatch) -> None:
    """One definition of the env-to-field mapping, not two that can drift."""
    from helper_common.config import mqtt_env_overrides

    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "8883")
    monkeypatch.setenv("MQTT_USERNAME", "user1")
    monkeypatch.setenv("MQTT_SSL", "true")

    assert ConfigEditorServer._mqtt_env() == mqtt_env_overrides()


def test_bad_mqtt_port_does_not_break_the_editor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A helper exits on an invalid MQTT_PORT; the editor must stay usable.

    It is the tool an operator reaches for to fix configuration, and it cannot
    repair an environment variable. Deliberate behaviour change: it now reports
    no Supervisor-supplied MQTT settings at all rather than silently dropping
    just the port and claiming the rest are Supervisor-controlled.

    No current endpoint exposes _mqtt_env()'s output directly (see the note
    above test_get_entry_does_not_return_the_broker_password), so this is
    exercised as a direct call, same pattern as test_mqtt_env_matches_helper_common.
    """
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "not-a-number")

    with caplog.at_level(logging.WARNING):
        result = ConfigEditorServer._mqtt_env()

    assert result == {}
    assert "MQTT_PORT" in caplog.text


def test_out_of_range_mqtt_port_is_also_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "99999")

    assert ConfigEditorServer._mqtt_env() == {}


def test_post_save_still_succeeds_when_mqtt_port_env_is_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_redact_and_merge_entries calls _mqtt_env() on every save/preview request; a
    broken MQTT_PORT env var must not take the whole request down with it.

    GET /api/entry/<id> never touches the MQTT environment (SPEC.md §12), so
    unlike the two tests above, this genuinely needs an HTTP-level guard --
    /api/save is the one HTTP entrypoint that actually calls _mqtt_env().
    """
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "not-a-number")
    server = _make_server(tmp_path)

    status, _headers, body = _dispatch_post(
        server,
        "/api/save",
        {"entries": {"nordpool": {"enabled": True, "config": _MINIMAL_NORDPOOL}}},
    )

    assert status == 200, json.loads(body)
    assert (tmp_path / "nordpool.yaml").exists()


# ---------------------------------------------------------------------------
# GET /api/registry
# ---------------------------------------------------------------------------

_ALL_ENTRY_IDS = {
    "mimirheim",
    "nordpool",
    "zonneplan",
    "pv-fetcher",
    "pv-ml-learner",
    "baseload-static",
    "baseload-ha",
    "baseload-ha-db",
    "reporter",
    "scheduler",
}


def test_get_registry_lists_every_bundled_entry(tmp_path: Path) -> None:
    """GET /api/registry lists every bundled entry, each with its x-mimirheim fields."""
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/registry")
    assert status == 200
    data = json.loads(body)
    assert set(data["entries"]) == _ALL_ENTRY_IDS
    assert data["problems"] == []
    nordpool = data["entries"]["nordpool"]
    assert nordpool["x-mimirheim"]["file"] == "nordpool.yaml"
    assert nordpool["x-mimirheim"]["category"] == "prices"
    assert nordpool["enabled"] is False


def test_get_registry_reports_enabled_from_file_presence(tmp_path: Path) -> None:
    """An entry is 'enabled' exactly when its YAML file exists on disk."""
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(_MINIMAL_NORDPOOL))
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/registry")
    data = json.loads(body)
    assert data["entries"]["nordpool"]["enabled"] is True
    assert data["entries"]["reporter"]["enabled"] is False


# ---------------------------------------------------------------------------
# GET /api/entry/<id>
# ---------------------------------------------------------------------------


def test_get_entry_unknown_id_returns_404(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _dispatch_get(server, "/api/entry/does-not-exist")
    assert status == 404


def test_get_entry_mimirheim_schema_title_matches_the_model(tmp_path: Path) -> None:
    """mimirheim's own entry schema has title == 'MimirheimConfig'."""
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/entry/mimirheim")
    assert status == 200
    data = json.loads(body)
    assert data["schema"].get("title") == "MimirheimConfig"


def test_get_entry_mimirheim_malformed_yaml_degrades_to_defaults(tmp_path: Path) -> None:
    """A malformed mimirheim.yaml must not raise out of the request handler.

    _read_yaml_file degrades any unreadable/malformed config file to {} --
    this pins that mimirheim.yaml's own entry goes through the same shared
    helper as every other entry (and as reporter.yaml, see
    test_malformed_reporter_yaml_degrades_to_not_configured).
    """
    (tmp_path / "mimirheim.yaml").write_text("mqtt: [unclosed\n")
    server = _make_server(tmp_path)

    status, _headers, body = _dispatch_get(server, "/api/entry/mimirheim")

    assert status == 200
    data = json.loads(body)
    assert data["enabled"] is True  # the file exists, even though it's unreadable
    assert data["value"] == {}


def test_get_entry_mimirheim_has_no_context_key(tmp_path: Path) -> None:
    """mimirheim.yaml's own entry gets no injected context subtree (SPEC.md §5)."""
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/entry/mimirheim")
    assert status == 200
    data = json.loads(body)
    assert "context" not in data["schema"].get("properties", {})
    assert "context" not in data["value"]


def test_get_entry_nordpool_schema_has_injected_context(tmp_path: Path) -> None:
    """A non-mimirheim entry's schema gets the read-only context subtree (SPEC.md §5)."""
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/entry/nordpool")
    assert status == 200
    data = json.loads(body)
    context_schema = data["schema"]["properties"]["context"]
    assert context_schema["readOnly"] is True
    assert set(context_schema["properties"]) == {"mqtt_topic_prefix", "pv_arrays", "static_loads"}


def test_get_entry_context_value_reflects_mimirheim_yaml(tmp_path: Path) -> None:
    """The context snapshot in an entry's value is built from mimirheim.yaml on disk."""
    mimirheim_config = {
        "mqtt": {"host": "localhost", "client_id": "mimir", "topic_prefix": "myhome"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "pv_arrays": {"roof": {"max_power_kw": 8.0}},
    }
    (tmp_path / "mimirheim.yaml").write_text(yaml.dump(mimirheim_config))
    server = _make_server(tmp_path)

    status, _headers, body = _dispatch_get(server, "/api/entry/nordpool")

    assert status == 200
    context = json.loads(body)["value"]["context"]
    assert context["mqtt_topic_prefix"] == "myhome"
    assert "roof" in context["pv_arrays"]
    assert context["static_loads"] == {}


def test_get_entry_disabled_returns_model_defaults(tmp_path: Path) -> None:
    """A not-yet-enabled entry's value comes from its Pydantic model's own defaults."""
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/entry/nordpool")
    assert status == 200
    data = json.loads(body)
    assert data["enabled"] is False
    # mimir_topic_prefix defaults to "mimir"; mqtt/trigger_topic have no
    # default and so are absent, not present-with-a-placeholder-value.
    assert data["value"]["mimir_topic_prefix"] == "mimir"
    assert "mqtt" not in data["value"]


def test_get_entry_enabled_returns_current_file_content(tmp_path: Path) -> None:
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(_MINIMAL_NORDPOOL))
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_get(server, "/api/entry/nordpool")
    data = json.loads(body)
    assert data["enabled"] is True
    assert data["value"]["nordpool"]["area"] == "NL"


def test_entry_id_with_dotdot_is_rejected_before_routing(tmp_path: Path) -> None:
    """The generic '..' guard covers the new /api/entry/ namespace too."""
    server = _make_server(tmp_path)
    status, _headers, _body = _dispatch_get(server, "/api/entry/../../etc/passwd")
    assert status == 400


# ---------------------------------------------------------------------------
# POST /api/save
# ---------------------------------------------------------------------------


def test_post_save_writes_a_single_entry(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_post(
        server,
        "/api/save",
        {"entries": {"nordpool": {"enabled": True, "config": _MINIMAL_NORDPOOL}}},
    )
    assert status == 200
    assert json.loads(body)["ok"] is True
    assert (tmp_path / "nordpool.yaml").exists()


def test_post_save_rejects_whole_batch_when_one_entry_is_invalid(tmp_path: Path) -> None:
    """validate-all-then-write-all: an invalid entry blocks writing any dirty file."""
    server = _make_server(tmp_path)
    bad_nordpool = {"mqtt": {"host": "localhost", "client_id": "np"}, "trigger_topic": "t"}
    status, _headers, body = _dispatch_post(
        server,
        "/api/save",
        {
            "entries": {
                "reporter": {
                    "enabled": True,
                    "config": {"mqtt": {"host": "localhost", "client_id": "r"}},
                },
                "nordpool": {"enabled": True, "config": bad_nordpool},
            }
        },
    )
    assert status == 422
    data = json.loads(body)
    assert data["ok"] is False
    assert "nordpool" in data["errors"]
    assert not (tmp_path / "reporter.yaml").exists()
    assert not (tmp_path / "nordpool.yaml").exists()


def test_post_save_unknown_entry_id_returns_404(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _dispatch_post(
        server, "/api/save", {"entries": {"does-not-exist": {"enabled": False}}}
    )
    assert status == 404


def test_post_save_malformed_body_returns_400(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = server.handle_request("POST", "/api/save", body=b"not json")
    assert status == 400


def test_post_save_missing_entries_key_returns_400(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, _body = _dispatch_post(server, "/api/save", {"not_entries": {}})
    assert status == 400


def test_post_save_exclusive_group_deletes_siblings_generically(tmp_path: Path) -> None:
    """Saving baseload-static.yaml deletes baseload-ha.yaml/baseload-ha-db.yaml (SPEC.md §8)."""
    (tmp_path / "baseload-ha.yaml").write_text(yaml.dump({"some": "value"}))
    (tmp_path / "baseload-ha-db.yaml").write_text(yaml.dump({"some": "value"}))
    server = _make_server(tmp_path)
    static_config = {
        "mqtt": {"host": "localhost", "client_id": "bl"},
        "trigger_topic": "t",
        "baseload": {"profile_kw": [0.5] * 24},
    }
    status, _headers, body = _dispatch_post(
        server,
        "/api/save",
        {"entries": {"baseload-static": {"enabled": True, "config": static_config}}},
    )
    assert status == 200, json.loads(body)
    assert not (tmp_path / "baseload-ha.yaml").exists()
    assert not (tmp_path / "baseload-ha-db.yaml").exists()


def test_post_save_disable_deletes_the_file(tmp_path: Path) -> None:
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(_MINIMAL_NORDPOOL))
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"nordpool": {"enabled": False}}}
    )
    assert status == 200, json.loads(body)
    assert not (tmp_path / "nordpool.yaml").exists()


def test_post_save_strips_placeholder_and_merges_env_for_validation_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MQTT redaction (Decision 8) carries over to /api/save unchanged."""
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PASSWORD", "SuperSecret123")
    server = _make_server(tmp_path)
    submitted = {
        "mqtt": {"client_id": "nordpool", "password": MQTT_ENV_REDACTED},
        "trigger_topic": "mimir/input/nordpool/trigger",
        "nordpool": {"area": "NL"},
    }

    status, _headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"nordpool": {"enabled": True, "config": submitted}}}
    )

    assert status == 200, json.loads(body)
    written = (tmp_path / "nordpool.yaml").read_text()
    assert MQTT_ENV_REDACTED not in written
    assert "SuperSecret123" not in written


def test_post_save_keeps_a_password_the_user_actually_typed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    server = _make_server(tmp_path)
    submitted = {
        "mqtt": {"client_id": "nordpool", "password": "my-own-password"},
        "trigger_topic": "mimir/input/nordpool/trigger",
        "nordpool": {"area": "NL"},
    }

    status, _headers, body = _dispatch_post(
        server, "/api/save", {"entries": {"nordpool": {"enabled": True, "config": submitted}}}
    )

    assert status == 200, json.loads(body)
    loaded = yaml.safe_load((tmp_path / "nordpool.yaml").read_text())
    assert loaded["mqtt"]["password"] == "my-own-password"


# ---------------------------------------------------------------------------
# POST /api/preview
# ---------------------------------------------------------------------------


def test_post_preview_never_writes_to_disk(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_post(
        server,
        "/api/preview",
        {"entries": {"nordpool": {"enabled": True, "config": _MINIMAL_NORDPOOL}}},
    )
    assert status == 200, json.loads(body)
    data = json.loads(body)
    assert data["ok"] is True
    assert not (tmp_path / "nordpool.yaml").exists()
    assert "nordpool.yaml" in data["diffs"]
    assert "area: NL" in data["diffs"]["nordpool.yaml"]


def test_post_preview_shares_validation_with_save(tmp_path: Path) -> None:
    """An invalid entry is rejected by /api/preview the same way /api/save rejects it."""
    server = _make_server(tmp_path)
    bad_nordpool = {"mqtt": {"host": "localhost", "client_id": "np"}, "trigger_topic": "t"}
    status, _headers, body = _dispatch_post(
        server, "/api/preview", {"entries": {"nordpool": {"enabled": True, "config": bad_nordpool}}}
    )
    assert status == 422
    data = json.loads(body)
    assert data["ok"] is False
    assert "nordpool" in data["errors"]
    assert not (tmp_path / "nordpool.yaml").exists()


def test_post_preview_deletion_diff_does_not_delete(tmp_path: Path) -> None:
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(_MINIMAL_NORDPOOL))
    server = _make_server(tmp_path)
    status, _headers, body = _dispatch_post(
        server, "/api/preview", {"entries": {"nordpool": {"enabled": False}}}
    )
    assert status == 200, json.loads(body)
    data = json.loads(body)
    assert (tmp_path / "nordpool.yaml").exists()
    assert "nordpool.yaml" in data["diffs"]


def test_post_preview_shows_a_key_deleted_by_omission(tmp_path: Path) -> None:
    """A save deletes keys absent from the submitted config -- the preview diff must show it.

    This is Decision 3's motivating scenario (plan 70): a user removing a
    field's value in the form is not obviously "delete this YAML key
    entirely" rather than "revert to a default" -- the preview diff is what
    makes that visible before the user confirms.
    """
    with_output_topic = {**_MINIMAL_NORDPOOL, "output_topic": "mimir/input/prices"}
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(with_output_topic))
    server = _make_server(tmp_path)

    status, _headers, body = _dispatch_post(
        server,
        "/api/preview",
        {"entries": {"nordpool": {"enabled": True, "config": _MINIMAL_NORDPOOL}}},
    )

    assert status == 200, json.loads(body)
    data = json.loads(body)
    diff = data["diffs"]["nordpool.yaml"]
    assert "-output_topic: mimir/input/prices" in diff
    loaded = yaml.safe_load((tmp_path / "nordpool.yaml").read_text())
    assert loaded["output_topic"] == "mimir/input/prices"  # untouched -- preview never writes


# ---------------------------------------------------------------------------
# POST /api/reload
# ---------------------------------------------------------------------------


def test_post_reload_returns_the_same_shape_as_get_registry(tmp_path: Path) -> None:
    server = _make_server(tmp_path)
    status, _headers, body = server.handle_request("POST", "/api/reload", body=b"")
    assert status == 200
    data = json.loads(body)
    assert set(data["entries"]) == _ALL_ENTRY_IDS
    assert data["problems"] == []


def test_post_reload_picks_up_a_new_drop_in_schema(tmp_path: Path) -> None:
    """POST /api/reload re-runs discovery without restarting the process."""
    server = _make_server(tmp_path)
    status, _headers, body = server.handle_request("GET", "/api/registry", body=b"")
    assert "widget" not in json.loads(body)["entries"]

    schemas_dir = tmp_path / "schemas"
    schemas_dir.mkdir()
    (schemas_dir / "widget.schema.json").write_text(
        json.dumps(
            {
                "type": "object",
                "additionalProperties": False,
                "properties": {"enabled": {"type": "boolean"}},
                "x-mimirheim": {
                    "file": "widget.yaml",
                    "category": "other",
                    "python_package": "widget",
                },
            }
        )
    )

    status, _headers, body = server.handle_request("POST", "/api/reload", body=b"")
    assert status == 200
    assert "widget" in json.loads(body)["entries"]


# The deprecated aliases (GET /api/schema, GET/POST /api/config,
# GET /api/helper-configs, GET /api/helper-schemas,
# POST /api/helper-config/<filename>) have been deleted, not kept as thin
# adapters (plan 68 Decision 9, corrected during implementation). The three
# tests that used to check the aliases delegated to the registry rather than
# reimplementing it are gone along with them -- the registry endpoints
# themselves (GET /api/registry, GET /api/entry/<id>, POST /api/save) are
# already covered above.
