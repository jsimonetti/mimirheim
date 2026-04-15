"""Integration tests for the full MQTT round-trip.

These tests wire together all mimirheim components — ReadinessState, MqttClient,
MqttPublisher, and the solve loop — and exercise them against an in-process
amqtt broker. They validate end-to-end behaviour: inputs arrive on MQTT,
the solver runs, outputs are published back to MQTT.

These tests do NOT import or invoke ``mimirheim.__main__``. They assemble the
components directly, which makes failure isolation clearer: if a test fails,
the cause is in the component layer, not the argument-parsing or signal-
handling glue in the entry point.

What this module does not test:
- Argument parsing in ``__main__``
- Config file loading from disk
- SIGTERM/SIGINT shutdown
- Debug dump file writing
"""

import asyncio
import json
import queue
import threading
import uuid
from datetime import UTC, datetime, timedelta

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

_PRICES_96 = [0.10 if t < 48 else 0.30 for t in range(96)]
_EXPORT_96 = [0.05] * 96
_CONF_96 = [1.0] * 96

_BAT_SOC_TOPIC = "mimir/input/bat/soc"
_PRICES_TOPIC = "mimir/input/prices"
_BASE_FORECAST_TOPIC = "mimir/input/base/forecast"
_TRIGGER_TOPIC = "mimir/input/trigger"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_battery_config(port: int) -> MimirheimConfig:
    """Return a minimal config with one battery and no other devices.

    The battery SOC topic and the prices topic are the two required inputs.
    """
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "127.0.0.1",
            "port": port,
            "client_id": f"mimirheim-{uid}",
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


def _make_infeasible_config(port: int) -> MimirheimConfig:
    """Return a config that always produces an infeasible solve.

    The grid import limit is set to zero while a static load consumes constant
    power. With no generation capability and no imports allowed, the power
    balance constraint cannot be satisfied for any time step.
    """
    uid = uuid.uuid4().hex[:8]
    return MimirheimConfig.model_validate({
        "mqtt": {
            "host": "127.0.0.1",
            "port": port,
            "client_id": f"mimirheim-infeasible-{uid}",
            "topic_prefix": "mimir",
        },
        "outputs": {
            "schedule": "mimir/schedule",
            "current": "mimir/current",
            "last_solve": "mimir/status/last_solve",
            "availability": "mimir/status/availability",
        },
        "grid": {"import_limit_kw": 0.0, "export_limit_kw": 5.0},
        "static_loads": {
            "base": {"topic_forecast": _BASE_FORECAST_TOPIC},
        },
    })


def _make_paho_client(port: int, client_id: str | None = None) -> paho.Client:
    """Create a paho VERSION1 client connected to the local test broker.

    Connects and starts the network loop immediately. Use this for probe
    clients that need to be connected before the mimirheim stack is wired up.

    Args:
        port: Broker port.
        client_id: Optional client identifier. A random UUID is used if None.

    Returns:
        A connected paho client with ``loop_start()`` already called.
    """
    cid = client_id or f"probe-{uuid.uuid4().hex[:8]}"
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=cid)
    client.connect("127.0.0.1", port)
    client.loop_start()
    return client


def _make_hioo_paho_client(client_id: str) -> paho.Client:
    """Create a paho VERSION1 client for use by MqttClient.

    Does NOT connect. ``MqttClient.start()`` calls ``connect()`` and
    ``loop_start()``, so the client must not be pre-connected.

    Args:
        client_id: MQTT client identifier.

    Returns:
        An unconnected paho client with callbacks not yet registered.
    """
    return paho.Client(paho.CallbackAPIVersion.VERSION2, client_id=client_id)


