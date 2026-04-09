"""Unit tests for PvFetcherDaemon._run_cycle in pv_fetcher.__main__.

Tests verify:
- A RatelimitError on the first array aborts the cycle (no further arrays fetched).
- A RatelimitError suppresses the mimirheim trigger even when signal_mimir is True.
- _run_cycle returns the reset_at datetime when rate-limited, None otherwise.
- A generic FetchError for one array still allows subsequent arrays to proceed.
- After a ratelimit, triggers received before reset_at are suppressed by the
  base class (tested via HelperDaemon._on_message integration).
"""

import inspect
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from helper_common.cycle import CycleResult
from pv_fetcher.__main__ import PvFetcherDaemon
from pv_fetcher.config import (
    ArrayConfig,
    ConfidenceDecayConfig,
    ForecastSolarApiConfig,
    MqttConfig,
    PvFetcherConfig,
)
from pv_fetcher.fetcher import FetchError, RatelimitError


def _make_config(signal_mimir: bool = False) -> PvFetcherConfig:
    return PvFetcherConfig(
        mqtt=MqttConfig(host="localhost", client_id="test"),
        trigger_topic="test/trigger",
        forecast_solar=ForecastSolarApiConfig(),
        arrays={
            "array_a": ArrayConfig(
                output_topic="mimir/input/pv_a",
                latitude=52.0,
                longitude=4.0,
                declination=30,
                azimuth=0,
                peak_power_kwp=5.0,
            ),
            "array_b": ArrayConfig(
                output_topic="mimir/input/pv_b",
                latitude=52.0,
                longitude=4.0,
                declination=30,
                azimuth=0,
                peak_power_kwp=3.0,
            ),
        },
        confidence_decay=ConfidenceDecayConfig(),
        signal_mimir=signal_mimir,
        mimir_trigger_topic="mimir/input/trigger",
    )


def _make_daemon(signal_mimir: bool = False) -> PvFetcherDaemon:
    return PvFetcherDaemon(_make_config(signal_mimir=signal_mimir))


def _ratelimit_error() -> RatelimitError:
    reset_time = datetime(2026, 3, 31, 16, 0, 0, tzinfo=timezone.utc)
    return RatelimitError("rate limit exceeded", reset_at=reset_time)


def _close_coro_and_return(result):
    def _runner(coro):
        if inspect.iscoroutine(coro):
            coro.close()
        return result

    return _runner


def _close_coro_and_raise(exc: Exception):
    def _runner(coro):
        if inspect.iscoroutine(coro):
            coro.close()
        raise exc

    return _runner


def test_ratelimit_aborts_remaining_arrays() -> None:
    """When the first array hits the rate limit the second must not be fetched."""
    daemon = _make_daemon()
    client = MagicMock()

    fetch_call_count = 0

    def _fake_fetch(*args, **kwargs):
        nonlocal fetch_call_count
        fetch_call_count += 1
        raise _ratelimit_error()

    with patch("pv_fetcher.__main__.fetch_array", side_effect=_fake_fetch):
        with patch(
            "pv_fetcher.__main__.asyncio.run",
            side_effect=lambda coro: (coro.close(), _fake_fetch())[1],
        ):
            daemon._run_cycle(client)

    # Only one asyncio.run call — second array was never attempted.
    assert fetch_call_count == 1


def test_ratelimit_suppresses_hioo_trigger() -> None:
    """A ratelimit during the fetch cycle must not fire the mimirheim trigger."""
    daemon = _make_daemon(signal_mimir=True)
    client = MagicMock()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_raise(_ratelimit_error()),
    ):
        daemon._run_cycle(client)

    # No publish calls at all — neither array payload nor mimirheim trigger.
    client.publish.assert_not_called()


def test_run_cycle_returns_none_on_success() -> None:
    """_run_cycle must return None when at least one array succeeds."""
    daemon = _make_daemon()
    client = MagicMock()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return({}),
    ):
        with patch("pv_fetcher.__main__.apply_confidence", return_value=[]):
            with patch("pv_fetcher.__main__.publish_array"):
                result = daemon._run_cycle(client)

    assert result is None


def test_run_cycle_returns_reset_at_on_ratelimit() -> None:
    """_run_cycle must return reset_at when rate-limited."""
    daemon = _make_daemon()
    client = MagicMock()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_raise(_ratelimit_error()),
    ):
        result = daemon._run_cycle(client)

    assert result is not None
    assert isinstance(result, CycleResult)
    assert result.suppress_until == datetime(2026, 3, 31, 16, 0, 0, tzinfo=timezone.utc)


def test_generic_fetch_error_continues_to_next_array() -> None:
    """A non-ratelimit FetchError for one array must not abort the other."""
    daemon = _make_daemon()
    client = MagicMock()

    call_count = 0

    def _side_effect(coro):
        if inspect.iscoroutine(coro):
            coro.close()
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FetchError("connection timeout")
        return {}

    with patch("pv_fetcher.__main__.asyncio.run", side_effect=_side_effect):
        with patch("pv_fetcher.__main__.apply_confidence", return_value=[]):
            with patch("pv_fetcher.__main__.publish_array"):
                daemon._run_cycle(client)

    assert call_count == 2


def test_on_message_ignores_triggers_before_reset_at() -> None:
    """After a ratelimit, triggers received before reset_at must be suppressed."""
    daemon = _make_daemon()

    reset_time = datetime(2026, 3, 31, 16, 0, 0, tzinfo=timezone.utc)
    cycle_call_count = 0

    def _fake_cycle(client):
        nonlocal cycle_call_count
        cycle_call_count += 1
        return CycleResult(suppress_until=reset_time)  # signal rate-limit on first call

    daemon._run_cycle = _fake_cycle  # type: ignore[method-assign]

    msg = MagicMock()
    msg.topic = "test/trigger"
    msg.retain = False
    msg.payload = b""

    before_reset = datetime(2026, 3, 31, 15, 59, 0, tzinfo=timezone.utc)

    with patch("helper_common.daemon.time") as mock_time:
        mock_time.monotonic.side_effect = [100.0, 106.0]
        with patch("helper_common.daemon.datetime") as mock_dt:
            mock_dt.now.return_value = before_reset
            # First trigger fires the cycle and sets the rate-limit.
            daemon._on_message(daemon._client, None, msg)
            # Second trigger arrives before reset_at — must be suppressed.
            daemon._on_message(daemon._client, None, msg)

    assert cycle_call_count == 1


