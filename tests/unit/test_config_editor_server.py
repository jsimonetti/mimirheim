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


def test_post_config_preserves_yaml_comments(tmp_path: Path) -> None:
    """POST /api/config preserves YAML comments in the existing file.
    
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
    
    status, headers, body = _dispatch_post(server, "/api/config", edited_config)
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


def test_post_config_removes_deleted_list_items(tmp_path: Path) -> None:
    """POST /api/config removes items deleted from lists.
    
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
    
    status, headers, body = _dispatch_post(server, "/api/config", edited_config)
    assert status == 200
    
    # Read back and verify garage_battery is gone
    result = yaml_path.read_text()
    assert "home_battery" in result
    assert "garage_battery" not in result


# ---------------------------------------------------------------------------
# Static file serving
# ---------------------------------------------------------------------------

def test_static_path_traversal_returns_400(tmp_path: Path) -> None:
    """GET /static/../config.py must return 400 (rejected before routing)."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/static/../config.py")
    assert status == 400


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


# ---------------------------------------------------------------------------
# MQTT env var handling
# ---------------------------------------------------------------------------

def test_get_config_includes_empty_mqtt_env_when_no_env_vars(tmp_path: Path) -> None:
    """GET /api/config returns mqtt_env: {} when no MQTT env vars are set."""
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/config")
    data = json.loads(body)
    assert "mqtt_env" in data
    assert data["mqtt_env"] == {}


def test_get_config_includes_mqtt_env_from_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /api/config returns mqtt_env populated from MQTT_* env vars."""
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1883")
    monkeypatch.setenv("MQTT_USERNAME", "user1")
    monkeypatch.setenv("MQTT_PASSWORD", "secret")
    monkeypatch.setenv("MQTT_SSL", "false")
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/config")
    data = json.loads(body)
    assert data["mqtt_env"]["host"] == "core-mosquitto"
    assert data["mqtt_env"]["port"] == 1883
    assert data["mqtt_env"]["username"] == "user1"
    assert data["mqtt_env"]["tls"] is False