def _run_solve_loop(
    solve_queue: queue.Queue,
    config: MimirheimConfig,
    publisher: MqttPublisher,
    stop_event: threading.Event,
) -> None:
    """Drain the solve queue and call build_and_solve until stop_event is set.

    Publishes the result and status after each solve. Exceptions inside
    build_and_solve are caught, logged to stdout, and reported via the status
    topic.

    Args:
        solve_queue: Queue that receives SolveBundle objects from MqttClient.
        config: Static system configuration.
        publisher: MQTT publisher for results and status.
        stop_event: Set this event to shut down the loop cleanly.
    """
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
            print(f"[solve thread] error: {exc}")
        publisher.publish_last_solve_status(result, error_msg)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_full_stack_publishes_schedule(mqtt_broker: str) -> None:
    """Publishing valid inputs to the broker causes a schedule to be published.

    This test verifies the critical happy path: prices and battery SOC arrive
    on MQTT, ReadinessState becomes ready, the solver runs, and the schedule
    is published to the output topic within a reasonable time window.
    """
    port = int(mqtt_broker.split(":")[-1])
    config = _make_battery_config(port)

    stop_event = threading.Event()
    mqtt_client = None
    probe = None
    hioo_paho = None
    solve_thread = None

    try:
        solve_queue: queue.Queue = queue.Queue(maxsize=1)
        readiness = ReadinessState(config)
        hioo_paho = _make_hioo_paho_client(client_id=f"mimirheim-{uuid.uuid4().hex[:8]}")
        publisher = MqttPublisher(hioo_paho, config)

        # MqttClient must accept solve_queue — this call will raise TypeError if the
        # parameter has not yet been added, which is the expected "red" test signal.
        mqtt_client = MqttClient(
            config=config,
            readiness=readiness,
            publisher=publisher,
            paho_client=hioo_paho,
            solve_queue=solve_queue,
        )

        schedule_received = threading.Event()
        schedule_payload: dict = {}

        probe = _make_paho_client(port)
        probe.subscribe(config.outputs.schedule, qos=1)

        def _on_probe_message(client: paho.Client, userdata: object, msg: paho.MQTTMessage) -> None:
            schedule_payload.update(json.loads(msg.payload))
            schedule_received.set()

        probe.on_message = _on_probe_message

        solve_thread = threading.Thread(
            target=_run_solve_loop,
            args=(solve_queue, config, publisher, stop_event),
            daemon=True,
        )

        mqtt_client.start()
        solve_thread.start()

        # Give paho and amqtt time to establish connections and subscriptions.
        await asyncio.sleep(0.5)

        prices_msg = json.dumps([
            {
                "ts": (datetime.now(UTC) + timedelta(minutes=15 * t)).isoformat(),
                "import_eur_per_kwh": _PRICES_96[t],
                "export_eur_per_kwh": _EXPORT_96[t],
                "confidence": 1.0,
            }
            for t in range(96)
        ]).encode()
        probe.publish(_PRICES_TOPIC, prices_msg, qos=1)

        bat_msg = b"5.0"
        probe.publish(_BAT_SOC_TOPIC, bat_msg, qos=1)
        # Trigger the solve. Data topics alone do not queue a solve;
        # the trigger topic must receive a non-retained message.
        await asyncio.sleep(0.2)
        probe.publish(_TRIGGER_TOPIC, b"", qos=1, retain=False)

        # Wait for the full round trip: MQTT delivery, solve (HiGHS), publish.
        # HiGHS on a 96-step battery problem typically solves in under 2 seconds.
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
        if probe is not None:
            probe.loop_stop()
            probe.disconnect()

    assert schedule_received.is_set(), (
        "No schedule was published within 10 seconds of publishing inputs."
    )
    assert "schedule" in schedule_payload, (
        f"Published schedule is missing 'schedule' key: {schedule_payload}"
    )
    assert len(schedule_payload["schedule"]) >= 94, (
        f"Expected at least 94 schedule steps (up to 96 depending on solve time), got {len(schedule_payload['schedule'])}."
    )


async def test_infeasible_solve_publishes_error_status(mqtt_broker: str) -> None:
    """An infeasible solve publishes status 'error' on the last_solve topic.

    Configures a system in which no solution can exist (zero grid import with
    a non-zero static load and no generation). The solver returns 'infeasible',
    and the publisher must publish a status message with ``"status": "error"``
    on the configured last_solve topic.
    """
    port = int(mqtt_broker.split(":")[-1])
    config = _make_infeasible_config(port)

    stop_event = threading.Event()
    mqtt_client = None
    probe = None
    hioo_paho = None
    solve_thread = None

    try:
        solve_queue: queue.Queue = queue.Queue(maxsize=1)
        readiness = ReadinessState(config)
        hioo_paho = _make_hioo_paho_client(client_id=f"mimirheim-infeas-{uuid.uuid4().hex[:8]}")
        publisher = MqttPublisher(hioo_paho, config)

        mqtt_client = MqttClient(
            config=config,
            readiness=readiness,
            publisher=publisher,
            paho_client=hioo_paho,
            solve_queue=solve_queue,
        )

        status_received = threading.Event()
        status_payload: dict = {}
        last_schedule: list = []

        probe = _make_paho_client(port)
        probe.subscribe(config.outputs.last_solve, qos=1)
        probe.subscribe(config.outputs.schedule, qos=1)

        def _on_probe_message(client: paho.Client, userdata: object, msg: paho.MQTTMessage) -> None:
            if msg.topic == config.outputs.last_solve:
                status_payload.update(json.loads(msg.payload))
                status_received.set()
            elif msg.topic == config.outputs.schedule:
                last_schedule.append(json.loads(msg.payload))

        probe.on_message = _on_probe_message

        solve_thread = threading.Thread(
            target=_run_solve_loop,
            args=(solve_queue, config, publisher, stop_event),
            daemon=True,
        )

        mqtt_client.start()
        solve_thread.start()

        await asyncio.sleep(0.5)

        prices_msg = json.dumps([
            {
                "ts": (datetime.now(UTC) + timedelta(minutes=15 * t)).isoformat(),
                "import_eur_per_kwh": 0.10,
                "export_eur_per_kwh": 0.05,
                "confidence": 1.0,
            }
            for t in range(96)
        ]).encode()
        probe.publish(_PRICES_TOPIC, prices_msg, qos=1)

        # Static load forecast: 1 kW constant across all steps.
        base_msg = json.dumps([
            {
                "ts": (datetime.now(UTC) + timedelta(minutes=15 * t)).isoformat(),
                "kw": 1.0,
            }
            for t in range(96)
        ]).encode()
        probe.publish(_BASE_FORECAST_TOPIC, base_msg, qos=1)
        # Trigger the solve.
        await asyncio.sleep(0.2)
        probe.publish(_TRIGGER_TOPIC, b"", qos=1, retain=False)

        for _ in range(20):
            await asyncio.sleep(0.5)
            if status_received.is_set():
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
        if probe is not None:
            probe.loop_stop()
            probe.disconnect()

    assert status_received.is_set(), (
        "No status message was published within 10 seconds of publishing inputs."
    )
    assert status_payload.get("status") == "error", (
        f"Expected status='error' for infeasible solve, got: {status_payload}"
    )
    assert last_schedule == [], (
        "Schedule should not be published for an infeasible solve."
    )
