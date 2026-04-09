"""Fetch long-term statistics from Home Assistant.

This module calls the HA recorder statistics API and returns the raw hourly
mean readings per entity. It has no forecast, config, or MQTT dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when the Home Assistant statistics API call fails.

    Callers should catch this, log the traceback, and leave the existing
    retained MQTT payload unchanged rather than publishing a partial forecast.
    """


async def fetch_statistics(
    *,
    url: str,
    token: str,
    entity_ids: list[str],
    lookback_days: int,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch hourly mean statistics for a set of HA entities.

    Queries the HA recorder statistics endpoint for all requested entities in a
    single HTTP call, covering the last lookback_days days from now.

    Args:
        url: Base URL of the HA instance (e.g. "http://homeassistant.local:8123").
        token: Long-Lived Access Token. Sent as a Bearer token in the
            Authorization header.
        entity_ids: List of entity IDs to fetch statistics for.
        lookback_days: Number of days of history to request. The window starts
            at midnight lookback_days days ago and ends now.

    Returns:
        Dict mapping entity ID to a list of hourly reading dicts. Each reading
        dict has at minimum a "start" key (ISO 8601 timestamp) and a "mean" key
        (numeric). Entities absent from the HA response are not included in the
        return value — callers should treat missing entities as having no data.

    Raises:
        FetchError: If the HTTP request fails, returns a non-2xx status, or the
            response cannot be parsed as JSON.
    """
    now = datetime.now(tz=timezone.utc)
    start_time = (now - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    endpoint = f"{url.rstrip('/')}/api/recorder/statistics_during_period"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = {
        "start_time": start_time.isoformat(),
        "statistic_ids": entity_ids,
        "period": "hour",
        "types": ["mean"],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(endpoint, json=body, headers=headers)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise FetchError(
            f"HA statistics API returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise FetchError(f"HA statistics request failed: {exc}") from exc
    except Exception as exc:
        raise FetchError(f"Unexpected error fetching HA statistics: {exc}") from exc
