"""Integration tests for ReadinessState and the MQTT connection lifecycle.

Verifies that retained MQTT messages published before a mimirheim instance connects
are sufficient to trigger a solve — the broker delivers them on subscribe and
ReadinessState becomes ready without any additional publishes.

Staleness detection is not tested here; that behaviour is a pure Python concern
already covered in full by ``tests/unit/test_readiness.py``.

What this module does not test:
- The solve result itself (covered by test_mqtt_roundtrip.py)
- MQTT publish formatting (covered by unit tests)
- Pydantic model validation (covered by unit tests)
"""

import asyncio
import json
import queue
import threading
import uuid
from datetime import UTC, datetime

import paho.mqtt.client as paho
import pytest

from mimirheim.config.schema import MimirheimConfig

pytestmark = pytest.mark.integration
from mimirheim.core.model_builder import build_and_solve
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.mqtt_client import MqttClient
from mimirheim.io.mqtt_publisher import MqttPublisher

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BAT_SOC_TOPIC = "mimir/input/bat/soc"
_PRICES_TOPIC = "mimir/input/prices"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_battery_config(port: int) -> MimirheimConfig:
    """Return a minimal config with one battery for readiness testing."""
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "127.0.0.1",
            "port": port,
            "client_id": f"mimirheim-readiness-{uid}",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "grid": {"import_limit_kw": 10.0, "export_limit_kw": 5.0},
        "batteries": {
            "bat": {
                "capacity_kwh": 10.0,
                "charge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "discharge_segments": [{"power_max_kw": 3.0, "efficiency": 0.95}],
                "wear_cost_eur_per_kwh": 0.005,
                "inputs": {
                    "soc": {
                        "topic": _BAT_SOC_TOPIC,
                        "unit": "kwh",
                    },
                },
            },
        },
    })


def _make_paho_client(port: int, client_id: str | None = None) -> paho.Client:
    """Create a paho VERSION1 client connected to the local test broker."""
    cid = client_id or f"probe-{uuid.uuid4().hex[:8]}"
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=cid)
    client.connect("127.0.0.1", port)
    client.loop_start()
    return client


def _make_hioo_paho_client(client_id: str) -> paho.Client:
    """Create a paho VERSION1 client for use by MqttClient (no pre-connect).

    ``MqttClient.start()`` calls ``connect()`` and ``loop_start()``.
    """
    return paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id)


def _prices_msg() -> bytes:
    """Return a serialised 96-step prices payload."""
    return json.dumps({
        "steps": [
            {
                "t": t,
                "import_price_eur_kwh": 0.20,
                "export_price_eur_kwh": 0.05,
                "confidence": 1.0,
            }
            for t in range(96)
        ],
    }).encode()


def _bat_soc_msg(ts: datetime | None = None) -> bytes:
    """Return a serialised battery SOC payload with the given timestamp."""
    if ts is None:
        ts = datetime.now(UTC)
    return json.dumps({"soc_kwh": 5.0, "timestamp": ts.isoformat()}).encode()


def _run_solve_loop(
    solve_queue: queue.Queue,
    config: MimirheimConfig,
    publisher: MqttPublisher,
    stop_event: threading.Event,
) -> None:
    """Minimal solve loop for use in integration test threads."""
    while not stop_event.is_set():
        try:
            bundle = solve_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        result = None
        error_msg = None
        try:
            result = build_and_solve(bundle, config)
            if result.solve_status != "infeasible":
                publisher.publish_result(result)
        except Exception as exc:  # noqa: BLE001
            error_msg = str(exc)
        publisher.publish_last_solve_status(result, error_msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_retained_messages_trigger_solve_on_connect(mqtt_broker: str) -> None:
    """Retained MQTT messages deliver inputs on connect and trigger a solve.

    The broker retains the last message for any topic published with
    ``retain=True``. When a new client subscribes, the broker immediately
    delivers those retained messages. This test verifies that mimirheim's
    ReadinessState becomes ready from retained messages alone—no additional
    publishes are needed after the mimirheim client connects.

    This is the expected restart behaviour: after a mimirheim process restarts, it
    should begin solving immediately if previously-retained inputs are still
    within the staleness window.
    """
    port = int(mqtt_broker.split(":")[-1])
    config = _make_battery_config(port)

    # Pre-publish retained inputs using a short-lived probe client, before
    # the mimirheim MqttClient is created. The broker will retain both messages
    # and deliver them when the mimirheim client subscribes.
    pre_probe = _make_paho_client(port, client_id="pre-probe")
    await asyncio.sleep(0.3)  # let the connection complete
    pre_probe.publish(_PRICES_TOPIC, _prices_msg(), qos=1, retain=True)
    pre_probe.publish(_BAT_SOC_TOPIC, _bat_soc_msg(), qos=1, retain=True)
    await asyncio.sleep(0.3)  # let amqtt store the retained messages
    pre_probe.loop_stop()
    pre_probe.disconnect()

    stop_event = threading.Event()
    mqtt_client = None
    result_probe = None
    hioo_paho = None
    solve_thread = None

    try:
        # Now create a fresh mimirheim stack. The retained messages should arrive on
        # subscribe and cause the solve to fire without any additional publishes.
        solve_queue: queue.Queue = queue.Queue(maxsize=1)
        readiness = ReadinessState(config)
        hioo_paho = _make_hioo_paho_client(client_id=f"mimirheim-retained-{uuid.uuid4().hex[:8]}")
        publisher = MqttPublisher(hioo_paho, config)

        mqtt_client = MqttClient(
            config=config,
            readiness=readiness,
            publisher=publisher,
            paho_client=hioo_paho,
            solve_queue=solve_queue,
        )

        schedule_received = threading.Event()

        result_probe = _make_paho_client(port, client_id=f"result-probe-{uuid.uuid4().hex[:8]}")
        result_probe.subscribe(config.outputs.schedule, qos=1)

        def _on_result(client: paho.Client, userdata: object, msg: paho.MQTTMessage) -> None:
            schedule_received.set()

        result_probe.on_message = _on_result

        solve_thread = threading.Thread(
            target=_run_solve_loop,
            args=(solve_queue, config, publisher, stop_event),
            daemon=True,
        )

        mqtt_client.start()
        solve_thread.start()

        # Wait for retained messages to arrive, solve, and result to be published.
        for _ in range(20):
            await asyncio.sleep(0.5)
            if schedule_received.is_set():
                break
    finally:
        stop_event.set()
        if solve_thread is not None:
            solve_thread.join(timeout=5.0)
        if mqtt_client is not None:
            mqtt_client.stop()
        elif hioo_paho is not None:
            hioo_paho.loop_stop()
            hioo_paho.disconnect()
        if result_probe is not None:
            result_probe.loop_stop()
            result_probe.disconnect()

    assert schedule_received.is_set(), (
        "Retained messages did not trigger a solve within 10 seconds of connecting."
    )
