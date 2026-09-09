"""HTTP fetcher for the Open-Meteo solar forecast.

This module wraps the ``open_meteo_solar_forecast`` async library and provides
a ``fetch_array`` coroutine that returns one average-power series per mimirheim
array. All caller-visible exceptions are wrapped in ``FetchError`` so the main
loop can handle API errors without importing library internals.

Two details of the library shape this module:

- Its per-plane parameters are parallel lists, one entry per plane, and it sums
  the planes into a single estimate. That is exactly the grouping mimirheim
  wants, so one array is one library call regardless of how many planes it has.
- ``Estimate.watts`` is the *instantaneous* power at each timestamp, while
  ``Estimate.wh_period_15m`` is the energy delivered over each quarter hour.
  mimirheim schedules average power per step, so this module derives kW from
  the energy series. Using ``watts`` would overstate production on the steep
  parts of the morning and evening curve.

What this module does not do:
- It does not trim or attach confidence values. That is series.py's job.
- It does not publish to MQTT.
- It does not import from mimirheim.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from aiohttp import ClientError as AiohttpClientError
from open_meteo_solar_forecast import OpenMeteoSolarForecast
from open_meteo_solar_forecast.exceptions import (
    OpenMeteoSolarForecastError,
    OpenMeteoSolarForecastRatelimitError,
)

from pv_openmeteo.config import ArrayConfig, OpenMeteoApiConfig

logger = logging.getLogger("pv_openmeteo.fetcher")

# How long to stay quiet after a rate-limit response. Open-Meteo does not
# report when the limit resets, so this is a fixed back-off rather than a value
# read from the response. Its free tier allows several thousand calls a day,
# well beyond what a half-hourly fetch of a handful of arrays consumes, so this
# path exists for a misconfigured trigger rather than for normal operation.
RATELIMIT_BACKOFF = timedelta(minutes=15)

# The horizon profile the library falls back to when a plane has none. It is
# only read for planes with use_horizon set, but the argument is a parallel
# list, so every plane needs an entry of the right shape.
_NO_HORIZON: tuple[tuple[float, float], ...] = ((0.0, 0.0), (360.0, 0.0))


class FetchError(Exception):
    """Raised when the Open-Meteo API call fails for any reason.

    Wraps every open_meteo_solar_forecast exception so callers need to catch
    only one type. The original exception is stored as ``__cause__``.
    """


class RatelimitError(FetchError):
    """Raised when Open-Meteo rejects the request with a rate-limit response.

    A subclass of ``FetchError`` so existing callers keep working. Callers that
    handle rate limiting specifically catch this first and use ``reset_at`` to
    suppress further triggers.

    Attributes:
        reset_at: UTC datetime after which the next request may be sent.
    """

    def __init__(self, message: str, reset_at: datetime) -> None:
        super().__init__(message)
        self.reset_at = reset_at


async def fetch_array(
    *,
    array: ArrayConfig,
    api: OpenMeteoApiConfig,
) -> dict[datetime, float]:
    """Fetch the combined power forecast for one mimirheim array.

    Makes a single library call covering every plane in the array. The library
    issues one Open-Meteo request per plane, converts irradiance to power with
    a cell-temperature correction, applies the per-plane inverter clamp, sums
    the planes, and applies the shared inverter clamp if there is one.

    Args:
        array: The array to fetch, including its planes and inverter limits.
            Plane coordinates must already be resolved against the site, which
            ``PvOpenMeteoConfig`` does at load time.
        api: Open-Meteo API settings shared by every array.

    Returns:
        A dict mapping UTC-aware timestamps to average power in kW over the
        quarter hour that begins at that timestamp.

    Raises:
        RatelimitError: If Open-Meteo returns a rate-limit response. Carries the
            time after which requests may resume.
        FetchError: If the call fails for any other reason, or if the response
            contains no forecast steps.
    """
    planes = array.planes

    # Every per-plane argument is a list in plane order. The library requires
    # them all to be the same length and zips them together.
    kwargs: dict = {
        "latitude": [plane.latitude for plane in planes],
        "longitude": [plane.longitude for plane in planes],
        "declination": [plane.declination for plane in planes],
        "azimuth": [plane.azimuth for plane in planes],
        "dc_kwp": [plane.peak_power_kwp for plane in planes],
        "efficiency_factor": [plane.efficiency_factor for plane in planes],
        "tracking": [plane.tracking for plane in planes],
        "damping_morning": [plane.damping_morning for plane in planes],
        "damping_evening": [plane.damping_evening for plane in planes],
        "max_snowcover_depth_cm": [plane.max_snowcover_depth_cm for plane in planes],
        "use_horizon": [plane.use_horizon for plane in planes],
        "partial_shading": [plane.partial_shading for plane in planes],
        "horizon_map": [_horizon_map(plane.horizon) for plane in planes],
        "ac_kwp": _ac_kwp(array),
        "api_key": api.api_key,
        "base_url": api.base_url,
        "weather_model": api.weather_model,
        "forecast_days": api.forecast_days,
        # past_days counts whole local days, so any retention window at all
        # needs yesterday: a fetch just after local midnight looks back across
        # the day boundary. The extra day is a few hundred bytes of response.
        "past_days": math.ceil(api.past_hours / 24),
    }

    try:
        async with OpenMeteoSolarForecast(**kwargs) as forecast:
            estimate = await forecast.estimate()
            energy_wh = estimate.wh_period_15m
    except OpenMeteoSolarForecastRatelimitError as exc:
        raise RatelimitError(
            f"Rate limit exceeded: {exc}",
            reset_at=datetime.now(tz=timezone.utc) + RATELIMIT_BACKOFF,
        ) from exc
    except OpenMeteoSolarForecastError as exc:
        raise FetchError(f"API error: {exc}") from exc
    except AiohttpClientError as exc:
        # aiohttp errors such as a DNS failure can escape the library without
        # being wrapped in its own exception hierarchy.
        raise FetchError(f"HTTP client error: {exc}") from exc

    if not energy_wh:
        raise FetchError("Open-Meteo returned no forecast steps")

    # Each entry is the energy delivered over the quarter hour starting at its
    # timestamp. Average power over that quarter hour is four times the energy,
    # and a further factor of a thousand converts watt-hours to kilowatts.
    return {
        _as_utc(ts): wh / 250.0
        for ts, wh in sorted(energy_wh.items(), key=lambda item: _as_utc(item[0]))
    }


def _ac_kwp(array: ArrayConfig) -> float | list[float | None] | None:
    """Return the ``ac_kwp`` argument for one array.

    The library reads the argument's type as the topology: a scalar is one
    inverter shared by every plane, clamping their combined output, and a list
    is one inverter per plane, clamping each separately. ``None`` in either
    position means no clamp.

    Args:
        array: The array whose inverter layout is being described.

    Returns:
        A float for a shared inverter, a list for per-plane inverters, or None
        when the array declares no AC limit at all.
    """
    if array.inverter_kwp is not None:
        return array.inverter_kwp
    per_plane = [plane.inverter_kwp for plane in array.planes]
    if any(value is not None for value in per_plane):
        return per_plane
    return None


def _horizon_map(
    horizon: list | None,
) -> tuple[tuple[float, float], ...]:
    """Convert a plane's horizon profile to the library's tuple-of-pairs form.

    Args:
        horizon: The plane's horizon points, or None when it has no profile.

    Returns:
        A tuple of (compass bearing, elevation) pairs. Planes without a profile
        get a flat placeholder, which the library ignores because their
        ``use_horizon`` flag is False.
    """
    if not horizon:
        return _NO_HORIZON
    return tuple((point.azimuth, point.elevation) for point in horizon)


def _as_utc(ts: datetime) -> datetime:
    """Return ``ts`` as a UTC-aware datetime, treating a naive value as UTC."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
