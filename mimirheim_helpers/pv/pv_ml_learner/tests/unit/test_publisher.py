"""Tests for pv_ml_learner.publisher."""

from __future__ import annotations

import datetime
import json
from unittest.mock import MagicMock, call

import pytest

from pv_ml_learner.predictor import ForecastStep


def _steps(n: int = 3) -> list[ForecastStep]:
    base = datetime.datetime(2026, 4, 2, 10, 0, 0, tzinfo=datetime.timezone.utc)
    return [
        ForecastStep(
            ts=base + datetime.timedelta(hours=i),
            kw=1.0 + i * 0.1,
            confidence=0.85 - i * 0.01,
        )
        for i in range(n)
    ]


class TestPayloadFormat:
    def test_json_contains_exactly_ts_kw_confidence(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        publish_forecast(client, "pv/forecast", _steps(3), signal_mimir=False)

        payload_str = client.publish.call_args[0][1]
        payload = json.loads(payload_str)

        assert isinstance(payload, list)
        assert len(payload) == 3
        for item in payload:
            assert set(item.keys()) == {"ts", "kw", "confidence"}

    def test_ts_is_utc_iso8601(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        publish_forecast(client, "pv/forecast", _steps(1), signal_mimir=False)

        payload = json.loads(client.publish.call_args[0][1])
        ts_str: str = payload[0]["ts"]

        # Must be parseable as UTC-aware ISO 8601 with explicit offset
        parsed = datetime.datetime.fromisoformat(ts_str)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == datetime.timedelta(0)

    def test_published_retained_qos1(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        publish_forecast(client, "pv/forecast", _steps(3), signal_mimir=False)

        args, kwargs = client.publish.call_args
        assert args[0] == "pv/forecast"
        assert kwargs.get("retain") is True
        assert kwargs.get("qos") == 1


class TestSignalHioo:
    def test_signal_mimir_true_without_topic_raises_before_publish(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        with pytest.raises(ValueError):
            publish_forecast(
                client,
                "pv/forecast",
                _steps(1),
                signal_mimir=True,
                mimir_trigger_topic=None,
            )
        # No publish call must have been made
        client.publish.assert_not_called()

    def test_signal_mimir_publishes_trigger(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        publish_forecast(
            client,
            "pv/forecast",
            _steps(2),
            signal_mimir=True,
            mimir_trigger_topic="mimir/solve/trigger",
        )

        # Should have been called twice: once for forecast, once for trigger
        assert client.publish.call_count == 2
        trigger_call = client.publish.call_args_list[1]
        assert trigger_call[0][0] == "mimir/solve/trigger"

    def test_signal_mimir_false_publishes_once(self) -> None:
        from pv_ml_learner.publisher import publish_forecast

        client = MagicMock()
        publish_forecast(client, "pv/forecast", _steps(2), signal_mimir=False)
        assert client.publish.call_count == 1
