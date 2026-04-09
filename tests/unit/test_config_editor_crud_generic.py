"""Integration-style tests for the config editor CRUD API.

These tests start a real ConfigEditorServer on a random local port using a
background thread, then hit the live endpoints with urllib.request. They prove
that the generic DeviceListEditor CRUD pattern works end-to-end through actual
HTTP without any device-specific server code.

What these tests do not cover:
- JavaScript rendering of the CRUD UI
- Browser-side interaction
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

from config_editor.server import ConfigEditorServer


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def live_server(tmp_path: Path):
    """Start a real ConfigEditorServer on a random OS-assigned port.

    Yields the base URL (e.g. 'http://127.0.0.1:54321').
    Server is shut down after the test.
    """
    server = ConfigEditorServer(config_dir=tmp_path, port=0)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def _post_json(url: str, data: dict) -> tuple[int, dict]:
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _get_json(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url) as resp:
        return resp.status, json.loads(resp.read())


# ---------------------------------------------------------------------------
# Battery CRUD round-trip
# ---------------------------------------------------------------------------

def test_crud_battery_round_trip(live_server: str) -> None:
    """POST a config with one battery, GET it back and assert the instance is present."""
    config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "batteries": {
            "home_battery": {
                "capacity_kwh": 13.5,
                "min_soc_kwh": 1.4,
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"unit": "percent"}},
            }
        },
    }
    post_status, post_body = _post_json(f"{live_server}/api/config", config)
    assert post_status == 200
    assert post_body["ok"] is True

    get_status, get_body = _get_json(f"{live_server}/api/config")
    assert get_status == 200
    assert get_body["exists"] is True
    assert "home_battery" in get_body["config"]["batteries"]


# ---------------------------------------------------------------------------
# PV array CRUD round-trip
# ---------------------------------------------------------------------------

def test_crud_pv_round_trip(live_server: str) -> None:
    """POST a config with one PV array, GET it back and assert the instance is present.

    This test passes purely by virtue of the generic CRUD path — no PV-specific
    server code is required.
    """
    config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "pv_arrays": {
            "roof_pv": {"max_power_kw": 8.0},
        },
    }
    post_status, post_body = _post_json(f"{live_server}/api/config", config)
    assert post_status == 200
    assert post_body["ok"] is True

    get_status, get_body = _get_json(f"{live_server}/api/config")
    assert get_status == 200
    assert "roof_pv" in get_body["config"]["pv_arrays"]


# ---------------------------------------------------------------------------
# Multiple instances
# ---------------------------------------------------------------------------

def test_crud_add_second_battery(live_server: str) -> None:
    """POST a config with two battery instances; both are returned on GET."""
    config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "batteries": {
            "battery_a": {
                "capacity_kwh": 10.0,
                "min_soc_kwh": 1.0,
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"unit": "percent"}},
            },
            "battery_b": {
                "capacity_kwh": 6.0,
                "min_soc_kwh": 0.6,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"unit": "percent"}},
            },
        },
    }
    post_status, post_body = _post_json(f"{live_server}/api/config", config)
    assert post_status == 200

    get_status, get_body = _get_json(f"{live_server}/api/config")
    batteries = get_body["config"]["batteries"]
    assert "battery_a" in batteries
    assert "battery_b" in batteries


# ---------------------------------------------------------------------------
# Validation rejection
# ---------------------------------------------------------------------------

def test_crud_field_validation_battery_capacity(live_server: str) -> None:
    """POST a battery with capacity_kwh as a string returns HTTP 422."""
    config = {
        "mqtt": {"host": "localhost", "client_id": "mimir"},
        "grid": {"import_limit_kw": 25.0, "export_limit_kw": 25.0},
        "batteries": {
            "bad_battery": {
                "capacity_kwh": "not-a-number",
                "min_soc_kwh": 1.0,
                "charge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 5.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {"soc": {"unit": "percent"}},
            }
        },
    }
    status, body = _post_json(f"{live_server}/api/config", config)
    assert status == 422
    assert body["ok"] is False
    assert "errors" in body
