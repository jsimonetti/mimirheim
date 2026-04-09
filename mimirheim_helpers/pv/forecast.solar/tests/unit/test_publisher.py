"""Unit tests for pv_fetcher.publisher.

Tests verify:
- publish_array publishes to the correct output_topic with retain=True, qos=1.
- The payload is valid JSON matching the mimirheim PV forecast format.
- signal_mimir=True causes a second publish to mimir_trigger_topic.
- signal_mimir=False does not publish to mimir_trigger_topic.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from pv_fetcher.publisher import publish_array


def _steps() -> list[dict]:
    now = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
    return [
        {"ts": (now + timedelta(hours=i)).isoformat(), "kw": float(i), "confidence": 0.9}
        for i in range(3)
    ]


def test_publish_array_retained_qos1() -> None:
    client = MagicMock()
    publish_array(client, "mimir/input/pv", _steps(), signal_mimir=False)
    client.publish.assert_called_once()
    _, kwargs = client.publish.call_args
    assert kwargs["retain"] is True
    assert kwargs["qos"] == 1


def test_publish_array_topic() -> None:
    client = MagicMock()
    publish_array(client, "mimir/input/pv", _steps(), signal_mimir=False)
    args, _ = client.publish.call_args
    assert args[0] == "mimir/input/pv"


def test_publish_array_payload_is_valid_json() -> None:
    client = MagicMock()
    publish_array(client, "mimir/input/pv", _steps(), signal_mimir=False)
    args, _ = client.publish.call_args
    payload = json.loads(args[1])
    assert isinstance(payload, list)
    assert len(payload) == 3
    for step in payload:
        assert "ts" in step
        assert "kw" in step
        assert "confidence" in step


def test_signal_mimir_false_no_trigger_publish() -> None:
    client = MagicMock()
    publish_array(client, "mimir/input/pv", _steps(), signal_mimir=False)
    assert client.publish.call_count == 1


def test_signal_mimir_true_publishes_trigger() -> None:
    client = MagicMock()
    publish_array(
        client, "mimir/input/pv", _steps(),
        signal_mimir=True,
        mimir_trigger_topic="mimir/input/trigger",
    )
    assert client.publish.call_count == 2
    topics = [c.args[0] for c in client.publish.call_args_list]
    assert "mimir/input/trigger" in topics


def test_signal_mimir_trigger_is_not_retained() -> None:
    client = MagicMock()
    publish_array(
        client, "mimir/input/pv", _steps(),
        signal_mimir=True,
        mimir_trigger_topic="mimir/input/trigger",
    )
    trigger_call = next(
        c for c in client.publish.call_args_list if c.args[0] == "mimir/input/trigger"
    )
    assert trigger_call.kwargs.get("retain") is False
