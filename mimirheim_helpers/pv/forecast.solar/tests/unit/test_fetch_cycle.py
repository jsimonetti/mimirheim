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
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt

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


def _mqtt_client() -> MagicMock:
    """Return a mock paho client whose publish() reports success.

    publish_checked inspects the rc of the MQTTMessageInfo that publish()
    returns. A bare MagicMock yields a mock attribute there, which is exactly
    why no test in this suite could ever have exercised a publish failure.
    """
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    return client


def test_ratelimit_aborts_remaining_arrays() -> None:
    """When the first array hits the rate limit the second must not be fetched."""
    daemon = _make_daemon()
    client = _mqtt_client()

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
    client = _mqtt_client()

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
    client = _mqtt_client()

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
    client = _mqtt_client()

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
    client = _mqtt_client()

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




# ---------------------------------------------------------------------------
# An all-zero forecast is data, not an absence of data
# ---------------------------------------------------------------------------


def _zero_watts() -> dict:
    """A forecast.solar response whose every value is zero."""
    return {
        datetime(2026, 3, 31, h, 0, 0, tzinfo=timezone.utc): 0
        for h in range(0, 24)
    }


def _nonzero_watts() -> dict:
    """A forecast.solar response with a daylight curve."""
    watts = {
        datetime(2026, 3, 31, h, 0, 0, tzinfo=timezone.utc): 0
        for h in range(0, 24)
    }
    for h in (10, 11, 12, 13):
        watts[datetime(2026, 3, 31, h, 0, 0, tzinfo=timezone.utc)] = 3000
    return watts


def test_all_zero_forecast_is_published() -> None:
    """An all-zero curve must reach the topic.

    The branch logged "publishing %d steps, all zero kW" and then continued,
    so it published nothing. The previous non-zero forecast stayed retained,
    which left the topic advertising PV production that was not coming.
    """
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return(_zero_watts()),
    ):
        daemon._run_cycle(client)

    published_topics = [call.args[0] for call in client.publish.call_args_list]
    assert "mimir/input/pv_a" in published_topics
    assert "mimir/input/pv_b" in published_topics


def test_all_zero_forecast_payload_is_all_zero() -> None:
    """The published payload must be the zero curve, not a stale one."""
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return(_zero_watts()),
    ):
        daemon._run_cycle(client)

    payload = json.loads(client.publish.call_args_list[0].args[1])
    assert payload, "an all-zero forecast must not publish an empty list"
    assert all(step["kw"] == 0 for step in payload)


def test_all_zero_forecast_signals_mimir_when_configured() -> None:
    """Publishing a forecast means the solver has something new to act on."""
    daemon = _make_daemon(signal_mimir=True)
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return(_zero_watts()),
    ):
        daemon._run_cycle(client)

    published_topics = [call.args[0] for call in client.publish.call_args_list]
    assert "mimir/input/trigger" in published_topics


def test_all_zero_forecast_reports_a_horizon() -> None:
    """A published forecast has a horizon; CycleResult must say so."""
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return(_zero_watts()),
    ):
        result = daemon._run_cycle(client)

    assert isinstance(result, CycleResult)
    assert result.horizon_hours is not None
    assert result.horizon_hours > 0


def test_empty_forecast_is_still_not_published() -> None:
    """No steps at all is a different case and must keep being skipped.

    Publishing "[]" writes an empty retained value that mimirheim's parser
    rejects, which would leave the PV topic invalid rather than stale.
    """
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return({}),
    ):
        daemon._run_cycle(client)

    client.publish.assert_not_called()


def test_nonzero_forecast_is_still_published() -> None:
    """Regression guard on the path that already worked."""
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_fetcher.__main__.asyncio.run",
        side_effect=_close_coro_and_return(_nonzero_watts()),
    ):
        daemon._run_cycle(client)

    published_topics = [call.args[0] for call in client.publish.call_args_list]
    assert "mimir/input/pv_a" in published_topics


def test_all_zero_forecast_log_says_publishing_and_means_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The log line claimed a publish that did not happen."""
    daemon = _make_daemon()
    client = _mqtt_client()

    with caplog.at_level(logging.INFO, logger="pv_fetcher"):
        with patch(
            "pv_fetcher.__main__.asyncio.run",
            side_effect=_close_coro_and_return(_zero_watts()),
        ):
            daemon._run_cycle(client)

    assert "all zero kW" in caplog.text
    assert client.publish.called
