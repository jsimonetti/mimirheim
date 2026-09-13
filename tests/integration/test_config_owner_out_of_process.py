"""Integration test proving the Config Service protocol works with a
genuinely out-of-process Config Owner (config-editor-v3 ticket 09).

Runs nordpool (ticket 06's first helper Config Owner) as a real OS
subprocess, reachable only via the shared in-process amqtt broker -- there is
no in-process shortcut of any kind between the Config Editor and this Config
Owner. Confirms discovery and edit round-trip identically to the in-process
case, and that killing the subprocess clears it from the registry via its
last-will without disrupting the Config Editor's ability to keep serving
mimirheim core's configuration and a second, unrelated, still-running
in-process Config Owner's.

What this module does not test:
- A second genuinely out-of-process Config Owner running alongside the
  first (only one real subprocess is exercised here; see the module
  docstring's cross-references below for the in-process-only variant of
  discovery/render/edit).
- The Config Editor's real HTTP transport (`ConfigEditorServer.serve_forever`
  and its socket handling) -- `handle_request` is called directly, matching
  every other test of this server; see `config_editor_v3/server.py`'s own
  docstring for why that call is equivalent to a live request.
- FormSpec rendering details (Tier collapse, Conditional Visibility, nested
  shapes) -- covered by `mimirheim_helpers/config_editor_v3/tests/unit/test_render.py`.

See tests/integration/test_config_service_roundtrip.py and
test_validate_and_write_roundtrip.py for the faked-in-process-owner variant
of the same protocol; this test's only added dimension is genuine process
separation for the Config Owner under test (nordpool).
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import urllib.parse
import uuid
from collections.abc import Callable
from pathlib import Path

import paho.mqtt.client as paho
import pytest
import yaml
from pydantic import BaseModel, ConfigDict

pytestmark = pytest.mark.integration

from mimirheim.config.schema import GridConfig, MimirheimConfig, MqttConfig as CoreMqttConfig
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.mqtt_client import MqttClient
from mimirheim.io.mqtt_publisher import MqttPublisher

from helper_common.config import MqttConfig as HelperMqttConfig
from helper_common.config_owner import ConfigOwnerSupport

from config_editor_v3.mqtt_client import ConfigEditorMqttClient
from config_editor_v3.registry import ConfigOwnerRegistry
from config_editor_v3.render import RenderedGroup, build_groups
from config_editor_v3.server import ConfigEditorServer
from mimirheim_shared.formspec import FieldSpec, FormSpec

_NORDPOOL_OWNER_ID = "nordpool"
_CORE_OWNER_ID = "mimirheim-core"
_THIRD_OWNER_ID = "third-party-helper"

_CORE_FIXTURE_PATH = Path(__file__).parent.parent / "unit" / "fixtures" / "sample_mimirheim_config.yaml"


class _ThirdOwnerConfig(BaseModel):
    """A minimal, unrelated Config Owner model.

    Stands in for "any other running Config Owner" independently of
    mimirheim core, so the graceful-degradation assertions below prove that
    nordpool's outage does not affect an unrelated owner, not just core's
    own (which already has a dedicated topic/permission path of its own).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


_THIRD_OWNER_FORM_SPEC = FormSpec(fields={"enabled": FieldSpec(label="Enabled", description="Enabled.")})


class _EditorConfig:
    """Minimal stand-in for `ConfigEditorV3Config`; `ConfigEditorMqttClient` only reads `.mqtt`."""

    def __init__(self, port: int) -> None:
        self.mqtt = HelperMqttConfig(host="127.0.0.1", port=port, client_id=f"editor-{uuid.uuid4().hex[:8]}")


def _make_core_config(port: int) -> MimirheimConfig:
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig(
        mqtt=CoreMqttConfig(host="127.0.0.1", port=port, client_id=f"mimirheim-{uid}"),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
    )


def _write_nordpool_config(path: Path, port: int) -> None:
    path.write_text(
        f"""
mqtt:
  host: 127.0.0.1
  port: {port}
  client_id: nordpool-out-of-process-test

trigger_topic: mimir/input/tools/prices/trigger

nordpool:
  area: NL
"""
    )


