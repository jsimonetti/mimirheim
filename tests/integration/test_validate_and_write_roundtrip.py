"""Integration test for the Config Service validate_and_write round trip.

Connects a real MqttClient (mimirheim core's one and only MQTT connection;
see mimirheim_shared/docs/adr/0005) to an in-process amqtt broker, publishes a
validate_and_write request from a second, independent client the way a Config
Editor would, and confirms the published result and the on-disk configuration
file end to end — not just against a faked paho client. See
tests/unit/test_config_service.py for the faked-client unit coverage of
validation and the write itself.
"""

import asyncio
import uuid
from pathlib import Path

import paho.mqtt.client as paho
import pytest
from ruamel.yaml import YAML

pytestmark = pytest.mark.integration

from mimirheim.config.schema import GridConfig, MimirheimConfig, MqttConfig
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.config_service import REQUEST_TOPIC, RESPONSE_TOPIC
from mimirheim.io.mqtt_client import MqttClient
from mimirheim.io.mqtt_publisher import MqttPublisher
from mimirheim_shared.config_service import ValidateAndWriteRequest, ValidateAndWriteResult

FIXTURE_PATH = Path(__file__).parent.parent / "unit" / "fixtures" / "sample_mimirheim_config.yaml"


def _make_config(port: int) -> MimirheimConfig:
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig(
        mqtt=MqttConfig(host="127.0.0.1", port=port, client_id=f"mimirheim-{uid}"),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
    )


async def test_validate_and_write_round_trip_against_a_real_broker(
    mqtt_broker: str, tmp_path: Path
) -> None:
    port = int(mqtt_broker.split(":")[-1])
    config = _make_config(port)
    config_path = tmp_path / "mimirheim.yaml"
    config_path.write_text(FIXTURE_PATH.read_text())

    paho_client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=config.mqtt.client_id,
    )
    readiness = ReadinessState(config)
    publisher = MqttPublisher(paho_client, config)
    mqtt_client = MqttClient(config, readiness, publisher, paho_client, config_path)

    editor = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=f"editor-{uuid.uuid4().hex[:8]}",
    )
    editor.connect("127.0.0.1", port)
    editor.loop_start()

    received = asyncio.Event()
    payloads: list[bytes] = []
    loop = asyncio.get_event_loop()

    def _on_editor_message(_client: paho.Client, _userdata: object, msg: paho.MQTTMessage) -> None:
        payloads.append(msg.payload)
        loop.call_soon_threadsafe(received.set)

    editor.on_message = _on_editor_message

    try:
        mqtt_client.start()
        await asyncio.sleep(0.5)

        editor.subscribe(RESPONSE_TOPIC, qos=1)
        await asyncio.sleep(0.2)

        request = ValidateAndWriteRequest(
            request_id="editor-req-1",
            values={
                "mqtt": {"host": "localhost", "port": 1883},
                "grid": {"import_limit_kw": 12.0, "export_limit_kw": 5.0},
            },
        )
        editor.publish(REQUEST_TOPIC, payload=request.model_dump_json().encode("utf-8"), qos=1)

        await asyncio.wait_for(received.wait(), timeout=10.0)
    finally:
        mqtt_client.stop()
        editor.loop_stop()
        editor.disconnect()

    assert payloads, "No validate_and_write result was received by the editor."
    result = ValidateAndWriteResult.model_validate_json(payloads[0])
    assert result == ValidateAndWriteResult(request_id="editor-req-1", success=True)

    yaml = YAML()
    with config_path.open() as fh:
        written = yaml.load(fh)
    assert written["grid"]["import_limit_kw"] == 12.0
    assert written["grid"]["export_limit_kw"] == 5.0
