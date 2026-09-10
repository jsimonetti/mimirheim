"""Fetch EPEX day-ahead spot price predictions from the EpexPredictor API.

This module wraps ``GET /prices`` on the EpexPredictor API
(https://epexpredictor.batzill.com) and returns raw (timestamp, price) pairs
plus the API's real/predicted boundary. It has no MQTT, config, or confidence
dependencies.

It does not handle scheduling, MQTT, or file I/O.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when the EpexPredictor API call fails unrecoverably.

    Callers should catch this, log it, and leave the existing retained MQTT
    payload unchanged rather than publishing a partial or empty payload.
    """


@dataclass
class FetchResult:
    """Raw prices and the API's real/predicted boundary for one fetch.

    Attributes:
        steps: Sorted (UTC-aware step start time, price in EUR/kWh) pairs.
        known_until: The API's ``knownUntil`` timestamp (UTC-aware): steps at
            or before this instant are real EPEX auction data, steps after
            it are the model's own prediction.
    """

    steps: list[tuple[datetime, float]]
    known_until: datetime


def fetch_raw_prices(
    *,
    area: str,
    horizon_hours: int,
    price_interval: str,
    base_url: str,
) -> FetchResult:
    """Fetch raw predicted spot prices from EpexPredictor.

    Always requests unit=EUR_PER_KWH and timezone=UTC, and always sends
    surcharge=0.0 and taxPercent=0.0 regardless of any caller input, since
    this helper has no config surface for either: markup and tax belong
    exclusively in import_formula / export_formula.

    Only steps at or after the current UTC hour are returned, matching the
    Nordpool helper's truncation behaviour, applied defensively in addition
    to whatever window the API itself returns by default. This truncation
    never drops ``known_until`` itself: it is read from the response's
    ``knownUntil`` field independently of the (possibly truncated) ``prices``
    list.

    Args:
        area: EPEX bidding zone code.
        horizon_hours: Hours ahead to request (the API's ``hours`` param).
        price_interval: "quarter_hourly" or "hourly"; maps to the API's
            ``hourly`` boolean.
        base_url: API base URL, public or self-hosted. Requests are made
            against ``f"{base_url}/prices"``.

    Returns:
        A ``FetchResult`` with the sorted price steps and ``known_until``.

    Raises:
        FetchError: On HTTP failure, network failure, or a malformed
            response body (missing "prices" or "knownUntil" key, unparsable
            timestamp).
    """
    params = {
        "hours": horizon_hours,
        "region": area,
        "unit": "EUR_PER_KWH",
        "hourly": price_interval == "hourly",
        "timezone": "UTC",
        "surcharge": 0.0,
        "taxPercent": 0.0,
    }

    try:
        resp = requests.get(f"{base_url}/prices", params=params, timeout=15)
        if resp.status_code >= 300:
            raise FetchError(f"GET /prices returned HTTP {resp.status_code}")
        body = resp.json()
    except FetchError:
        raise
    except Exception as exc:
        raise FetchError(f"GET /prices failed: {exc}") from exc

    try:
        raw_prices = body["prices"]
        known_until = datetime.fromisoformat(body["knownUntil"]).astimezone(timezone.utc)
    except (KeyError, ValueError) as exc:
        raise FetchError(f"Malformed EpexPredictor response: {exc}") from exc

    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)

    steps: list[tuple[datetime, float]] = []
    for entry in raw_prices:
        ts = datetime.fromisoformat(entry["startsAt"]).astimezone(timezone.utc)
        if ts < now:
            # Skip periods that have already started or passed.
            continue
        steps.append((ts, entry["total"]))
    steps.sort(key=lambda pair: pair[0])

    return FetchResult(steps=steps, known_until=known_until)
