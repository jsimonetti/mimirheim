"""Unit tests for baseload_static.publisher.

Covers MQTT publish behaviour for the base load forecast.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from baseload_static.publisher import publish_forecast


_STEPS = [
    {"ts": "2026-03-31T14:00:00+00:00", "kw": 0.42},
    {"ts": "2026-03-31T15:00:00+00:00", "kw": 0.38},
]


@pytest.fixture
def mqtt_client() -> MagicMock:
    return MagicMock()


class TestPublishForecast:
    def test_publishes_json_array_retained(self, mqtt_client: MagicMock) -> None:
        publish_forecast(mqtt_client, "mimir/input/base", _STEPS, signal_mimir=False)
        mqtt_client.publish.assert_called_once()
        topic, payload, *_ = mqtt_client.publish.call_args.args
        assert topic == "mimir/input/base"
        assert json.loads(payload) == _STEPS

    def test_publishes_with_retain_and_qos1(self, mqtt_client: MagicMock) -> None:
        publish_forecast(mqtt_client, "mimir/input/base", _STEPS, signal_mimir=False)
        kwargs = mqtt_client.publish.call_args.kwargs
        assert kwargs.get("retain") is True
        assert kwargs.get("qos") == 1

    def test_signals_hioo_when_enabled(self, mqtt_client: MagicMock) -> None:
        publish_forecast(
            mqtt_client,
            "mimir/input/base",
            _STEPS,
            signal_mimir=True,
            mimir_trigger_topic="mimir/input/trigger",
        )
        assert mqtt_client.publish.call_count == 2
        trigger_call = mqtt_client.publish.call_args_list[1]
        assert trigger_call.args[0] == "mimir/input/trigger"
        assert trigger_call.kwargs.get("retain") is False

    def test_does_not_signal_mimir_when_disabled(self, mqtt_client: MagicMock) -> None:
        publish_forecast(mqtt_client, "mimir/input/base", _STEPS, signal_mimir=False)
        assert mqtt_client.publish.call_count == 1

    def test_raises_if_signal_mimir_without_trigger_topic(
        self, mqtt_client: MagicMock
    ) -> None:
        with pytest.raises(ValueError, match="mimir_trigger_topic"):
            publish_forecast(mqtt_client, "mimir/input/base", _STEPS, signal_mimir=True)
