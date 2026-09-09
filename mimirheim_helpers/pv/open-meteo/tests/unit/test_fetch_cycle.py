"""Unit tests for PvOpenMeteoDaemon._run_cycle in pv_openmeteo.__main__.

Tests verify:
- Each array is published to its own topic, and the reported horizon measures
  forward from the fetch time rather than counting retained history.
- The reported horizon is the shortest array's, since the solve cannot reach
  past its weakest input.
- A rate-limit response aborts the cycle before the remaining arrays and
  returns the reset time, without firing the mimirheim trigger.
- A fetch failure on one array still lets the others publish.
- Steps outside the retention window are dropped, and an array left with no
  steps is skipped rather than published as an empty payload.
- The mimirheim trigger fires once per cycle, only when something was published.
"""

import inspect
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest

from helper_common.cycle import CycleResult
from pv_openmeteo.__main__ import PvOpenMeteoDaemon
from pv_openmeteo.config import PvOpenMeteoConfig
from pv_openmeteo.fetcher import FetchError, RatelimitError

_NOW = datetime.now(tz=timezone.utc)


def _make_config(signal_mimir: bool = False) -> PvOpenMeteoConfig:
    return PvOpenMeteoConfig.model_validate({
        "mqtt": {"host": "localhost", "client_id": "test"},
        "trigger_topic": "test/trigger",
        "site": {"latitude": 52.0, "longitude": 5.0},
        "arrays": {
            "solaredge": {
                "inverter_kwp": 5.0,
                "planes": [
                    {"declination": 25, "azimuth": -90, "peak_power_kwp": 1.04},
                    {"declination": 25, "azimuth": 90, "peak_power_kwp": 1.04},
                ],
            },
            "enphase": {
                "planes": [
                    {"declination": 45, "azimuth": 90, "peak_power_kwp": 4.35,
                     "inverter_kwp": 3.25},
                ],
            },
        },
        "signal_mimir": signal_mimir,
    })


def _make_daemon(signal_mimir: bool = False) -> PvOpenMeteoDaemon:
    return PvOpenMeteoDaemon(_make_config(signal_mimir=signal_mimir))


def _mqtt_client() -> MagicMock:
    """Return a mock paho client whose publish() reports success."""
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    return client


def _series(hours: int = 2) -> dict[datetime, float]:
    """Return a quarter-hourly series starting now."""
    return {_NOW + timedelta(minutes=15 * i): float(i) for i in range(hours * 4)}


def _runner(*results):
    """Return an asyncio.run stand-in yielding or raising the given results in turn."""
    remaining = list(results)

    def _run(coro):
        if inspect.iscoroutine(coro):
            coro.close()
        outcome = remaining.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return _run


def test_every_array_is_published_to_its_own_topic() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch("pv_openmeteo.__main__.asyncio.run", side_effect=_runner(_series(), _series())):
        result = daemon._run_cycle(client)

    topics = [call.args[0] for call in client.publish.call_args_list]
    assert topics == [
        "mimir/input/pv/solaredge/forecast",
        "mimir/input/pv/enphase/forecast",
    ]
    assert isinstance(result, CycleResult)
    # Eight quarter-hour steps starting at now reach 1h45m ahead, not 2 h:
    # the horizon is the distance to the last step, not the step count.
    assert result.horizon_hours == pytest.approx(1.75, abs=0.01)


def test_published_payload_is_the_mimirheim_step_format() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch("pv_openmeteo.__main__.asyncio.run", side_effect=_runner(_series(1), _series(1))):
        daemon._run_cycle(client)

    payload = json.loads(client.publish.call_args_list[0].args[1])
    assert len(payload) == 4
    assert set(payload[0]) == {"ts", "kw", "confidence"}
    assert payload[0]["confidence"] == 0.9


def test_ratelimit_aborts_the_remaining_arrays() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()
    reset_at = _NOW + timedelta(minutes=15)

    run = MagicMock(side_effect=_runner(RatelimitError("slow down", reset_at=reset_at)))
    with patch("pv_openmeteo.__main__.asyncio.run", run):
        result = daemon._run_cycle(client)

    assert run.call_count == 1
    assert result.suppress_until == reset_at
    client.publish.assert_not_called()


