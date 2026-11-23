"""Unit tests for baseload_weather.openmeteo.

All HTTP traffic is mocked with respx; no real API calls are made. Retry
back-off sleeps are replaced so the tests run instantly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from baseload_weather import openmeteo
from baseload_weather.config import WeatherForecastConfig
from baseload_weather.openmeteo import (
    RateLimitError,
    WeatherForecastError,
    fetch_hourly_temperature,
)

URL = "https://api.open-meteo.com/v1/forecast"
S = datetime(2026, 1, 10, 12, tzinfo=UTC)
E = S + timedelta(hours=4)
CFG = WeatherForecastConfig(latitude=52.0, longitude=5.1)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record back-off delays instead of sleeping."""
    delays: list[float] = []
    monkeypatch.setattr(openmeteo, "_sleep", delays.append)
    return delays


def _body(start: datetime, values: list[float | None]) -> dict[str, Any]:
    """Build an Open-Meteo hourly response starting at ``start`` (UTC)."""
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(len(values))]
    return {"hourly": {"time": times, "temperature_2m": values}}


def _full_body() -> dict[str, Any]:
    """Cover S - 24 h through E inclusive (29 hours)."""
    start = S - timedelta(hours=24)
    return _body(start, [float(i) for i in range(29)])


@respx.mock
def test_request_parameters() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_full_body()))
    fetch_hourly_temperature(CFG, start=S, end=E)
    params = route.calls.last.request.url.params
    assert params["latitude"] == "52.0"
    assert params["longitude"] == "5.1"
    assert params["hourly"] == "temperature_2m"
    assert params["timezone"] == "UTC"
    assert params["temperature_unit"] == "celsius"
    assert params["start_hour"] == "2026-01-09T12:00"
    assert params["end_hour"] == "2026-01-10T16:00"


@respx.mock
def test_returns_hours_keyed_by_utc_epoch_including_lag_window() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_full_body()))
    temps = fetch_hourly_temperature(CFG, start=S, end=E)
    first = int((S - timedelta(hours=24)).timestamp())
    assert temps[first] == 0.0
    assert temps[int(S.timestamp())] == 24.0
    assert temps[int(E.timestamp())] == 28.0
    assert len(temps) == 29


@respx.mock
def test_custom_base_url() -> None:
    cfg = WeatherForecastConfig(latitude=1.0, longitude=2.0, base_url="http://meteo.local:8080/")
    route = respx.get("http://meteo.local:8080/v1/forecast").mock(
        return_value=httpx.Response(200, json=_full_body())
    )
    fetch_hourly_temperature(cfg, start=S, end=E)
    assert route.called


@respx.mock
def test_null_in_lag_window_is_skipped() -> None:
    body = _full_body()
    body["hourly"]["temperature_2m"][0] = None
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    temps = fetch_hourly_temperature(CFG, start=S, end=E)
    assert int((S - timedelta(hours=24)).timestamp()) not in temps


@respx.mock
def test_missing_hour_in_horizon_raises() -> None:
    body = _full_body()
    body["hourly"]["temperature_2m"][26] = None  # S + 2 h
    respx.get(URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises(WeatherForecastError, match="cover"):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
def test_short_response_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=_body(S - timedelta(hours=24), [1.0] * 27)))
    with pytest.raises(WeatherForecastError, match="cover"):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"hourly": {}},
        {"hourly": {"time": ["2026-01-10T12:00"], "temperature_2m": []}},
        {"hourly": {"time": ["not-a-time"], "temperature_2m": [1.0]}},
        {"hourly": {"time": ["2026-01-10T12:00"], "temperature_2m": ["warm"]}},
        [1, 2, 3],
    ],
)
def test_malformed_body_raises(payload: Any) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(WeatherForecastError):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
def test_non_json_body_raises() -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(WeatherForecastError):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_status_retried_then_succeeds(status: int, _no_sleep: list[float]) -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(status), httpx.Response(status), httpx.Response(200, json=_full_body())]
    )
    temps = fetch_hourly_temperature(CFG, start=S, end=E)
    assert route.call_count == 3
    assert len(temps) == 29
    assert len(_no_sleep) == 2
    assert _no_sleep[1] > _no_sleep[0] * 1.0  # exponential growth (jitter is additive and bounded)


@respx.mock
def test_transient_status_gives_up_after_four_attempts() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(503))
    with pytest.raises(WeatherForecastError, match="503"):
        fetch_hourly_temperature(CFG, start=S, end=E)
    assert route.call_count == 4


@respx.mock
def test_transport_error_retried() -> None:
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectTimeout("slow"), httpx.Response(200, json=_full_body())]
    )
    fetch_hourly_temperature(CFG, start=S, end=E)
    assert route.call_count == 2


@respx.mock
def test_transport_error_gives_up() -> None:
    respx.get(URL).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(WeatherForecastError):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
def test_rate_limit_not_retried_and_reports_reset() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(429))
    now = datetime(2026, 1, 10, 12, 5, tzinfo=UTC)
    with pytest.raises(RateLimitError) as excinfo:
        fetch_hourly_temperature(CFG, start=S, end=E, now=now)
    assert route.call_count == 1
    assert excinfo.value.reset_at == now + timedelta(minutes=15)


@respx.mock
def test_client_error_reports_api_reason_without_retry() -> None:
    route = respx.get(URL).mock(
        return_value=httpx.Response(400, json={"error": True, "reason": "Latitude must be in range"})
    )
    with pytest.raises(WeatherForecastError, match="Latitude must be in range"):
        fetch_hourly_temperature(CFG, start=S, end=E)
    assert route.call_count == 1


def test_naive_datetimes_rejected() -> None:
    with pytest.raises(ValueError):
        fetch_hourly_temperature(CFG, start=S.replace(tzinfo=None), end=E)


def test_unaligned_hours_rejected() -> None:
    with pytest.raises(ValueError):
        fetch_hourly_temperature(CFG, start=S + timedelta(minutes=30), end=E)


def test_end_before_start_rejected() -> None:
    with pytest.raises(ValueError):
        fetch_hourly_temperature(CFG, start=E, end=S)


@respx.mock
@pytest.mark.parametrize("index", [0, 26])  # lag window, horizon
@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1e309"])
def test_non_finite_temperature_rejected(index: int, raw: str) -> None:
    body = _full_body()
    values = body["hourly"]["temperature_2m"]
    times = body["hourly"]["time"]
    # json.dumps cannot emit these tokens portably, so build the body by hand.
    text = (
        '{"hourly": {"time": ['
        + ",".join(f'"{t}"' for t in times)
        + '], "temperature_2m": ['
        + ",".join(raw if i == index else repr(v) for i, v in enumerate(values))
        + "]}}"
    )
    respx.get(URL).mock(return_value=httpx.Response(200, text=text, headers={"content-type": "application/json"}))
    with pytest.raises(WeatherForecastError, match="finite"):
        fetch_hourly_temperature(CFG, start=S, end=E)


@respx.mock
def test_timeout_applied_to_injected_client_which_stays_open() -> None:
    route = respx.get(URL).mock(return_value=httpx.Response(200, json=_full_body()))
    cfg = WeatherForecastConfig(latitude=52.0, longitude=5.1, timeout_s=7.0)
    with httpx.Client(timeout=None) as client:
        fetch_hourly_temperature(cfg, start=S, end=E, client=client)
        assert not client.is_closed
        timeouts = route.calls.last.request.extensions["timeout"]
        assert timeouts == {"connect": 7.0, "read": 7.0, "write": 7.0, "pool": 7.0}
