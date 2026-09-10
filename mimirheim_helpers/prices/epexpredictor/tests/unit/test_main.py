"""Unit tests for EpexPredictorPricesDaemon._run_cycle in epexpredictor_prices.__main__.

Covers the successful publish path, FetchError handling, and the
horizon_hours calculation on the returned CycleResult (scaled by step
duration, same rule as the Nordpool helper).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from epexpredictor_prices.__main__ import EpexPredictorPricesDaemon
from epexpredictor_prices.config import (
    EpexPredictorApiConfig,
    EpexPredictorPricesConfig,
    MqttConfig,
)
from epexpredictor_prices.fetcher import FetchError, FetchResult


def _make_daemon(price_interval: str = "quarter_hourly") -> EpexPredictorPricesDaemon:
    config = EpexPredictorPricesConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        trigger_topic="mimir/input/tools/prices/trigger",
        epexpredictor=EpexPredictorApiConfig(area="NL", price_interval=price_interval),
    )
    return EpexPredictorPricesDaemon(config)


def _fake_result(n_steps: int) -> FetchResult:
    now = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
    steps = [(now, 0.10 + i * 0.01) for i in range(n_steps)]
    return FetchResult(steps=steps, known_until=now)


class TestRunCycleSuccess:
    def test_publishes_steps_and_returns_cycle_result(self) -> None:
        daemon = _make_daemon("hourly")
        client = MagicMock()

        with patch(
            "epexpredictor_prices.__main__.fetch_raw_prices",
            return_value=_fake_result(24),
        ):
            with patch("epexpredictor_prices.__main__.publish_prices") as publish:
                result = daemon._run_cycle(client)

        assert result is not None
        assert result.horizon_hours == 24
        publish.assert_called_once()

    def test_horizon_hours_quarter_hourly_scaled_by_quarter(self) -> None:
        daemon = _make_daemon("quarter_hourly")
        client = MagicMock()

        with patch(
            "epexpredictor_prices.__main__.fetch_raw_prices",
            return_value=_fake_result(96),
        ):
            with patch("epexpredictor_prices.__main__.publish_prices"):
                result = daemon._run_cycle(client)

        assert result is not None
        assert result.horizon_hours == 24


class TestRunCycleFetchError:
    def test_fetch_error_returns_none_and_does_not_publish(self) -> None:
        daemon = _make_daemon("hourly")
        client = MagicMock()

        with patch(
            "epexpredictor_prices.__main__.fetch_raw_prices",
            side_effect=FetchError("network failure"),
        ):
            with patch("epexpredictor_prices.__main__.publish_prices") as publish:
                result = daemon._run_cycle(client)

        assert result is None
        publish.assert_not_called()


class TestSignalMimir:
    def test_signal_mimir_true_triggers_publish_prices_with_signal(self) -> None:
        config = EpexPredictorPricesConfig(
            mqtt=MqttConfig(host="localhost", client_id="test"),
            trigger_topic="mimir/input/tools/prices/trigger",
            epexpredictor=EpexPredictorApiConfig(area="NL"),
            signal_mimir=True,
            mimir_trigger_topic="mimir/input/trigger",
        )
        daemon = EpexPredictorPricesDaemon(config)
        client = MagicMock()

        with patch(
            "epexpredictor_prices.__main__.fetch_raw_prices",
            return_value=_fake_result(4),
        ):
            with patch("epexpredictor_prices.__main__.publish_prices") as publish:
                daemon._run_cycle(client)

        assert publish.call_args.kwargs["signal_mimir"] is True
        assert publish.call_args.kwargs["mimir_trigger_topic"] == "mimir/input/trigger"