def test_ratelimit_does_not_fire_the_mimirheim_trigger() -> None:
    daemon = _make_daemon(signal_mimir=True)
    client = _mqtt_client()

    with patch(
        "pv_openmeteo.__main__.asyncio.run",
        side_effect=_runner(RatelimitError("slow down", reset_at=_NOW)),
    ):
        daemon._run_cycle(client)

    client.publish.assert_not_called()


def test_one_failing_array_does_not_stop_the_others() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()

    with patch(
        "pv_openmeteo.__main__.asyncio.run",
        side_effect=_runner(FetchError("boom"), _series()),
    ):
        result = daemon._run_cycle(client)

    topics = [call.args[0] for call in client.publish.call_args_list]
    assert topics == ["mimir/input/pv/enphase/forecast"]
    assert result.horizon_hours == pytest.approx(1.75, abs=0.01)


def test_reported_horizon_is_the_shortest_array_and_excludes_history() -> None:
    """Retained history is not coverage, and the solve stops at its weakest input."""
    daemon = _make_daemon()
    client = _mqtt_client()
    long_series = {_NOW - timedelta(minutes=30): 1.0}
    long_series.update({_NOW + timedelta(minutes=15 * i): float(i) for i in range(1, 17)})
    short_series = {_NOW + timedelta(minutes=15 * i): float(i) for i in range(1, 5)}

    with patch(
        "pv_openmeteo.__main__.asyncio.run",
        side_effect=_runner(long_series, short_series),
    ):
        result = daemon._run_cycle(client)

    # Longest array reaches 4 h ahead and carries 30 min of history; shortest
    # reaches 1 h. Neither the history nor the longer array may inflate this.
    assert result.horizon_hours == pytest.approx(1.0, abs=0.01)


def test_elapsed_steps_are_trimmed_to_the_retention_window() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()
    stale = {
        _NOW - timedelta(hours=5): 1.0,
        _NOW - timedelta(minutes=30): 2.0,
        _NOW + timedelta(minutes=15): 3.0,
    }

    with patch("pv_openmeteo.__main__.asyncio.run", side_effect=_runner(stale, stale)):
        daemon._run_cycle(client)

    payload = json.loads(client.publish.call_args_list[0].args[1])
    assert [step["kw"] for step in payload] == [2.0, 3.0]


def test_array_with_no_steps_in_the_window_is_skipped() -> None:
    daemon = _make_daemon()
    client = _mqtt_client()
    only_stale = {_NOW - timedelta(days=1): 1.0}

    with patch(
        "pv_openmeteo.__main__.asyncio.run",
        side_effect=_runner(only_stale, _series()),
    ):
        daemon._run_cycle(client)

    topics = [call.args[0] for call in client.publish.call_args_list]
    assert topics == ["mimir/input/pv/enphase/forecast"]


def test_all_zero_forecast_is_still_published() -> None:
    """A forecast of no production is information the solver needs."""
    daemon = _make_daemon()
    client = _mqtt_client()
    night = {_NOW + timedelta(minutes=15 * i): 0.0 for i in range(8)}

    with patch("pv_openmeteo.__main__.asyncio.run", side_effect=_runner(night, night)):
        daemon._run_cycle(client)

    assert len(client.publish.call_args_list) == 2


def test_mimirheim_trigger_fires_once_after_a_successful_cycle() -> None:
    daemon = _make_daemon(signal_mimir=True)
    client = _mqtt_client()

    with patch("pv_openmeteo.__main__.asyncio.run", side_effect=_runner(_series(), _series())):
        daemon._run_cycle(client)

    topics = [call.args[0] for call in client.publish.call_args_list]
    assert topics.count("mimir/input/trigger") == 1
    assert topics[-1] == "mimir/input/trigger"


def test_no_trigger_when_nothing_was_published() -> None:
    daemon = _make_daemon(signal_mimir=True)
    client = _mqtt_client()

    with patch(
        "pv_openmeteo.__main__.asyncio.run",
        side_effect=_runner(FetchError("a"), FetchError("b")),
    ):
        result = daemon._run_cycle(client)

    client.publish.assert_not_called()
    assert result is None