def test_get_config_mqtt_env_tls_true_when_ssl_env_is_true(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MQTT_SSL=true maps to mqtt_env.tls=True."""
    monkeypatch.setenv("MQTT_HOST", "broker")
    monkeypatch.setenv("MQTT_SSL", "true")
    server = _make_server(tmp_path)
    status, headers, body = _dispatch_get(server, "/api/config")
    data = json.loads(body)
    assert data["mqtt_env"]["tls"] is True


def test_post_config_valid_when_mqtt_provided_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/config succeeds when mqtt fields are absent but supplied by env vars.

    When running as a HA add-on the user's YAML typically has no mqtt: section;
    the broker settings come from env. Pydantic validation should pass because
    the server merges env into the validation copy.
    """
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1883")
    server = _make_server(tmp_path)
    # Config with no mqtt section — valid only because env supplies host.
    config_no_mqtt = {
        "mqtt": {"client_id": "mimir"},  # client_id is not env-supplied; user must set it
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
    }
    status, headers, body = _dispatch_post(server, "/api/config", config_no_mqtt)
    data = json.loads(body)
    assert status == 200, f"Expected 200 but got {status}: {data}"
    assert data["ok"] is True
    # The written YAML must not contain mqtt.host (env-supplied, not saved).
    yaml_path = tmp_path / "mimirheim.yaml"
    loaded = yaml.safe_load(yaml_path.read_text())
    assert loaded.get("mqtt", {}).get("host") is None


def test_post_config_env_does_not_override_explicit_mqtt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both env and submitted config contain mqtt.host, the submitted value is saved."""
    monkeypatch.setenv("MQTT_HOST", "env-broker")
    server = _make_server(tmp_path)
    config_with_mqtt = {
        "mqtt": {"host": "my-broker", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
    }
    status, headers, body = _dispatch_post(server, "/api/config", config_with_mqtt)
    assert status == 200
    loaded = yaml.safe_load((tmp_path / "mimirheim.yaml").read_text())
    # User's explicit value must be written, not the env value.
    assert loaded["mqtt"]["host"] == "my-broker"


def test_post_helper_config_valid_when_mqtt_provided_by_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/helper-config/nordpool.yaml succeeds when mqtt is absent but provided by env."""
    monkeypatch.setenv("MQTT_HOST", "core-mosquitto")
    monkeypatch.setenv("MQTT_PORT", "1883")
    server = _make_server(tmp_path)
    # Nordpool config without mqtt.host — valid only because env supplies it.
    config_no_mqtt = {
        "mqtt": {"client_id": "nordpool"},  # client_id is not env-supplied
        "trigger_topic": "mimirheim/trigger",
        "nordpool": {"area": "NL"},
    }
    status, headers, body = _dispatch_post(
        server, "/api/helper-config/nordpool.yaml", {"enabled": True, "config": config_no_mqtt}
    )
    data = json.loads(body)
    assert status == 200, f"Expected 200 but got {status}: {data}"
    assert data["ok"] is True
    loaded = yaml.safe_load((tmp_path / "nordpool.yaml").read_text())
    assert loaded.get("mqtt", {}).get("host") is None


# ---------------------------------------------------------------------------
# ui_source metadata in helper schemas (Phase 3 acceptance tests)
# ---------------------------------------------------------------------------

def _get_helper_schemas(tmp_path: Path) -> dict[str, Any]:
    """Return the parsed helper-schemas response dict."""
    server = _make_server(tmp_path)
    status, _, body = _dispatch_get(server, "/api/helper-schemas")
    assert status == 200
    return json.loads(body)


def test_baseload_static_mimir_static_load_name_has_ui_source(tmp_path: Path) -> None:
    """baseload-static schema exposes ui_source='static_loads' on mimir_static_load_name."""
    schemas = _get_helper_schemas(tmp_path)
    props = schemas["baseload-static.yaml"]["properties"]
    field = props["mimir_static_load_name"]
    assert field.get("ui_source") == "static_loads"


def test_baseload_ha_mimir_static_load_name_has_ui_source(tmp_path: Path) -> None:
    """baseload-ha schema exposes ui_source='static_loads' on mimir_static_load_name."""
    schemas = _get_helper_schemas(tmp_path)
    props = schemas["baseload-ha.yaml"]["properties"]
    field = props["mimir_static_load_name"]
    assert field.get("ui_source") == "static_loads"


def test_baseload_ha_db_mimir_static_load_name_has_ui_source(tmp_path: Path) -> None:
    """baseload-ha-db schema exposes ui_source='static_loads' on mimir_static_load_name."""
    schemas = _get_helper_schemas(tmp_path)
    props = schemas["baseload-ha-db.yaml"]["properties"]
    field = props["mimir_static_load_name"]
    assert field.get("ui_source") == "static_loads"


def test_pv_fetcher_array_output_topic_has_ui_source(tmp_path: Path) -> None:
    """pv-fetcher ArrayConfig.output_topic exposes ui_source='pv_arrays'."""
    schemas = _get_helper_schemas(tmp_path)
    array_config = schemas["pv-fetcher.yaml"]["$defs"]["ArrayConfig"]
    field = array_config["properties"]["output_topic"]
    assert field.get("ui_source") == "pv_arrays"


def test_pv_ml_learner_array_output_topic_has_ui_source(tmp_path: Path) -> None:
    """pv-ml-learner ArrayConfig.output_topic exposes ui_source='pv_arrays'."""
    schemas = _get_helper_schemas(tmp_path)
    array_config = schemas["pv-ml-learner.yaml"]["$defs"]["ArrayConfig"]
    field = array_config["properties"]["output_topic"]
    assert field.get("ui_source") == "pv_arrays"
