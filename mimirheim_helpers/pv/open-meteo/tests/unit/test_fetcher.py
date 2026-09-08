"""Unit tests for pv_openmeteo.fetcher.

The fetcher calls the async open_meteo_solar_forecast library. These tests
patch the library class so no HTTP traffic occurs.

Tests verify:
- Per-plane values are passed as parallel lists in plane order.
- A shared array inverter is passed as a scalar ac_kwp, per-plane inverters as
  a list, and an array with neither passes None.
- The horizon profile is converted to the tuple-of-pairs form the library takes,
  and use_horizon follows from its presence.
- wh_period_15m is converted to average kW rather than Estimate.watts, which is
  the instantaneous series.
- Library errors surface as FetchError, and a rate-limit response as
  RatelimitError carrying a reset time.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from open_meteo_solar_forecast.exceptions import (
    OpenMeteoSolarForecastConnectionError,
    OpenMeteoSolarForecastRatelimitError,
)

from pv_openmeteo.config import ArrayConfig, OpenMeteoApiConfig
from pv_openmeteo.fetcher import FetchError, RatelimitError, fetch_array


def _array(**overrides: object) -> ArrayConfig:
    raw: dict = {
        "planes": [
            {
                "declination": 25,
                "azimuth": -90,
                "peak_power_kwp": 1.04,
                "efficiency_factor": 0.9,
                "latitude": 52.0,
                "longitude": 5.0,
            },
            {
                "declination": 45,
                "azimuth": 90,
                "peak_power_kwp": 1.3,
                "efficiency_factor": 0.8,
                "latitude": 52.0,
                "longitude": 5.0,
            },
        ]
    }
    raw.update(overrides)
    return ArrayConfig.model_validate(raw)


def _mock_library(wh_period_15m: dict) -> tuple[MagicMock, AsyncMock]:
    """Return a patchable library class and the instance it yields."""
    estimate = MagicMock()
    estimate.wh_period_15m = wh_period_15m
    estimate.watts = {ts: 999999 for ts in wh_period_15m}

    instance = AsyncMock()
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=False)
    instance.estimate = AsyncMock(return_value=estimate)

    cls = MagicMock(return_value=instance)
    return cls, instance


async def test_per_plane_values_are_passed_as_parallel_lists() -> None:
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=OpenMeteoApiConfig())

    kwargs = cls.call_args.kwargs
    assert kwargs["declination"] == [25.0, 45.0]
    assert kwargs["azimuth"] == [-90.0, 90.0]
    assert kwargs["dc_kwp"] == [1.04, 1.3]
    assert kwargs["efficiency_factor"] == [0.9, 0.8]
    assert kwargs["latitude"] == [52.0, 52.0]
    assert kwargs["longitude"] == [5.0, 5.0]
    assert kwargs["tracking"] == ["none", "none"]
    assert kwargs["damping_morning"] == [0.0, 0.0]
    assert kwargs["damping_evening"] == [0.0, 0.0]
    assert kwargs["max_snowcover_depth_cm"] == [0.0, 0.0]
    assert kwargs["use_horizon"] == [False, False]
    assert kwargs["partial_shading"] == [False, False]


async def test_array_without_inverter_passes_none() -> None:
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=OpenMeteoApiConfig())
    assert cls.call_args.kwargs["ac_kwp"] is None


async def test_shared_inverter_is_passed_as_a_scalar() -> None:
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(inverter_kwp=5.0), api=OpenMeteoApiConfig())
    assert cls.call_args.kwargs["ac_kwp"] == 5.0


async def test_per_plane_inverters_are_passed_as_a_list() -> None:
    array = _array()
    array.planes[1].inverter_kwp = 3.25
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=array, api=OpenMeteoApiConfig())
    assert cls.call_args.kwargs["ac_kwp"] == [None, 3.25]


async def test_horizon_is_converted_to_pairs_per_plane() -> None:
    array = _array()
    raw = array.model_dump()
    raw["planes"][0]["horizon"] = [
        {"azimuth": 0, "elevation": 5},
        {"azimuth": 360, "elevation": 5},
    ]
    array = ArrayConfig.model_validate(raw)
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=array, api=OpenMeteoApiConfig())

    kwargs = cls.call_args.kwargs
    assert kwargs["use_horizon"] == [True, False]
    assert kwargs["horizon_map"][0] == ((0.0, 5.0), (360.0, 5.0))
    # A plane without a profile still needs a placeholder of the right shape.
    assert len(kwargs["horizon_map"]) == 2


async def test_api_settings_are_forwarded() -> None:
    api = OpenMeteoApiConfig(
        api_key="secret",
        base_url="https://customer-api.open-meteo.com",
        weather_model="icon_seamless",
        forecast_days=5,
        past_hours=1.0,
    )
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=api)

    kwargs = cls.call_args.kwargs
    assert kwargs["api_key"] == "secret"
    assert kwargs["base_url"] == "https://customer-api.open-meteo.com"
    assert kwargs["weather_model"] == "icon_seamless"
    assert kwargs["forecast_days"] == 5
    assert kwargs["past_days"] == 1


async def test_zero_past_hours_requests_no_past_days() -> None:
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=OpenMeteoApiConfig(past_hours=0.0))
    assert cls.call_args.kwargs["past_days"] == 0


async def test_energy_per_quarter_hour_becomes_average_kilowatts() -> None:
    ts = datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)
    cls, _ = _mock_library({ts: 500.0, ts + timedelta(minutes=15): 0.0})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        series = await fetch_array(array=_array(), api=OpenMeteoApiConfig())

    # 500 Wh delivered over a quarter of an hour is an average of 2 kW.
    assert series[ts] == 2.0
    assert series[ts + timedelta(minutes=15)] == 0.0


async def test_timestamps_are_returned_in_utc() -> None:
    local = timezone(timedelta(hours=2))
    ts = datetime(2026, 3, 30, 14, 0, tzinfo=local)
    cls, _ = _mock_library({ts: 250.0})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        series = await fetch_array(array=_array(), api=OpenMeteoApiConfig())

    assert list(series) == [datetime(2026, 3, 30, 12, 0, tzinfo=timezone.utc)]


async def test_empty_estimate_raises_fetch_error() -> None:
    cls, _ = _mock_library({})
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=OpenMeteoApiConfig())


async def test_connection_error_becomes_fetch_error() -> None:
    cls, instance = _mock_library({})
    instance.estimate = AsyncMock(
        side_effect=OpenMeteoSolarForecastConnectionError("boom")
    )
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(FetchError):
            await fetch_array(array=_array(), api=OpenMeteoApiConfig())


async def test_ratelimit_error_carries_a_reset_time() -> None:
    cls, instance = _mock_library({})
    instance.estimate = AsyncMock(
        side_effect=OpenMeteoSolarForecastRatelimitError("slow down")
    )
    before = datetime.now(tz=timezone.utc)
    with patch("pv_openmeteo.fetcher.OpenMeteoSolarForecast", cls):
        with pytest.raises(RatelimitError) as excinfo:
            await fetch_array(array=_array(), api=OpenMeteoApiConfig())

    assert excinfo.value.reset_at > before
    assert isinstance(excinfo.value, FetchError)
