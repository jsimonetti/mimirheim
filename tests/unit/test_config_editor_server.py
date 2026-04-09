"""Unit tests for the config editor HTTP server.

Tests instantiate ConfigEditorServer directly using a temp directory as
config_dir. No subprocess is spawned; all requests are dispatched in-process
via the handler's internal dispatch logic.

What these tests do not cover:
- Full HTTP round-trips with a live socket (see test_config_editor_crud_generic.py)
- Frontend JavaScript rendering
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from config_editor.server import ConfigEditorServer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_server(tmp_path: Path) -> ConfigEditorServer:
    """Return a ConfigEditorServer wired to a temp config directory."""
    return ConfigEditorServer(config_dir=tmp_path, port=0)


def _dispatch_get(server: ConfigEditorServer, path: str) -> tuple[int, dict[str, str], bytes]:
    """Simulate a GET request and return (status_code, headers, body)."""
    return server.handle_request("GET", path, body=b"")


def _dispatch_post(server: ConfigEditorServer, path: str, body: Any) -> tuple[int, dict[str, str], bytes]:
    """Simulate a POST request with a JSON body."""
    raw = json.dumps(body).encode()
    return server.handle_request("POST", path, body=raw)


# ---------------------------------------------------------------------------
# GET /api/schema
# ---------------------------------------------------------------------------

def test_get_schema_returns_mimirheim_schema(tmp_path: Path) -> None:
    """GET /api/schema returns a JSON schema with title == 'MimirheimConfig'."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/schema")
    assert status == 200
    data = json.loads(body)
    assert data.get("title") == "MimirheimConfig"


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------

def test_get_config_when_file_absent(tmp_path: Path) -> None:
    """GET /api/config with no mimirheim.yaml returns exists=false and empty config."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/config")
    assert status == 200
    data = json.loads(body)
    assert data["exists"] is False
    assert data["config"] == {}


def test_get_config_returns_parsed_yaml(tmp_path: Path) -> None:
    """GET /api/config with a YAML file present returns the parsed dict."""
    yaml_content = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
    }
    (tmp_path / "mimirheim.yaml").write_text(yaml.dump(yaml_content))
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/config")
    assert status == 200
    data = json.loads(body)
    assert data["exists"] is True
    assert data["config"]["grid"]["import_limit_kw"] == 25.0


# ---------------------------------------------------------------------------
# POST /api/config
# ---------------------------------------------------------------------------

_MINIMAL_VALID_CONFIG = {
    "mqtt": {"host": "localhost", "client_id": "mimir"},
    "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
}


def test_post_config_valid_writes_file(tmp_path: Path) -> None:
    """POST a valid config dict returns HTTP 200 and writes mimirheim.yaml."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_post(server, "/api/config", _MINIMAL_VALID_CONFIG)
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True
    yaml_path = tmp_path / "mimirheim.yaml"
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded["grid"]["import_limit_kw"] == 25.0


def test_post_config_invalid_returns_422(tmp_path: Path) -> None:
    """POST a config with invalid values returns HTTP 422 with an 'errors' key."""
    server = _make_server(tmp_path)
    bad_config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": -1.0, "export_limit_kw": 25.0},
    }
    status, headers, body = _dispatch_post(server, "/api/config", bad_config)
    assert status == 422
    data = json.loads(body)
    assert data["ok"] is False
    assert "errors" in data


def test_post_config_atomic_write(tmp_path: Path) -> None:
    """If the final rename fails, the original mimirheim.yaml is unchanged."""
    original = {"mqtt": {"host": "original", "client_id": "mimir"}, "grid": {"import_limit_kw": 10.0, "export_limit_kw": 10.0}}
    yaml_path = tmp_path / "mimirheim.yaml"
    yaml_path.write_text(yaml.dump(original))

    server = _make_server(tmp_path)

    new_config = {
        "mqtt": {"host": "new-host", "client_id": "mimir"},
        "grid": {"import_limit_kw": 20.0, "export_limit_kw": 20.0},
    }

    with patch("os.replace", side_effect=OSError("disk full")):
        try:
            _dispatch_post(server, "/api/config", new_config)
        except OSError:
            pass

    # The original must be intact.
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded["mqtt"]["host"] == "original"


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

def test_static_path_traversal_returns_403(tmp_path: Path) -> None:
    """GET /static/../config.py must return 403, not 404 or 200."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/static/../config.py")
    assert status == 403


# ---------------------------------------------------------------------------
# GET /api/helper-configs
# ---------------------------------------------------------------------------

def test_get_helper_configs_returns_all_known_helpers(tmp_path: Path) -> None:
    """GET /api/helper-configs with empty config dir returns all 8 helpers as disabled."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/helper-configs")
    assert status == 200
    data = json.loads(body)
    expected_files = {
        "nordpool.yaml",
        "pv-fetcher.yaml",
        "pv-ml-learner.yaml",
        "baseload-static.yaml",
        "baseload-ha.yaml",
        "baseload-ha-db.yaml",
        "reporter.yaml",
        "scheduler.yaml",
    }
    assert set(data.keys()) == expected_files
    for fname, state in data.items():
        assert state["enabled"] is False, f"{fname} should be disabled when file is absent"
        assert "config" in state


