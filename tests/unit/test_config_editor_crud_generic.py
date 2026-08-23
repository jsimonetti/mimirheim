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
import socket
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

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


# ---------------------------------------------------------------------------
# Request body size and Content-Length handling
# ---------------------------------------------------------------------------


def _raw_post(base_url: str, path: str, headers: bytes, body: bytes) -> int:
    """Send a hand-built POST over a socket and return the response status.

    urllib computes Content-Length itself, so a malformed or lying header has
    to be written by hand.
    """
    host, port = base_url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        sock.sendall(
            b"POST " + path.encode() + b" HTTP/1.1\r\nHost: localhost\r\n"
            + headers
            + b"\r\n"
            + body
        )
        sock.shutdown(socket.SHUT_WR)
        raw = b""
        while chunk := sock.recv(4096):
            raw += chunk
    status_line = raw.split(b"\r\n", 1)[0]
    return int(status_line.split(b" ")[1])


def test_malformed_content_length_returns_400(live_server: str) -> None:
    """int() on a non-numeric header raised inside the handler thread."""
    status = _raw_post(
        live_server,
        "/api/config",
        b"Content-Type: application/json\r\nContent-Length: not-a-number\r\n",
        b"{}",
    )
    assert status == 400


def test_negative_content_length_returns_400(live_server: str) -> None:
    status = _raw_post(
        live_server,
        "/api/config",
        b"Content-Type: application/json\r\nContent-Length: -5\r\n",
        b"{}",
    )
    assert status == 400


def test_oversized_body_returns_413(live_server: str) -> None:
    """A declared length above the cap must be refused, not buffered.

    self.rfile.read(length) read whatever the client declared straight into
    memory. On a Home Assistant add-on box that is a cheap way to exhaust it.
    """
    from config_editor.server import MAX_REQUEST_BODY_BYTES

    status = _raw_post(
        live_server,
        "/api/config",
        b"Content-Type: application/json\r\nContent-Length: "
        + str(MAX_REQUEST_BODY_BYTES + 1).encode()
        + b"\r\n",
        b"{}",
    )
    assert status == 413


def test_a_normal_sized_body_is_still_accepted(live_server: str) -> None:
    """Regression guard: a real config must stay well under the cap."""
    status, _data = _post_json(
        f"{live_server}/api/helper-config/nordpool.yaml", {"enabled": False}
    )
    assert status == 200


def test_missing_content_length_is_treated_as_empty(live_server: str) -> None:
    """No header means no body, which the JSON parser rejects as a 400."""
    status = _raw_post(
        live_server, "/api/config", b"Content-Type: application/json\r\n", b""
    )
    assert status == 400


def test_the_body_cap_is_a_sane_size() -> None:
    """The cap has to be small enough to be worth having.

    The oversize test above derives its Content-Length from this constant, so
    it stays green no matter how large the constant becomes. This pins the
    value itself: the largest thing this API legitimately receives is a full
    mimirheim.yaml as JSON, a few tens of kilobytes.
    """
    from config_editor.server import MAX_REQUEST_BODY_BYTES

    assert 64 * 1024 <= MAX_REQUEST_BODY_BYTES <= 8 * 1024 * 1024
