"""Unit tests for nordpool.publisher.

Covers MQTT publish behaviour including retention and mimirheim signalling.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, call

import pytest

from nordpool.publisher import _normalise_zeros, publish_prices


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


class TestNormaliseZeros:
    def test_negative_zero_becomes_integer_zero(self) -> None:
        """A -0.0 float value is replaced with the integer 0."""
        result = _normalise_zeros([{"export_eur_per_kwh": -0.0}])
        assert result[0]["export_eur_per_kwh"] == 0
        assert isinstance(result[0]["export_eur_per_kwh"], int)

    def test_positive_zero_float_becomes_integer_zero(self) -> None:
        """A 0.0 float value is replaced with the integer 0."""
        result = _normalise_zeros([{"export_eur_per_kwh": 0.0}])
        assert result[0]["export_eur_per_kwh"] == 0
        assert isinstance(result[0]["export_eur_per_kwh"], int)

    def test_non_zero_floats_are_unchanged(self) -> None:
        """Non-zero float values pass through unmodified."""
        result = _normalise_zeros([{"import_eur_per_kwh": 0.2418, "export_eur_per_kwh": 0.1952}])
        assert result[0]["import_eur_per_kwh"] == 0.2418
        assert result[0]["export_eur_per_kwh"] == 0.1952

    def test_non_float_values_are_unchanged(self) -> None:
        """Non-float values (strings, ints, etc.) are not affected."""
        result = _normalise_zeros([{"ts": "2026-01-01T00:00:00+00:00", "confidence": 1.0}])
        assert result[0]["ts"] == "2026-01-01T00:00:00+00:00"
        # confidence 1.0 is non-zero, so it stays as a float
        assert result[0]["confidence"] == 1.0

    def test_original_dicts_are_not_mutated(self) -> None:
        """The input list and its dicts are not modified in place."""
        original = [{"export_eur_per_kwh": -0.0}]
        _ = _normalise_zeros(original)
        # The original dict value should still be -0.0
        import math
        assert math.copysign(1.0, original[0]["export_eur_per_kwh"]) == -1.0

    def test_normalised_zeros_serialise_as_bare_zero(self) -> None:
        """-0.0 and 0.0 both serialise to '0' (not '-0.0' or '0.0') after normalisation."""
        import json
        result = _normalise_zeros([{"import_eur_per_kwh": 0.0, "export_eur_per_kwh": -0.0}])
        serialised = json.dumps(result[0])
        assert "-0" not in serialised
        assert "0.0" not in serialised
