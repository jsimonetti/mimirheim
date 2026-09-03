"""Unit tests for ZonneplanPricesDaemon._run_cycle in zonneplan_prices.__main__.

Covers the horizon_hours calculation on the returned CycleResult: it must
reflect actual hours of published coverage, not the raw number of price
steps. A "quarter_hourly" cycle publishes four steps per hour, so the step
count must be scaled by 0.25 to report true hours — mirroring the equivalent
calculation in the nordpool helper, which takes the same price_interval
setting.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from zonneplan_prices.__main__ import ZonneplanPricesDaemon
from zonneplan_prices.config import (
    MqttConfig,
    ZonneplanApiConfig,
    ZonneplanPricesConfig,
)

_VALID_TOKEN: dict = {
    "access_token": "at",
    "refresh_token": "rt",
    "expires_at": "2999-01-01T00:00:00+00:00",
}


def _make_config(price_interval: str) -> ZonneplanPricesConfig:
    return ZonneplanPricesConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        trigger_topic="mimir/input/tools/prices/trigger",
        zonneplan=ZonneplanApiConfig(
            email="user@example.com",
            price_interval=price_interval,
        ),
    )


def _make_daemon(price_interval: str) -> ZonneplanPricesDaemon:
    return ZonneplanPricesDaemon(_make_config(price_interval))


def _run_cycle_with_n_steps(daemon: ZonneplanPricesDaemon, n_steps: int):
    client = MagicMock()
    fake_steps = [{"ts": f"step-{i}"} for i in range(n_steps)]

    with patch("zonneplan_prices.__main__.load_token", return_value=_VALID_TOKEN):
        with patch("zonneplan_prices.__main__.is_token_valid", return_value=True):
            with patch(
                "zonneplan_prices.__main__.fetch_prices", return_value=fake_steps
            ):
                with patch("zonneplan_prices.__main__.publish_prices"):
                    return daemon._run_cycle(client)


def test_horizon_hours_hourly_equals_step_count() -> None:
    """In hourly mode each step is one hour, so horizon_hours == step count."""
    daemon = _make_daemon("hourly")
    result = _run_cycle_with_n_steps(daemon, 24)

    assert result is not None
    assert result.horizon_hours == 24


def test_horizon_hours_quarter_hourly_is_scaled_by_quarter() -> None:
    """In quarter_hourly mode each step is 15 minutes, so horizon_hours must
    be a quarter of the raw step count — not the raw count itself."""
    daemon = _make_daemon("quarter_hourly")
    result = _run_cycle_with_n_steps(daemon, 96)

    assert result is not None
    assert result.horizon_hours == 24
