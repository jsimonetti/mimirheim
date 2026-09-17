"""Integration test for the Config Service Descriptor round trip.

Connects a real MqttClient (mimirheim core's one and only MQTT connection;
see mimirheim_shared/docs/adr/0005) to an in-process amqtt broker and confirms
a second, independent client can read the retained Descriptor it publishes —
proving the retained publish (and topic naming) work end to end, not just
against a faked paho client. See tests/unit/test_mqtt_client.py for the
faked-client unit coverage of the Descriptor publish/clear behaviour.
"""

import asyncio
import uuid
from pathlib import Path

import paho.mqtt.client as paho
import pytest

pytestmark = pytest.mark.integration

from mimirheim.config.schema import GridConfig, MimirheimConfig, MqttConfig
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.config_service import OWNER_ID, TOPIC
from mimirheim.io.mqtt_client import MqttClient
from mimirheim.io.mqtt_publisher import MqttPublisher
from mimirheim_shared.config_service import Descriptor


def _make_config(port: int) -> MimirheimConfig:
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig(
        mqtt=MqttConfig(host="127.0.0.1", port=port, client_id=f"mimirheim-{uid}"),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
    )


async def test_descriptor_is_retained_and_readable_by_a_second_client(
    mqtt_broker: str, tmp_path: Path
) -> None:
    port = int(mqtt_broker.split(":")[-1])
    config = _make_config(port)

    paho_client = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=config.mqtt.client_id,
    )
    readiness = ReadinessState(config)
    publisher = MqttPublisher(paho_client, config)
    mqtt_client = MqttClient(
        config, readiness, publisher, paho_client, tmp_path / "mimirheim.yaml"
    )

    probe = paho.Client(
        paho.CallbackAPIVersion.VERSION2,
        client_id=f"probe-{uuid.uuid4().hex[:8]}",
    )
    probe.connect("127.0.0.1", port)
    probe.loop_start()

    received = asyncio.Event()
    payloads: list[bytes] = []
    loop = asyncio.get_event_loop()

    def _on_probe_message(_client: paho.Client, _userdata: object, msg: paho.MQTTMessage) -> None:
        payloads.append(msg.payload)
        loop.call_soon_threadsafe(received.set)

    probe.on_message = _on_probe_message

    try:
        mqtt_client.start()
        await asyncio.sleep(0.5)

        # Subscribing after the retained publish must still deliver it: that
        # is the whole point of retain=True for a Descriptor a Config Editor
        # may connect to well after mimirheim core has started.
        probe.subscribe(TOPIC, qos=1)

        await asyncio.wait_for(received.wait(), timeout=10.0)
    finally:
        mqtt_client.stop()
        probe.loop_stop()
        probe.disconnect()

    assert payloads, "No retained Descriptor was received by the second client."
    descriptor = Descriptor.model_validate_json(payloads[0])
    assert descriptor.owner_id == OWNER_ID