def _flatten_leaf_values(groups: list[RenderedGroup]) -> dict[str, str]:
    """Collects every leaf field's dotted name/value from a rendered tree.

    Mirrors what a real browser submits for the same rendered page. Copied
    from tests/unit/test_server.py's own e2e helper (config_editor_v3),
    which cannot be imported directly since it lives in another package's
    test tree.
    """
    values: dict[str, str] = {}
    for group in groups:
        for rendered_field in (*group.basic_fields, *group.expert_fields):
            if rendered_field.entries is not None:
                for entry in rendered_field.entries:
                    values.update(_flatten_leaf_values(entry.groups))
            elif rendered_field.nested_groups is not None:
                values.update(_flatten_leaf_values(rendered_field.nested_groups))
            elif rendered_field.value is not None:
                values[rendered_field.name] = str(rendered_field.value)
    return values


async def _get(server: ConfigEditorServer, path: str) -> tuple[int, str]:
    """Calls handle_request in a worker thread.

    A GET on an owner path blocks the calling thread on a `threading.Event`
    (`ConfigEditorMqttClient.get_current_values`) until the MQTT response
    arrives. The in-process amqtt broker that must relay that response runs
    as asyncio tasks on this test's own event loop, so calling
    `handle_request` directly from the test coroutine would freeze the very
    loop the response depends on -- a self-deadlock. `asyncio.to_thread`
    keeps the block off the event loop's thread.
    """
    status, _headers, body = await asyncio.to_thread(server.handle_request, "GET", path, b"")
    return status, body.decode("utf-8")


async def _post(server: ConfigEditorServer, path: str, form: dict[str, str]) -> tuple[int, str]:
    """Calls handle_request in a worker thread. See `_get` for why."""
    body = urllib.parse.urlencode(form).encode("utf-8")
    status, _headers, response_body = await asyncio.to_thread(server.handle_request, "POST", path, body)
    return status, response_body.decode("utf-8")


