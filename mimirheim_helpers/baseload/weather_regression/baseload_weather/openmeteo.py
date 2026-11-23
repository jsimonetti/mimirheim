"""Open-Meteo hourly temperature forecast client.

Fetches ``temperature_2m`` in degrees Celsius for an hour-aligned UTC window
from the Open-Meteo forecast API. The window starts 24 hours before the
forecast start so the model's temperature smoothing can bridge the lag
between the last recorder statistics row and the present.

Transient failures are retried here: HTTP 502, 503 and 504 and transport
errors (timeouts, refused connections) up to four attempts with exponential
back-off and jitter. HTTP 429 is not retried; it raises ``RateLimitError``
with a reset time, which the daemon turns into a suppressed retry window.

This module does no modelling and no caching. Helpers may only import
``helper_common``, so this is independent of the PV Open-Meteo helper.
"""

from __future__ import annotations

import logging
import math
import random
import time
from datetime import UTC, datetime, timedelta

import httpx

from baseload_weather.config import WeatherForecastConfig

logger = logging.getLogger(__name__)

TRANSIENT_STATUSES = frozenset({502, 503, 504})
REQUEST_ATTEMPTS = 4
RETRY_BASE_DELAY_S = 2.0
RETRY_JITTER_S = 1.0
RATE_LIMIT_BACKOFF = timedelta(minutes=15)
LAG_WINDOW = timedelta(hours=24)
"""Hours requested before the forecast start, to bridge recorder lag."""

_HOUR = timedelta(hours=1)
_TIME_FORMAT = "%Y-%m-%dT%H:%M"

_sleep = time.sleep
"""Indirection so tests can replace the back-off sleep."""


class WeatherForecastError(Exception):
    """Raised when no complete temperature forecast could be obtained."""


class RateLimitError(WeatherForecastError):
    """Raised on HTTP 429.

    Attributes:
        reset_at: UTC time before which no further request should be made.
    """

    def __init__(self, message: str, reset_at: datetime) -> None:
        super().__init__(message)
        self.reset_at = reset_at


