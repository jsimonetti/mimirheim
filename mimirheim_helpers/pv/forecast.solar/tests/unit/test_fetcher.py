"""Unit tests for pv_fetcher.fetcher.

The fetcher calls the async forecast_solar library. These tests mock the
ForecastSolar context manager so no HTTP traffic occurs.

Tests verify:
- fetch_array returns a dict[datetime, int] from estimate.watts with all
  keys converted to UTC-aware datetimes.
- HTTP errors (ForecastSolarConnectionError) are re-raised as FetchError.
- Rate limit errors (ForecastSolarRatelimitError) are re-raised as RatelimitError.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pv_fetcher.fetcher import FetchError, RatelimitError, fetch_array


def _make_mock_estimate(watts: dict) -> MagicMock:
    """Return a mock Estimate object with the given watts dict."""
    est = MagicMock()
    est.watts = watts
    return est


def _make_mock_context(estimate: MagicMock) -> AsyncMock:
    """Return an async context manager mock that yields the given estimate."""
    forecast_instance = AsyncMock()
    forecast_instance.__aenter__ = AsyncMock(return_value=forecast_instance)
    forecast_instance.__aexit__ = AsyncMock(return_value=False)
    forecast_instance.estimate = AsyncMock(return_value=estimate)
    return forecast_instance


@pytest.mark.asyncio
async def test_fetch_array_returns_watts_dict() -> None:
    # The API returns naive UTC datetimes; fetch_array attaches timezone.utc.
    ts_naive = datetime(2026, 3, 30, 12, 0, 0)
    ts_utc = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
    mock_estimate = _make_mock_estimate({ts_naive: 3000})
    mock_ctx = _make_mock_context(mock_estimate)

    with patch("pv_fetcher.fetcher.ForecastSolar", return_value=mock_ctx):
        result = await fetch_array(
            api_key=None,
            latitude=52.37,
            longitude=4.89,
            declination=35,
            azimuth=0,
            kwp=5.0,
        )

    assert ts_utc in result
    assert result[ts_utc] == 3000


@pytest.mark.asyncio
async def test_fetch_array_raises_fetch_error_on_connection_failure() -> None:
    from forecast_solar.exceptions import ForecastSolarConnectionError

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=ForecastSolarConnectionError)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("pv_fetcher.fetcher.ForecastSolar", return_value=mock_ctx):
        with pytest.raises(FetchError):
            await fetch_array(
                api_key=None,
                latitude=52.37,
                longitude=4.89,
                declination=35,
                azimuth=0,
                kwp=5.0,
            )


@pytest.mark.asyncio
async def test_fetch_array_raises_fetch_error_on_ratelimit() -> None:
    # ForecastSolarRatelimitError requires a data dict; raise the base
    # ForecastSolarConnectionError instead to test the error-wrapping path
    # without coupling the test to the internal exception constructor format.
    from forecast_solar.exceptions import ForecastSolarConnectionError

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=ForecastSolarConnectionError("rate limited"))
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("pv_fetcher.fetcher.ForecastSolar", return_value=mock_ctx):
        with pytest.raises(FetchError):
            await fetch_array(
                api_key=None,
                latitude=52.37,
                longitude=4.89,
                declination=35,
                azimuth=0,
                kwp=5.0,
            )


@pytest.mark.asyncio
async def test_fetch_array_raises_ratelimit_error_with_reset_at() -> None:
    """ForecastSolarRatelimitError must produce a RatelimitError carrying reset_at."""
    from datetime import timezone
    from forecast_solar.exceptions import ForecastSolarRatelimitError

    reset_time = datetime(2026, 3, 31, 15, 0, 0, tzinfo=timezone.utc)
    exc = ForecastSolarRatelimitError({
        "text": "rate limit exceeded",
        "code": 429,
        "ratelimit": {"retry-at": reset_time.isoformat()},
    })

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=exc)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("pv_fetcher.fetcher.ForecastSolar", return_value=mock_ctx):
        with pytest.raises(RatelimitError) as exc_info:
            await fetch_array(
                api_key=None,
                latitude=52.37,
                longitude=4.89,
                declination=35,
                azimuth=0,
                kwp=5.0,
            )

    assert exc_info.value.reset_at == reset_time


@pytest.mark.asyncio
async def test_ratelimit_error_is_a_fetch_error() -> None:
    """RatelimitError must be a subclass of FetchError so callers that catch
    FetchError still handle it correctly."""
    from datetime import timezone
    from forecast_solar.exceptions import ForecastSolarRatelimitError

    reset_time = datetime(2026, 3, 31, 15, 0, 0, tzinfo=timezone.utc)
    exc = ForecastSolarRatelimitError({
        "text": "rate limit exceeded",
        "code": 429,
        "ratelimit": {"retry-at": reset_time.isoformat()},
    })

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__ = AsyncMock(side_effect=exc)
    mock_ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("pv_fetcher.fetcher.ForecastSolar", return_value=mock_ctx):
        with pytest.raises(FetchError):
            await fetch_array(
                api_key=None,
                latitude=52.37,
                longitude=4.89,
                declination=35,
                azimuth=0,
                kwp=5.0,
            )
