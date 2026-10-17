"""Unit tests for the per-request retry in pv_openmeteo.fetcher.

Open-Meteo's free API answers a share of requests with ``503 The service is
overloaded``. The library issues one request per plane and turns any 502 or 503
into a connection error, so without a retry a single overloaded response drops
the whole array for the cycle.

These tests run the real library against a fake HTTP session, so they exercise
the same request path production uses. ``asyncio.sleep`` is replaced through
``pv_openmeteo.fetcher._sleep`` so no test waits for real.

Tests verify:
- A 503 on one plane is retried for that plane only; the array publishes and the
  request count is the plane count plus one.
- A connection error or timeout on one request is retried the same way.
- When every attempt fails, FetchError is raised after REQUEST_ATTEMPTS requests,
  including when the failures are plain timeouts.
- A transient response whose body cannot be read is still retried.
- A 429 is not retried and still surfaces as RatelimitError.
- A retried response's status and Open-Meteo ``reason`` are logged.
- Consecutive plane requests are spaced apart.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from pv_openmeteo import fetcher
from pv_openmeteo.config import ArrayConfig, OpenMeteoApiConfig
from pv_openmeteo.fetcher import FetchError, RatelimitError, fetch_array

_DAY = int(datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp())
_NOON = _DAY + 12 * 3600


def _forecast_body() -> dict[str, Any]:
    """Return a minimal valid /v1/forecast response: four quarter hours at noon."""
    times = [_NOON + 900 * i for i in range(4)]
    values = [500.0] * len(times)
    return {
        "utc_offset_seconds": 0,
        "minutely_15": {
            "time": times,
            "temperature_2m": [15.0] * len(times),
            "global_tilted_irradiance": values,
            "global_tilted_irradiance_instant": values,
            "diffuse_radiation": [100.0] * len(times),
            "diffuse_radiation_instant": [100.0] * len(times),
            "direct_radiation": [400.0] * len(times),
            "direct_radiation_instant": [400.0] * len(times),
            "snow_depth": [0.0] * len(times),
        },
        "daily": {
            "time": [_DAY],
            "sunrise": [_DAY + 6 * 3600],
            "sunset": [_DAY + 18 * 3600],
        },
    }


class _FakeResponse:
    """The subset of aiohttp.ClientResponse the library and the retry read."""

    def __init__(self, status: int, body: Any, *, body_times_out: bool = False) -> None:
        self.status = status
        self._text = json.dumps(body)
        self._body_times_out = body_times_out
        self.headers = {"Content-Type": "application/json; charset=utf-8"}
        self.released = False

    async def json(self) -> Any:
        return json.loads(self._text)

    async def text(self) -> str:
        if self._body_times_out:
            raise TimeoutError()
        return self._text

    def release(self) -> None:
        self.released = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None,  # type: ignore[arg-type]
                history=(),
                status=self.status,
            )


_OK = "ok"
_SLOW_BODY = "503 with a body read that times out"


class _FakeSession:
    """Replays a scripted outcome per request, keyed by plane azimuth.

    Each script entry is ``_OK``, ``_SLOW_BODY``, an HTTP status code, or an
    exception instance.
    Requests beyond the end of a plane's script succeed.
    """

    def __init__(self, scripts: dict[str, list[Any]]) -> None:
        self._scripts = {key: list(value) for key, value in scripts.items()}
        self.requests: list[str] = []
        self.closed = False

    async def request(
        self, method: str, url: str, *, params: dict[str, Any] | None = None
    ) -> _FakeResponse:
        azimuth = (params or {})["azimuth"]
        self.requests.append(azimuth)
        script = self._scripts.get(azimuth, [])
        outcome = script.pop(0) if script else _OK
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome == _OK:
            return _FakeResponse(200, _forecast_body())
        if outcome == _SLOW_BODY:
            return _FakeResponse(503, {}, body_times_out=True)
        if outcome == 503:
            return _FakeResponse(503, {"error": True, "reason": "The service is overloaded"})
        return _FakeResponse(outcome, {"error": True, "reason": f"status {outcome}"})

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        self.closed = True


def _array() -> ArrayConfig:
    plane = {
        "declination": 25,
        "peak_power_kwp": 1.0,
        "latitude": 52.0,
        "longitude": 5.0,
    }
    return ArrayConfig.model_validate(
        {
            "planes": [
                {**plane, "azimuth": -90},
                {**plane, "azimuth": 90},
            ]
        }
    )


async def _fetch(session: _FakeSession) -> tuple[dict[datetime, float], AsyncMock]:
    sleep = AsyncMock()
    with (
        patch("pv_openmeteo.fetcher.ClientSession", return_value=session),
        patch("pv_openmeteo.fetcher._sleep", sleep),
    ):
        series = await fetch_array(array=_array(), api=OpenMeteoApiConfig(past_hours=0.0))
    return series, sleep


async def test_one_overloaded_plane_is_retried_and_the_array_publishes() -> None:
    session = _FakeSession({"90.0": [503]})
    series, _ = await _fetch(session)

    assert series
    # Two planes plus one retry for the plane that was overloaded. The healthy
    # plane is not fetched again.
    assert session.requests == ["-90.0", "90.0", "90.0"]
    assert session.closed


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_every_transient_status_is_retried(status: int) -> None:
    session = _FakeSession({"-90.0": [status]})
    series, _ = await _fetch(session)

    assert series
    assert len(session.requests) == 3


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("reset by peer"),
        TimeoutError(),
    ],
)
async def test_connection_errors_are_retried(error: BaseException) -> None:
    session = _FakeSession({"-90.0": [error]})
    series, _ = await _fetch(session)

    assert series
    assert len(session.requests) == 3


async def test_exhausted_retries_raise_fetch_error() -> None:
    session = _FakeSession({"-90.0": [503] * fetcher.REQUEST_ATTEMPTS})
    with pytest.raises(FetchError):
        await _fetch(session)

    assert session.requests == ["-90.0"] * fetcher.REQUEST_ATTEMPTS
    assert session.closed


async def test_exhausted_connection_errors_raise_fetch_error() -> None:
    errors = [aiohttp.ClientConnectionError("down")] * fetcher.REQUEST_ATTEMPTS
    session = _FakeSession({"-90.0": errors})
    with pytest.raises(FetchError):
        await _fetch(session)

    assert len(session.requests) == fetcher.REQUEST_ATTEMPTS


async def test_exhausted_timeouts_raise_fetch_error() -> None:
    # Plain TimeoutError is not an aiohttp.ClientError, so it needs its own
    # wrapping; otherwise it escapes the per-array FetchError handler.
    session = _FakeSession({"-90.0": [TimeoutError()] * fetcher.REQUEST_ATTEMPTS})
    with pytest.raises(FetchError):
        await _fetch(session)

    assert len(session.requests) == fetcher.REQUEST_ATTEMPTS


async def test_unreadable_error_body_is_still_retried() -> None:
    session = _FakeSession({"-90.0": [_SLOW_BODY]})
    series, _ = await _fetch(session)

    assert series
    assert len(session.requests) == 3


async def test_ratelimit_is_not_retried() -> None:
    session = _FakeSession({"-90.0": [429]})
    with pytest.raises(RatelimitError):
        await _fetch(session)

    assert session.requests == ["-90.0"]


async def test_retry_backs_off_exponentially() -> None:
    session = _FakeSession({"-90.0": [503, 503]})
    _, sleep = await _fetch(session)

    delays = [call.args[0] for call in sleep.await_args_list]
    base = fetcher.RETRY_BASE_DELAY_S
    # First request, retry, retry, then the second plane after the spacing gap.
    assert len(delays) == 3
    assert base <= delays[0] < 2 * base
    assert 2 * base <= delays[1] < 3 * base
    assert delays[2] == fetcher.REQUEST_SPACING_S


async def test_plane_requests_are_spaced_apart() -> None:
    session = _FakeSession({})
    _, sleep = await _fetch(session)

    # Only the second plane waits; the first request goes out immediately.
    assert [call.args[0] for call in sleep.await_args_list] == [fetcher.REQUEST_SPACING_S]


async def test_retried_status_and_reason_are_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _FakeSession({"-90.0": [503]})
    with caplog.at_level(logging.WARNING, logger="pv_openmeteo.fetcher"):
        await _fetch(session)

    messages = [record.getMessage() for record in caplog.records]
    assert any("503" in m and "The service is overloaded" in m for m in messages)
