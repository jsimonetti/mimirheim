"""HTTP fetcher for the forecast.solar API.

This module wraps the ``forecast_solar`` async library and provides a simple
``fetch_array`` coroutine that returns the raw ``estimate.watts`` dict. All
caller-visible exceptions are wrapped in ``FetchError`` so that the main loop
can handle API errors uniformly without importing forecast_solar internals.

What this module does not do:
- It does not convert watts to kilowatts or apply confidence values.
  That is confidence.py's responsibility.
- It does not publish to MQTT.
- It does not import from mimirheim.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from forecast_solar import ForecastSolar
from aiohttp import ClientError as AiohttpClientError
from forecast_solar.exceptions import (
    ForecastSolarConnectionError,
    ForecastSolarError,
    ForecastSolarRatelimitError,
    ForecastSolarRequestError,
)

logger = logging.getLogger("pv_fetcher.fetcher")


class FetchError(Exception):
    """Raised when the forecast.solar API call fails for any reason.

    Wraps all forecast_solar library exceptions so callers only need to
    catch one type. The original exception is stored as ``__cause__``.
    """


class RatelimitError(FetchError):
    """Raised when the forecast.solar rate limit is exceeded.

    A subclass of ``FetchError`` so existing callers that catch ``FetchError``
    continue to work. Callers that need to handle ratelimit specifically (e.g.
    to abort the fetch cycle and log the reset time) should catch this type
    before catching the broader ``FetchError``.

    Attributes:
        reset_at: UTC datetime after which the next request may be sent,
            as reported by the API in the ``ratelimit.retry-at`` field.
    """

    def __init__(self, message: str, reset_at: datetime) -> None:
        super().__init__(message)
        self.reset_at = reset_at


async def fetch_array(
    *,
    api_key: str | None,
    latitude: float,
    longitude: float,
    declination: int,
    azimuth: int,
    kwp: float,
) -> dict[datetime, int]:
    """Fetch a PV power forecast from the forecast.solar API.

    Calls the forecast.solar estimate endpoint for one array and returns the
    ``Estimate.watts`` dict with all keys converted to UTC-aware datetimes.
    The forecast.solar library requests UTC timestamps from the API
    (``time=utc``); the keys arrive as naive strings and are made aware here.

    The caller is responsible for converting watts to kW and applying
    confidence decay.

    Uses the free anonymous endpoint when ``api_key`` is None. With a paid
    key the API returns a longer horizon (up to 4 days) and has higher rate
    limits; see https://forecast.solar/pricing.

    Args:
        api_key: Optional API key. None = free anonymous tier.
        latitude: Site latitude in decimal degrees (positive = north).
        longitude: Site longitude in decimal degrees (positive = east).
        declination: Panel tilt in degrees from horizontal [0, 90].
        azimuth: Azimuth deviation from south in degrees [-180, 180].
        kwp: Array peak power in kWp.

    Returns:
        A dict mapping UTC-aware datetimes to integer watts.

    Raises:
        FetchError: If the API call fails for any reason (network error,
            DNS failure, HTTP error, or any other aiohttp-level exception).
            The original exception is available as ``__cause__``.
        RatelimitError: If the API returns a 429 rate-limit response. This is
            a subclass of ``FetchError``. ``reset_at`` carries the UTC datetime
            after which the next request may be sent.
    """
    kwargs: dict = dict(
        latitude=latitude,
        longitude=longitude,
        declination=declination,
        azimuth=azimuth,
        kwp=kwp,
    )
    if api_key is not None:
        kwargs["api_key"] = api_key

    try:
        async with ForecastSolar(**kwargs) as forecast:
            estimate = await forecast.estimate()
            # The library calls the API with time=utc, so the keys are UTC
            # timestamps represented as naive datetimes (no tzinfo). Attach
            # UTC here so all downstream code works in aware datetimes.
            return {
                ts.replace(tzinfo=timezone.utc): w
                for ts, w in estimate.watts.items()
            }
    except ForecastSolarRatelimitError as exc:
        raise RatelimitError(
            f"Rate limit exceeded: {exc}",
            reset_at=exc.reset_at,
        ) from exc
    except ForecastSolarConnectionError as exc:
        raise FetchError(f"Connection error: {exc}") from exc
    except ForecastSolarRequestError as exc:
        raise FetchError(f"Request error: {exc}") from exc
    except ForecastSolarError as exc:
        raise FetchError(f"API error: {exc}") from exc
    except AiohttpClientError as exc:
        # aiohttp exceptions (e.g. ClientConnectorDNSError on DNS failure) can
        # escape the forecast_solar library without being wrapped in its own
        # exception hierarchy. Catch them here so callers only see FetchError.
        raise FetchError(f"HTTP client error: {exc}") from exc
