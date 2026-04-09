"""Tests for pv_ml_learner.meteoserver_fetcher.

Uses respx to mock httpx calls. No real API calls are made.
"""

from __future__ import annotations

import json
import pytest
import respx
import httpx


def _make_response(n_steps: int = 10) -> dict:
    """Build a synthetic Meteoserver API response with ``n_steps`` hourly steps."""
    base_ts = 1_775_000_000
    data = []
    for i in range(n_steps):
        data.append({
            "tijd": str(base_ts + i * 3600),
            "tijd_nl": "02-04-2026 16:00",
            "offset": str(i),
            "temp": "8",
            "winds": "4",
            "windb": "3",
            "windknp": "8",
            "windkmh": "14.4",
            "windr": "250",
            "windrltr": "ZW",
            "vis": "30000",
            "neersl": "0.5",
            "luchtd": "1017.3",
            "rv": "72",
            "gr": str(100 + i * 10),
            "gr_w": str(277 + i * 27),
            "hw": "60",
            "mw": "40",
            "lw": "10",
            "tw": "70",
            "cond": "2",
            "ico": "d2",
            "samenv": "Halfbewolkt",
        })
    return {"plaatsnaam": [{"plaats": "Groenekan"}], "data": data}


METEOSERVER_URL = "https://data.meteoserver.nl/api/uurverwachting.php"


class TestSuccessfulFetch:
    @respx.mock
    def test_parses_all_fields_correctly(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast

        payload = _make_response(n_steps=5)
        respx.get(METEOSERVER_URL).mock(
            return_value=httpx.Response(200, json=payload)
        )

        rows = fetch_meteoserver_forecast(
            api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
        )

        assert len(rows) == 5
        first = rows[0]
        assert first.step_ts == 1_775_000_000
        assert first.ghi_wm2 == pytest.approx(277.0)  # gr_w field (W/m²)
        assert first.temp_c == pytest.approx(8.0)
        assert first.wind_ms == pytest.approx(4.0)
        assert first.rain_mm == pytest.approx(0.5)
        assert first.cloud_pct == pytest.approx(70.0)

    @respx.mock
    def test_capped_at_horizon_hours(self) -> None:
        """Response with 54 steps is truncated to horizon_hours."""
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast

        payload = _make_response(n_steps=54)
        respx.get(METEOSERVER_URL).mock(
            return_value=httpx.Response(200, json=payload)
        )

        rows = fetch_meteoserver_forecast(
            api_key="test", latitude=52.1, longitude=5.2, horizon_hours=10
        )
        assert len(rows) == 10

    @respx.mock
    def test_step_ts_is_integer_of_tijd(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast

        payload = _make_response(n_steps=1)
        payload["data"][0]["tijd"] = "1775138400"
        respx.get(METEOSERVER_URL).mock(
            return_value=httpx.Response(200, json=payload)
        )

        rows = fetch_meteoserver_forecast(
            api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
        )
        assert rows[0].step_ts == 1_775_138_400


class TestErrorHandling:
    @respx.mock
    def test_http_401_raises_configuration_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, ConfigurationError

        respx.get(METEOSERVER_URL).mock(return_value=httpx.Response(401))
        with pytest.raises(ConfigurationError):
            fetch_meteoserver_forecast(
                api_key="bad", latitude=52.1, longitude=5.2, horizon_hours=48
            )

    @respx.mock
    def test_http_403_raises_configuration_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, ConfigurationError

        respx.get(METEOSERVER_URL).mock(return_value=httpx.Response(403))
        with pytest.raises(ConfigurationError):
            fetch_meteoserver_forecast(
                api_key="bad", latitude=52.1, longitude=5.2, horizon_hours=48
            )

    @respx.mock
    def test_http_429_raises_rate_limit_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, RatelimitError

        respx.get(METEOSERVER_URL).mock(return_value=httpx.Response(429))
        with pytest.raises(RatelimitError):
            fetch_meteoserver_forecast(
                api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
            )

    @respx.mock
    def test_http_500_raises_fetch_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, FetchError

        respx.get(METEOSERVER_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(FetchError):
            fetch_meteoserver_forecast(
                api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
            )

    @respx.mock
    def test_malformed_json_raises_fetch_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, FetchError

        respx.get(METEOSERVER_URL).mock(
            return_value=httpx.Response(200, content=b"not json")
        )
        with pytest.raises(FetchError):
            fetch_meteoserver_forecast(
                api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
            )

    @respx.mock
    def test_missing_data_key_raises_fetch_error(self) -> None:
        from pv_ml_learner.meteoserver_fetcher import fetch_meteoserver_forecast, FetchError

        respx.get(METEOSERVER_URL).mock(
            return_value=httpx.Response(200, json={"plaatsnaam": []})
        )
        with pytest.raises(FetchError):
            fetch_meteoserver_forecast(
                api_key="test", latitude=52.1, longitude=5.2, horizon_hours=48
            )