def fetch_hourly_temperature(
    config: WeatherForecastConfig,
    *,
    start: datetime,
    end: datetime,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[int, float]:
    """Fetch hourly outdoor temperatures for ``[start - 24 h, end]``.

    Args:
        config: Location and endpoint settings.
        start: First forecast hour (UTC, timezone-aware, hour aligned).
        end: Last forecast hour, inclusive (UTC, timezone-aware, hour
            aligned, not before ``start``).
        client: Optional HTTP client, for connection reuse. A client is
            created and closed per call when omitted.
        now: Current time, used only for the rate-limit reset time.
            Defaults to the wall clock.

    Returns:
        Temperature in degrees Celsius keyed by the UTC epoch second of each
        hour's start. Every hour in ``[start, end]`` is present; hours in the
        24-hour lag window are present when the API returned a value.

    Raises:
        ValueError: If ``start`` or ``end`` is naive, not hour aligned, or
            ``end`` is before ``start``.
        RateLimitError: On HTTP 429.
        WeatherForecastError: On any other HTTP or transport failure after
            retries, a malformed body, or a response that does not cover every
            hour in ``[start, end]``.
    """
    for name, value in (("start", start), ("end", end)):
        if value.tzinfo is None:
            raise ValueError(f"{name} must be timezone-aware")
        if value.minute or value.second or value.microsecond:
            raise ValueError(f"{name} must be hour aligned")
    if end < start:
        raise ValueError("end must not be before start")

    first = (start - LAG_WINDOW).astimezone(UTC)
    params = {
        "latitude": config.latitude,
        "longitude": config.longitude,
        "hourly": "temperature_2m",
        "temperature_unit": "celsius",
        "timezone": "UTC",
        "start_hour": first.strftime(_TIME_FORMAT),
        "end_hour": end.astimezone(UTC).strftime(_TIME_FORMAT),
    }
    url = f"{config.base_url.rstrip('/')}/v1/forecast"

    owned = client is None
    http = client if client is not None else httpx.Client(timeout=config.timeout_s)
    try:
        payload = _get_with_retry(http, url, params, timeout_s=config.timeout_s, now=now)
    finally:
        if owned:
            http.close()

    temperatures = _parse(payload)
    missing = [
        h
        for h in _hours(start.astimezone(UTC), end.astimezone(UTC))
        if int(h.timestamp()) not in temperatures
    ]
    if missing:
        raise WeatherForecastError(
            f"Temperature forecast does not cover {len(missing)} hour(s) of the "
            f"horizon, first missing {missing[0].strftime(_TIME_FORMAT)} UTC."
        )
    return temperatures


def _get_with_retry(
    http: httpx.Client,
    url: str,
    params: dict[str, object],
    *,
    timeout_s: float,
    now: datetime | None,
) -> object:
    """Perform the GET with retries and return the decoded JSON body.

    The timeout is passed per request so it also applies to an injected
    client constructed with a different default.
    """
    last_problem = ""
    for attempt in range(REQUEST_ATTEMPTS):
        if attempt:
            delay = RETRY_BASE_DELAY_S * 2 ** (attempt - 1) + random.uniform(0.0, RETRY_JITTER_S)
            logger.warning("Open-Meteo request failed (%s); retrying in %.1f s.", last_problem, delay)
            _sleep(delay)
        try:
            response = http.get(url, params=params, timeout=timeout_s)
        except httpx.TransportError as exc:
            last_problem = f"{type(exc).__name__}: {exc}"
            continue
        if response.status_code in TRANSIENT_STATUSES:
            last_problem = f"HTTP {response.status_code}"
            continue
        if response.status_code == 429:
            reset_at = (now or datetime.now(UTC)) + RATE_LIMIT_BACKOFF
            raise RateLimitError(f"Open-Meteo rate limit (HTTP 429); retry after {reset_at.isoformat()}.", reset_at)
        if response.status_code != 200:
            raise WeatherForecastError(
                f"Open-Meteo returned HTTP {response.status_code}: {_reason(response)}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise WeatherForecastError("Open-Meteo returned a body that is not JSON.") from exc
    raise WeatherForecastError(
        f"Open-Meteo request failed after {REQUEST_ATTEMPTS} attempts ({last_problem})."
    )


def _reason(response: httpx.Response) -> str:
    """Return Open-Meteo's ``reason`` field from an error body, if present."""
    try:
        body = response.json()
    except ValueError:
        return response.reason_phrase or "no reason given"
    if isinstance(body, dict) and isinstance(body.get("reason"), str):
        return body["reason"]
    return response.reason_phrase or "no reason given"


def _parse(payload: object) -> dict[int, float]:
    """Turn an Open-Meteo hourly body into ``{utc_epoch_hour: celsius}``.

    Null values are skipped; any structural problem raises.
    """
    try:
        hourly = payload["hourly"]  # type: ignore[index]
        times = hourly["time"]
        values = hourly["temperature_2m"]
    except (KeyError, TypeError) as exc:
        raise WeatherForecastError("Open-Meteo body lacks hourly.time / hourly.temperature_2m.") from exc
    if not isinstance(times, list) or not isinstance(values, list) or len(times) != len(values):
        raise WeatherForecastError("Open-Meteo hourly arrays are missing or of unequal length.")
    result: dict[int, float] = {}
    for stamp, value in zip(times, values, strict=True):
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise WeatherForecastError(f"Open-Meteo temperature {value!r} is not a finite number.")
        try:
            hour = datetime.strptime(stamp, _TIME_FORMAT).replace(tzinfo=UTC)
        except (TypeError, ValueError) as exc:
            raise WeatherForecastError(f"Open-Meteo time {stamp!r} is not in {_TIME_FORMAT} form.") from exc
        result[int(hour.timestamp())] = float(value)
    return result


def _hours(start: datetime, end: datetime) -> list[datetime]:
    """Return every hour from ``start`` to ``end`` inclusive."""
    count = int((end - start) / _HOUR) + 1
    return [start + i * _HOUR for i in range(count)]
