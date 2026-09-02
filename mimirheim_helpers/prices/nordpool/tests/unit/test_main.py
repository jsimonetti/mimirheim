"""Unit tests for NordpoolDaemon._run_cycle in nordpool.__main__.

Covers the horizon_hours calculation on the returned CycleResult: it must
reflect actual hours of published coverage, not the raw number of price steps.
A "quarter_hourly" cycle publishes four steps per hour, so the step count must
be scaled by 0.25 to report true hours, while an "hourly" cycle publishes one
step per hour and reports the count unchanged.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from nordpool.__main__ import NordpoolDaemon
from nordpool.config import MqttConfig, NordpoolApiConfig, NordpoolConfig
from nordpool.fetcher import FetchError


def _make_daemon(price_interval: str) -> NordpoolDaemon:
    config = NordpoolConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        trigger_topic="mimir/input/tools/prices/trigger",
        nordpool=NordpoolApiConfig(area="NL", price_interval=price_interval),
    )
    return NordpoolDaemon(config)


def _run_cycle_with_n_steps(daemon: NordpoolDaemon, n_steps: int):
    client = MagicMock()
    fake_steps = [{"ts": f"step-{i}"} for i in range(n_steps)]

    with patch(
        "nordpool.__main__.fetch_prices", AsyncMock(return_value=fake_steps)
    ) as fetch:
        with patch("nordpool.__main__.publish_prices"):
            return daemon._run_cycle(client), fetch


def test_horizon_hours_hourly_equals_step_count() -> None:
    """In hourly mode each step is one hour, so horizon_hours == step count."""
    daemon = _make_daemon("hourly")
    result, _ = _run_cycle_with_n_steps(daemon, 24)

    assert result is not None
    assert result.horizon_hours == 24


def test_horizon_hours_quarter_hourly_is_scaled_by_quarter() -> None:
    """In quarter_hourly mode each step is 15 minutes, so horizon_hours must
    be a quarter of the raw step count — not the raw count itself."""
    daemon = _make_daemon("quarter_hourly")
    result, _ = _run_cycle_with_n_steps(daemon, 96)

    assert result is not None
    assert result.horizon_hours == 24


def test_price_interval_is_passed_to_the_fetcher() -> None:
    """The configured interval must reach fetch_prices, or aggregation never runs."""
    daemon = _make_daemon("hourly")
    _, fetch = _run_cycle_with_n_steps(daemon, 24)

    assert fetch.await_args.kwargs["price_interval"] == "hourly"


def test_fetch_error_returns_no_cycle_result() -> None:
    """A failed fetch leaves the retained payload alone and reports no horizon."""
    daemon = _make_daemon("hourly")
    client = MagicMock()

    with patch("nordpool.__main__.fetch_prices", AsyncMock(side_effect=FetchError("x"))):
        with patch("nordpool.__main__.publish_prices") as publish:
            result = daemon._run_cycle(client)

    assert result is None
    publish.assert_not_called()
