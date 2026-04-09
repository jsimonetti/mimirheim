"""Unit tests for trigger debouncing in mimirheim.io.mqtt_client.MqttClient.

Tests verify that two triggers arriving within the 5-second debounce window
result in only one solve being queued, and that a trigger arriving after the
window expires is allowed through.
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

from mimirheim.config.schema import (
    BatteryConfig,
    BatteryInputsConfig,
    EfficiencySegment,
    GridConfig,
    MimirheimConfig,
    MqttConfig,
    OutputsConfig,
    SocTopicConfig,
)
from mimirheim.core.readiness import ReadinessState
from mimirheim.io.mqtt_client import MqttClient


def _seg() -> EfficiencySegment:
    return EfficiencySegment(power_max_kw=5.0, efficiency=0.95)


def _make_config() -> MimirheimConfig:
    return MimirheimConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        outputs=OutputsConfig(
            schedule="mimir/schedule",
            current="mimir/current",
            last_solve="mimir/status",
            availability="mimir/status/availability",
        ),
        grid=GridConfig(import_limit_kw=10.0, export_limit_kw=5.0),
        batteries={
            "bat": BatteryConfig(
                capacity_kwh=10.0,
                charge_segments=[_seg()],
                discharge_segments=[_seg()],
                inputs=BatteryInputsConfig(
                    soc=SocTopicConfig(
                        topic="home/bat/soc",
                        unit="kwh",
                    )
                ),
            )
        },
    )


def _make_trigger_msg(retain: bool = False) -> MagicMock:
    msg = MagicMock()
    msg.retain = retain
    msg.topic = "mimir/input/trigger"
    msg.payload = b""
    return msg


def _make_mqtt_client(solve_queue: queue.Queue | None = None) -> MqttClient:
    config = _make_config()
    readiness = MagicMock(spec=ReadinessState)
    readiness.is_ready.return_value = True
    readiness.snapshot.return_value = MagicMock()
    publisher = MagicMock()
    paho_mock = MagicMock()
    return MqttClient(config, readiness, publisher, paho_mock, solve_queue=solve_queue)


class TestTriggerDebounce:
    def test_second_trigger_within_debounce_window_is_dropped(self) -> None:
        """Two triggers 3 s apart must produce only one queued solve."""
        q: queue.Queue = queue.Queue()
        client = _make_mqtt_client(solve_queue=q)
        msg = _make_trigger_msg()

        with patch("mimirheim.io.mqtt_client.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 103.0]
            client._on_message(None, None, msg)
            client._on_message(None, None, msg)

        assert q.qsize() == 1

    def test_second_trigger_after_debounce_window_is_allowed(self) -> None:
        """Two triggers 6 s apart must both produce queued solves."""
        q: queue.Queue = queue.Queue()
        client = _make_mqtt_client(solve_queue=q)
        msg = _make_trigger_msg()

        with patch("mimirheim.io.mqtt_client.time") as mock_time:
            mock_time.monotonic.side_effect = [100.0, 106.0]
            client._on_message(None, None, msg)
            client._on_message(None, None, msg)

        assert q.qsize() == 2

    def test_retained_trigger_is_not_debounced_but_still_dropped(self) -> None:
        """A retained message on the trigger topic is dropped before debounce; debounce state unchanged."""
        q: queue.Queue = queue.Queue()
        client = _make_mqtt_client(solve_queue=q)

        retained_msg = _make_trigger_msg(retain=True)
        normal_msg = _make_trigger_msg(retain=False)

        with patch("mimirheim.io.mqtt_client.time") as mock_time:
            # Retained message does not consume a monotonic call.
            mock_time.monotonic.side_effect = [100.0]
            client._on_message(None, None, retained_msg)  # dropped immediately
            client._on_message(None, None, normal_msg)    # should proceed

        assert q.qsize() == 1
