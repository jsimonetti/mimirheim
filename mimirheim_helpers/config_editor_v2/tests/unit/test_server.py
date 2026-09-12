"""Unit tests for config_editor_v2.server.ConfigEditorV2Server.

Tests verify, against the server's own `handle_request` (called directly,
per its own docstring, so no live socket is needed):

- `GET /api/entries` serves a real registered model's schema with a native,
  non-`x-mimir-` hint (`mqtt.password`'s `x-format: "password"`) intact.
  No field in any registered model currently carries an `x-mimir-adapter`
  hint, so `adapter.transform_schema_document`/`transform_value_document`
  are pass-throughs in these tests, exactly as in production today.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_editor_v2.registry import REGISTRY
from config_editor_v2.server import ConfigEditorV2Server


@pytest.fixture
def server(tmp_path: Path):
    instance = ConfigEditorV2Server(config_dir=tmp_path, port=0, entries=REGISTRY)
    yield instance
    # Not instance.shutdown(): these tests only ever call handle_request()
    # directly, never serve_forever(), and shutdown() blocks until
    # serve_forever()'s own loop notices the shutdown flag -- a loop that,
    # here, never started. server_close() just releases the listening
    # socket this constructor opened.
    instance._httpd.server_close()


def _get(server: ConfigEditorV2Server, path: str) -> dict:
    status, _headers, body = server.handle_request("GET", path, body=b"")
    assert status == 200
    return json.loads(body)


def _mimirheim_schema(server: ConfigEditorV2Server) -> dict:
    entries = _get(server, "/api/entries")
    (mimirheim,) = [entry for entry in entries if entry["name"] == "Mimirheim"]
    return mimirheim["schema"]


def test_mqtt_password_field_is_masked(server: ConfigEditorV2Server) -> None:
    """The real, live schema masks `mqtt.password` via Jedison's native `x-format`."""
    schema = _mimirheim_schema(server)

    password_schema = schema["$defs"]["MqttConfig"]["properties"]["password"]

    assert password_schema["x-format"] == "password"


def test_add_property_content_fields_get_object_add(server: ConfigEditorV2Server) -> None:
    """Every real field naming an add-button label gets `x-objectAdd: True`.

    `pv_arrays` sets `x-addPropertyContent` in schema.py with no matching
    manual `x-objectAdd` override; this is the live case
    `jedison_mapping.to_jedison_object_schema` exists to fix automatically.
    """
    schema = _mimirheim_schema(server)

    assert schema["properties"]["batteries"]["x-objectAdd"] is True
    assert schema["properties"]["pv_arrays"]["x-objectAdd"] is True