async def _wait_for(predicate: Callable[[], bool], timeout: float = 20.0, interval: float = 0.2) -> bool:
    """Polls `predicate` until it is truthy, yielding to the event loop between tries.

    The in-process amqtt broker (`mqtt_broker` fixture) runs as asyncio tasks
    on this same event loop; a blocking `time.sleep` here would starve it of
    scheduling time, so this awaits `asyncio.sleep` instead.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


async def test_out_of_process_config_owner_round_trips_and_degrades_gracefully(
    mqtt_broker: str, tmp_path: Path
) -> None:
    port = int(mqtt_broker.split(":")[-1])

    # --- mimirheim core, in-process. ---
    core_config_path = tmp_path / "mimirheim.yaml"
    core_config_path.write_text(_CORE_FIXTURE_PATH.read_text())
    core_config = _make_core_config(port)
    core_paho_client = paho.Client(
        paho.CallbackAPIVersion.VERSION2, client_id=core_config.mqtt.client_id
    )
    readiness = ReadinessState(core_config)
    publisher = MqttPublisher(core_paho_client, core_config)
    core_mqtt_client = MqttClient(
        core_config, readiness, publisher, core_paho_client, core_config_path
    )

    # --- the Config Editor itself, in-process. ---
    registry = ConfigOwnerRegistry()
    editor_mqtt_client = ConfigEditorMqttClient(_EditorConfig(port), registry)
    server = ConfigEditorServer(registry, editor_mqtt_client)

    # --- a third, unrelated in-process Config Owner, wired up the same way
    # a real helper's __main__.py would (helper_common.config_owner). Proves
    # the last acceptance criterion's "(and any other running Config
    # Owner's)" against an owner distinct from mimirheim core, not core
    # reused, and answers get_current_values for real rather than timing
    # out (which a bare retained-Descriptor publish with no responder would). ---
    third_owner_config_path = tmp_path / "third-party-helper.yaml"
    third_owner_config_path.write_text("enabled: false\n")
    third_owner_support = ConfigOwnerSupport(
        owner_id=_THIRD_OWNER_ID,
        display_name="Third-party helper",
        model=_ThirdOwnerConfig,
        form_spec=_THIRD_OWNER_FORM_SPEC,
        config_path=third_owner_config_path,
    )
    third_owner_client = paho.Client(
        paho.CallbackAPIVersion.VERSION2, client_id=f"third-owner-{uuid.uuid4().hex[:8]}"
    )
    third_owner_client.on_message = (
        lambda client, _userdata, message: third_owner_support.handle_message(client, message)
    )
    third_owner_client.on_connect = (
        lambda client, _userdata, _flags, reason_code, _properties: (
            None if reason_code.is_failure else third_owner_support.on_connect(client)
        )
    )
    third_owner_client.connect("127.0.0.1", port)
    third_owner_client.loop_start()

    # --- nordpool, as a genuinely separate OS process, reachable only via
    # the shared broker. ---
    nordpool_config_path = tmp_path / "nordpool.yaml"
    _write_nordpool_config(nordpool_config_path, port)
    nordpool_log_path = tmp_path / "nordpool.log"
    nordpool_process = subprocess.Popen(
        [sys.executable, "-m", "nordpool", "--config", str(nordpool_config_path)],
        stdout=nordpool_log_path.open("w"),
        stderr=subprocess.STDOUT,
    )

    try:
        core_mqtt_client.start()
        editor_mqtt_client.start()

        assert await _wait_for(lambda: registry.get(_CORE_OWNER_ID) is not None), (
            "mimirheim core was never discovered."
        )
        assert await _wait_for(lambda: registry.get(_NORDPOOL_OWNER_ID) is not None), (
            f"nordpool subprocess was never discovered. Log:\n{nordpool_log_path.read_text()}"
        )
        assert await _wait_for(lambda: registry.get(_THIRD_OWNER_ID) is not None), (
            "the third-party in-process Config Owner was never discovered."
        )

        # --- Discovery + render: identical to the in-process case. ---
        status, body = await _get(server, f"/owners/{_NORDPOOL_OWNER_ID}")
        assert status == 200
        assert "Nordpool area" in body
        assert 'value="NL"' in body

        # --- Edit: submitted through the same code path a real browser POST
        # takes (server.handle_request), forwarded over MQTT to the separate
        # nordpool process, which validates and writes its own file. ---
        descriptor = registry.get(_NORDPOOL_OWNER_ID)
        assert descriptor is not None
        current_values = await asyncio.to_thread(
            editor_mqtt_client.get_current_values, _NORDPOOL_OWNER_ID
        )
        form = _flatten_leaf_values(build_groups(descriptor, values=current_values))
        form["nordpool.area"] = "NO2"

        status, body = await _post(server, f"/owners/{_NORDPOOL_OWNER_ID}", form)
        assert status == 200, body
        assert "Saved" in body

        written = yaml.safe_load(nordpool_config_path.read_text())
        assert written["nordpool"]["area"] == "NO2"

        # --- Ungraceful stop: kill (not terminate) so the crash-safe
        # last-will fires, per ADR-0005/helper_common.config_owner. ---
        nordpool_process.kill()
        await asyncio.to_thread(nordpool_process.wait, 10.0)

        assert await _wait_for(lambda: registry.get(_NORDPOOL_OWNER_ID) is None), (
            "nordpool's Descriptor was not cleared from the registry after the process was killed."
        )

        # --- Graceful degradation: the stopped owner looks unavailable, not
        # broken, and everything else keeps working. ---
        status, body = await _get(server, f"/owners/{_NORDPOOL_OWNER_ID}")
        assert status == 404

        status, body = await _get(server, "/")
        assert status == 200
        assert "nordpool" not in body.lower()

        status, body = await _get(server, f"/owners/{_CORE_OWNER_ID}")
        assert status == 200

        write_result = await asyncio.to_thread(
            editor_mqtt_client.submit_validate_and_write,
            _CORE_OWNER_ID,
            {"grid": {"import_limit_kw": 11.0, "export_limit_kw": 5.0}},
        )
        assert write_result.success, write_result.errors

        # An owner unrelated to both mimirheim core and nordpool also keeps
        # working: nordpool's outage was not a general editor failure.
        status, body = await _get(server, f"/owners/{_THIRD_OWNER_ID}")
        assert status == 200
    finally:
        if nordpool_process.poll() is None:
            nordpool_process.kill()
            nordpool_process.wait(timeout=10.0)
        core_mqtt_client.stop()
        editor_mqtt_client.stop()
        third_owner_client.loop_stop()
        third_owner_client.disconnect()
        server._httpd.server_close()