def test_get_helper_configs_enabled_when_file_present(tmp_path: Path) -> None:
    """If nordpool.yaml exists in config_dir, it is reported as enabled."""
    stub = {"mqtt": {"host": "localhost", "client_id": "np"}, "trigger_topic": "t",
            "nordpool": {"area": "NL"}}
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(stub))
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/helper-configs")
    assert status == 200
    data = json.loads(body)
    assert data["nordpool.yaml"]["enabled"] is True
    assert data["nordpool.yaml"]["config"]["nordpool"]["area"] == "NL"


# ---------------------------------------------------------------------------
# GET /api/helper-schemas
# ---------------------------------------------------------------------------

def test_get_helper_schemas_returns_all_helpers(tmp_path: Path) -> None:
    """GET /api/helper-schemas returns schema objects for all 8 known helpers."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/helper-schemas")
    assert status == 200
    data = json.loads(body)
    expected_files = {
        "nordpool.yaml",
        "pv-fetcher.yaml",
        "pv-ml-learner.yaml",
        "baseload-static.yaml",
        "baseload-ha.yaml",
        "baseload-ha-db.yaml",
        "reporter.yaml",
        "scheduler.yaml",
    }
    assert set(data.keys()) == expected_files
    for fname, schema in data.items():
        assert "title" in schema, f"{fname} schema is missing 'title'"


# ---------------------------------------------------------------------------
# POST /api/helper-config/<filename>
# ---------------------------------------------------------------------------

_MINIMAL_NORDPOOL = {
    "mqtt": {"host": "localhost", "client_id": "np"},
    "trigger_topic": "mimirheim/trigger",
    "nordpool": {"area": "NL"},
}


def test_post_helper_config_valid_writes_file(tmp_path: Path) -> None:
    """POST {"enabled": true, "config": {...}} writes the helper YAML file."""
    server = _make_server(tmp_path)
    body = {"enabled": True, "config": _MINIMAL_NORDPOOL}
    status, headers, resp = _dispatch_post(server, "/api/helper-config/nordpool.yaml", body)
    assert status == 200
    data = json.loads(resp)
    assert data["ok"] is True
    yaml_path = tmp_path / "nordpool.yaml"
    assert yaml_path.exists()
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded["nordpool"]["area"] == "NL"


def test_post_helper_config_disable_deletes_file(tmp_path: Path) -> None:
    """POST {"enabled": false} deletes the helper config file."""
    (tmp_path / "nordpool.yaml").write_text(yaml.dump(_MINIMAL_NORDPOOL))
    server = _make_server(tmp_path)
    status, headers, resp = _dispatch_post(server, "/api/helper-config/nordpool.yaml", {"enabled": False})
    assert status == 200
    data = json.loads(resp)
    assert data["ok"] is True
    assert not (tmp_path / "nordpool.yaml").exists()


def test_post_helper_config_unknown_filename_returns_400(tmp_path: Path) -> None:
    """POST to a filename not in the allowlist returns 400."""
    server = _make_server(tmp_path)
    status, headers, resp = _dispatch_post(
        server, "/api/helper-config/../../etc/passwd", {"enabled": False}
    )
    assert status == 400


def test_post_helper_config_invalid_returns_422(tmp_path: Path) -> None:
    """POST an invalid Nordpool config body returns 422 with errors."""
    server = _make_server(tmp_path)
    bad = {"enabled": True, "config": {"mqtt": {"host": "localhost", "client_id": "np"},
                                        "trigger_topic": "t"}}  # missing nordpool.area
    status, headers, resp = _dispatch_post(server, "/api/helper-config/nordpool.yaml", bad)
    assert status == 422
    data = json.loads(resp)
    assert data["ok"] is False
    assert "errors" in data


def test_post_baseload_enable_disables_other_variants(tmp_path: Path) -> None:
    """Enabling baseload-static.yaml deletes any existing baseload-ha.yaml or baseload-ha-db.yaml."""
    # Pre-create the competing variant.
    ha_stub = {
        "mqtt": {"host": "localhost", "client_id": "bl"},
        "trigger_topic": "t",
        "homeassistant": {
            "url": "http://ha:8123",
            "token": "tok",
            "sum_entities": [{"entity_id": "sensor.power", "unit": "W"}],
        },
    }
    (tmp_path / "baseload-ha.yaml").write_text(yaml.dump(ha_stub))

    server = _make_server(tmp_path)
    static_config = {
        "mqtt": {"host": "localhost", "client_id": "bl"},
        "trigger_topic": "t",
        "baseload": {"profile_kw": [0.5] * 24},
    }
    status, headers, resp = _dispatch_post(
        server, "/api/helper-config/baseload-static.yaml", {"enabled": True, "config": static_config}
    )
    assert status == 200
    assert not (tmp_path / "baseload-ha.yaml").exists()
