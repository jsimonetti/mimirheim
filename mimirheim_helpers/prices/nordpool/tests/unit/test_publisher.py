"""Unit tests for nordpool.publisher.

Covers MQTT publish behaviour including retention and mimirheim signalling.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

from nordpool.publisher import publish_prices


_STEPS = [
    {
        "ts": "2026-03-30T14:00:00+00:00",
        "import_eur_per_kwh": 0.12,
        "export_eur_per_kwh": 0.10,
        "confidence": 1.0,
    },
    {
        "ts": "2026-03-30T15:00:00+00:00",
        "import_eur_per_kwh": 0.11,
        "export_eur_per_kwh": 0.09,
        "confidence": 1.0,
    },
]


@pytest.fixture
def mqtt_client() -> MagicMock:
    """Return a mock paho MQTT client."""
    return MagicMock()


class TestPublishPrices:
    def test_publishes_json_array_retained(self, mqtt_client: MagicMock) -> None:
        publish_prices(mqtt_client, "mimir/input/prices", _STEPS, signal_mimir=False)
        mqtt_client.publish.assert_called_once()
        topic, payload, *_ = mqtt_client.publish.call_args.args
        assert topic == "mimir/input/prices"
        assert json.loads(payload) == _STEPS

    def test_publishes_with_retain_and_qos1(self, mqtt_client: MagicMock) -> None:
        publish_prices(mqtt_client, "mimir/input/prices", _STEPS, signal_mimir=False)
        kwargs = mqtt_client.publish.call_args.kwargs
        assert kwargs.get("retain") is True
        assert kwargs.get("qos") == 1

    def test_empty_steps_publishes_empty_array(self, mqtt_client: MagicMock) -> None:
        publish_prices(mqtt_client, "mimir/input/prices", [], signal_mimir=False)
        _, payload, *_ = mqtt_client.publish.call_args.args
        assert json.loads(payload) == []

    def test_signals_hioo_when_configured(self, mqtt_client: MagicMock) -> None:
        publish_prices(
            mqtt_client,
            "mimir/input/prices",
            _STEPS,
            signal_mimir=True,
            mimir_trigger_topic="mimir/input/trigger",
        )
        assert mqtt_client.publish.call_count == 2
        trigger_call = mqtt_client.publish.call_args_list[1]
        assert trigger_call.args[0] == "mimir/input/trigger"
        assert trigger_call.kwargs.get("retain") is False

    def test_does_not_signal_mimir_when_disabled(self, mqtt_client: MagicMock) -> None:
        publish_prices(mqtt_client, "mimir/input/prices", _STEPS, signal_mimir=False)
        assert mqtt_client.publish.call_count == 1

    def test_raises_if_signal_mimir_without_trigger_topic(
        self, mqtt_client: MagicMock
    ) -> None:
        with pytest.raises(ValueError, match="mimir_trigger_topic"):
            publish_prices(
                mqtt_client, "mimir/input/prices", _STEPS, signal_mimir=True
            )
