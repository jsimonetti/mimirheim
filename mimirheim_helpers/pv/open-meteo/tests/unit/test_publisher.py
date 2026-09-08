"""Unit tests for pv_openmeteo.publisher.

Tests verify:
- The forecast is published to the array's output topic, retained, at QoS 1.
- The payload is JSON in the mimirheim forecast format.
- The solve trigger is empty and not retained.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt

from pv_openmeteo.publisher import publish_forecast, publish_trigger


def _steps() -> list[dict]:
    now = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    return [
        {"ts": (now + timedelta(minutes=15 * i)).isoformat(), "kw": float(i), "confidence": 0.9}
        for i in range(3)
    ]


def _mqtt_client() -> MagicMock:
    """Return a mock paho client whose publish() reports success."""
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    return client


def test_forecast_is_published_retained_at_qos1() -> None:
    client = _mqtt_client()
    publish_forecast(client, "mimir/input/pv/roof/forecast", _steps())
    client.publish.assert_called_once()
    args, kwargs = client.publish.call_args
    assert args[0] == "mimir/input/pv/roof/forecast"
    assert kwargs["retain"] is True
    assert kwargs["qos"] == 1


def test_payload_is_mimirheim_forecast_json() -> None:
    client = _mqtt_client()
    publish_forecast(client, "mimir/input/pv/roof/forecast", _steps())
    payload = json.loads(client.publish.call_args.args[1])
    assert len(payload) == 3
    for step in payload:
        assert set(step) == {"ts", "kw", "confidence"}


def test_trigger_is_empty_and_not_retained() -> None:
    client = _mqtt_client()
    publish_trigger(client, "mimir/input/trigger")
    args, kwargs = client.publish.call_args
    assert args[0] == "mimir/input/trigger"
    assert args[1] == b""
    assert kwargs["retain"] is False
