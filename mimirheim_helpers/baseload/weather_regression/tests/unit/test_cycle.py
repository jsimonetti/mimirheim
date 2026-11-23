"""Tests for WeatherBaseloadDaemon._run_cycle orchestration.

The recorder and Open-Meteo calls are replaced with in-memory fakes; the
real model runs. Covers the fetch windows, publication, the separate model
statistics topic, and every failure path (nothing published, retained
forecast left unchanged).
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock, patch

import paho.mqtt.client as mqtt
import pytest
from freezegun import freeze_time

from baseload_weather import __main__ as main_mod
from baseload_weather.__main__ import WeatherBaseloadDaemon
from baseload_weather.config import WeatherBaseloadConfig
from helper_common.publish import PublishError
from baseload_weather.model import history_window
from baseload_weather.openmeteo import RateLimitError, WeatherForecastError
from baseload_weather.recorder import RecorderError, RecorderHistory

NOW = datetime(2026, 1, 20, 12, 7, tzinfo=UTC)
START = NOW.replace(minute=0)
H = 3600


def _config(**overrides: Any) -> WeatherBaseloadConfig:
    data: dict[str, Any] = {
        "mqtt": {"host": "localhost", "client_id": "test"},
        "homeassistant": {
            "db_url": "sqlite:////config/home-assistant_v2.db",
            "sum_entities": [{"entity_id": "sensor.load"}],
            "temperature_entity": {"entity_id": "sensor.outdoor"},
        },
        "weather_forecast": {"latitude": 52.0, "longitude": 5.1},
        "model": {"timezone": "Europe/Amsterdam", "lookback_days": 14},
        "horizon_hours": 24,
    }
    data.update(overrides)
    return WeatherBaseloadConfig.model_validate(data)


def _temp(ts: int) -> float:
    return 8.0 + 6.0 * math.sin(ts / 86400 * 0.9)


def _client() -> MagicMock:
    client = MagicMock()
    client.publish.return_value.rc = mqtt.MQTT_ERR_SUCCESS
    return client


DaemonFactory = Callable[[WeatherBaseloadConfig], WeatherBaseloadDaemon]


@pytest.fixture
def daemon_factory(tmp_path: Path) -> DaemonFactory:
    def make(config: WeatherBaseloadConfig) -> WeatherBaseloadDaemon:
        with patch("helper_common.daemon.mqtt.Client"):
            return WeatherBaseloadDaemon(config, tmp_path / "config.yaml")

    return make


def _fake_history(config: WeatherBaseloadConfig, *, start_ts: int, end_ts: int) -> RecorderHistory:
    hours = range(start_ts, end_ts, H)
    return RecorderHistory(
        energy={"sensor.load": {ts: 0.5 + 0.2 * max(0.0, 15.0 - _temp(ts)) for ts in hours}},
        temperature_c={ts: _temp(ts) for ts in hours},
    )


def _fake_forecast(config: Any, *, start: datetime, end: datetime, **_: Any) -> dict[int, float]:
    first = int((start - timedelta(hours=24)).timestamp())
    return {ts: _temp(ts) for ts in range(first, int(end.timestamp()) + H, H)}


@freeze_time(NOW)
def test_cycle_fetches_windows_and_publishes(daemon_factory: DaemonFactory) -> None:
    config = _config()
    daemon = daemon_factory(config)
    client = _client()
    recorder = MagicMock(side_effect=_fake_history)
    forecast = MagicMock(side_effect=_fake_forecast)
    with patch.object(main_mod, "fetch_history", recorder), patch.object(
        main_mod, "fetch_hourly_temperature", forecast
    ):
        result = daemon._run_cycle(client)

    grid_start, _ = history_window(START, config.model)
    assert recorder.call_args.kwargs == {"start_ts": grid_start, "end_ts": int(START.timestamp())}
    assert forecast.call_args.kwargs["start"] == START
    assert forecast.call_args.kwargs["end"] == START + timedelta(hours=24)
    assert result is not None and result.success and result.horizon_hours == 24

    topic, payload = client.publish.call_args_list[0].args[:2]
    assert topic == "mimir/input/baseload/base_load/forecast"
    steps = json.loads(payload)
    assert len(steps) == 25
    assert steps[0]["ts"] == START.isoformat()
    assert client.publish.call_count == 1  # no model stats topic configured


@freeze_time(NOW)
def test_model_stats_published_to_separate_topic(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config(model_stats_topic="mimir/status/baseload_weather/model"))
    client = _client()
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        daemon._run_cycle(client)

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert topics == ["mimir/input/baseload/base_load/forecast", "mimir/status/baseload_weather/model"]
    stats = json.loads(client.publish.call_args_list[1].args[1])
    assert stats["ts"] == NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert {"t_base_c", "levels", "r2", "mae_kwh", "training_coverage_pct", "forecast_points"} <= set(stats)


@freeze_time(NOW)
def test_signal_mimir_after_forecast(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config(signal_mimir=True))
    client = _client()
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        daemon._run_cycle(client)
    assert [c.args[0] for c in client.publish.call_args_list] == [
        "mimir/input/baseload/base_load/forecast",
        "mimir/input/trigger",
    ]


@freeze_time(NOW)
@pytest.mark.parametrize(
    "recorder_exc,forecast_exc",
    [
        (RecorderError("db down"), None),
        (None, WeatherForecastError("bad body")),
    ],
)
def test_fetch_failures_publish_nothing(
    daemon_factory: DaemonFactory,
    recorder_exc: RecorderError | None,
    forecast_exc: WeatherForecastError | None,
) -> None:
    daemon = daemon_factory(_config(model_stats_topic="m/stats", signal_mimir=True))
    client = _client()
    with patch.object(
        main_mod, "fetch_history", side_effect=recorder_exc or _fake_history
    ), patch.object(main_mod, "fetch_hourly_temperature", side_effect=forecast_exc or _fake_forecast):
        result = daemon._run_cycle(client)
    assert result is not None and result.success is False
    assert result.suppress_until is None
    client.publish.assert_not_called()


@freeze_time(NOW)
def test_rate_limit_suppresses_until_reset(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config())
    client = _client()
    reset = NOW + timedelta(minutes=15)
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=RateLimitError("429", reset)
    ):
        result = daemon._run_cycle(client)
    assert result is not None and result.success is False
    assert result.suppress_until == reset
    client.publish.assert_not_called()


@freeze_time(NOW)
def test_model_failure_publishes_nothing(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config(model_stats_topic="m/stats"))
    client = _client()

    def empty_history(config: Any, *, start_ts: int, end_ts: int) -> RecorderHistory:
        return RecorderHistory(energy={"sensor.load": {}}, temperature_c={})

    with patch.object(main_mod, "fetch_history", side_effect=empty_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        result = daemon._run_cycle(client)
    assert result is not None and result.success is False
    client.publish.assert_not_called()


def test_main_quiets_httpx(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import logging

    logging.getLogger("httpx").setLevel(logging.NOTSET)
    monkeypatch.setattr("sys.argv", ["baseload_weather", "--config", str(tmp_path / "absent.yaml")])
    with patch.object(main_mod, "load_helper_config", side_effect=SystemExit(1)), pytest.raises(SystemExit):
        main_mod.main()
    assert logging.getLogger("httpx").level == logging.WARNING


@freeze_time(NOW)
def test_model_stats_publish_failure_keeps_successful_cycle(daemon_factory: DaemonFactory) -> None:
    # The forecast and the solve trigger are already out when the optional
    # statistics publish fails; MQTT publishes cannot be rolled back, so the
    # cycle still reports success with its horizon.
    daemon = daemon_factory(_config(signal_mimir=True, model_stats_topic="m/stats"))
    client = _client()
    ok = MagicMock(rc=mqtt.MQTT_ERR_SUCCESS)
    failed = MagicMock(rc=mqtt.MQTT_ERR_PAYLOAD_SIZE)
    client.publish.side_effect = [ok, ok, failed]
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        result = daemon._run_cycle(client)
    assert [c.args[0] for c in client.publish.call_args_list] == [
        "mimir/input/baseload/base_load/forecast",
        "mimir/input/trigger",
        "m/stats",
    ]
    assert result is not None and result.success is True and result.horizon_hours == 24


@freeze_time(NOW)
def test_forecast_publish_failure_stops_the_cycle(daemon_factory: DaemonFactory) -> None:
    # A rejected forecast publish must not be followed by the solve trigger
    # or the statistics; the error propagates so HelperDaemon records a failed
    # cycle and keeps running.
    daemon = daemon_factory(_config(signal_mimir=True, model_stats_topic="m/stats"))
    client = _client()
    client.publish.return_value.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ), pytest.raises(PublishError):
        daemon._run_cycle(client)
    assert [c.args[0] for c in client.publish.call_args_list] == [
        "mimir/input/baseload/base_load/forecast"
    ]


@freeze_time(NOW)
def test_daemon_survives_forecast_publish_failure(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config(stats_topic="m/run"))
    client = _client()
    client.publish.return_value.rc = mqtt.MQTT_ERR_QUEUE_SIZE
    daemon._client = client
    trigger = SimpleNamespace(topic="mimir/input/tools/baseload/trigger", payload=b"", retain=False)
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        daemon._on_message(client, None, trigger)
    run_stats = [c for c in client.publish.call_args_list if c.args[0] == "m/run"]
    assert len(run_stats) == 1
    assert json.loads(run_stats[0].args[1])["success"] is False


@freeze_time(NOW)
def test_no_connection_at_qos1_counts_as_queued(daemon_factory: DaemonFactory) -> None:
    daemon = daemon_factory(_config())
    client = _client()
    client.publish.return_value.rc = mqtt.MQTT_ERR_NO_CONN
    with patch.object(main_mod, "fetch_history", side_effect=_fake_history), patch.object(
        main_mod, "fetch_hourly_temperature", side_effect=_fake_forecast
    ):
        result = daemon._run_cycle(client)
    assert result is not None and result.success is True
