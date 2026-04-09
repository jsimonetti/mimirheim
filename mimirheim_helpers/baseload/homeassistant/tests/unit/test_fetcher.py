"""Unit tests for baseload_ha.fetcher.

The httpx client is mocked so no real HTTP calls are made.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from baseload_ha.fetcher import FetchError, fetch_statistics


_NOW = datetime(2026, 3, 30, 14, 0, 0, tzinfo=timezone.utc)

# A minimal HA statistics_during_period response for two entities.
_HA_RESPONSE = {
    "sensor.power_l1_w": [
        {"start": "2026-03-29T13:00:00+00:00", "mean": 500.0},
        {"start": "2026-03-29T14:00:00+00:00", "mean": 600.0},
    ],
    "sensor.battery_w": [
        {"start": "2026-03-29T13:00:00+00:00", "mean": 100.0},
    ],
}


class TestFetchStatistics:
    async def test_returns_readings_per_entity(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = _HA_RESPONSE

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("baseload_ha.fetcher.httpx.AsyncClient", return_value=mock_client):
            result = await fetch_statistics(
                url="http://ha.local:8123",
                token="tok",
                entity_ids=["sensor.power_l1_w", "sensor.battery_w"],
                lookback_days=7,
            )

        assert "sensor.power_l1_w" in result
        assert len(result["sensor.power_l1_w"]) == 2
        assert result["sensor.power_l1_w"][0]["mean"] == 500.0

    async def test_raises_fetch_error_on_http_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "401", request=MagicMock(), response=MagicMock()
            )
        )

        with patch("baseload_ha.fetcher.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(FetchError):
                await fetch_statistics(
                    url="http://ha.local:8123",
                    token="tok",
                    entity_ids=["sensor.power_l1_w"],
                    lookback_days=7,
                )

    async def test_request_includes_authorization_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json.return_value = {}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("baseload_ha.fetcher.httpx.AsyncClient", return_value=mock_client):
            await fetch_statistics(
                url="http://ha.local:8123",
                token="secret-token",
                entity_ids=["sensor.p"],
                lookback_days=3,
            )

        call_kwargs = mock_client.post.call_args.kwargs
        headers = call_kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer secret-token"
