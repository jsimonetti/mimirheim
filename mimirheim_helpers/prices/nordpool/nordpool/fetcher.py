"""Fetch day-ahead electricity prices from the Nordpool data portal.

This module wraps the pynordpool library and returns a list of normalised
price step dicts ready for publishing. It has no MQTT or config dependencies.

It does not handle scheduling, MQTT, or file I/O.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from pynordpool import NordPoolClient
from pynordpool.const import Currency
from pynordpool.exceptions import NordPoolError

from nordpool.config import _compile_formula

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when the Nordpool API call fails unrecoverably.

    Callers should catch this, log it, and leave the existing retained MQTT
    payload unchanged rather than publishing a partial or empty payload.
    """


async def fetch_prices(
    *,
    area: str,
    import_formula: str,
    export_formula: str,
) -> list[dict[str, Any]]:
    """Fetch day-ahead prices for today and tomorrow where available.

    A single API request is made for both calendar days. If tomorrow's prices
    have not yet been published by Nordpool, the call silently returns today's
    prices only — no special configuration is required to handle this case.

    Only steps whose start time is at or after the current UTC hour are returned.
    This means a midnight trigger yields a full 24-hour (or 48-hour) payload,
    while an afternoon trigger yields only the remaining hours of the day plus
    all of tomorrow (if available).

    Prices are fetched in EUR and divided by 1000 to convert from EUR/MWh to
    EUR/kWh. The import and export formulas are then applied to derive the
    all-in prices for the consumer.

    Args:
        area: Nordpool area code (e.g. "NO2", "NL", "SE3").
        import_formula: Python expression string for the all-in import price.
            Available variables: ``price`` (raw spot in EUR/kWh), ``ts`` (datetime UTC).
        export_formula: Python expression string for the net export price.
            Available variables: ``price`` (raw spot in EUR/kWh), ``ts`` (datetime UTC).

    Returns:
        Sorted list of step dicts. Each dict has:
        - ts: ISO 8601 UTC timestamp for the start of the price period.
        - import_eur_per_kwh: All-in import price from import_formula.
        - export_eur_per_kwh: Net export price from export_formula.
        - confidence: Always 1.0 for confirmed day-ahead prices.

    Raises:
        FetchError: If the Nordpool API returns an error, times out, or the
            requested area is absent from the response.
    """
    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0)
    tomorrow = today + timedelta(days=1)

    import_fn = _compile_formula(import_formula)
    export_fn = _compile_formula(export_formula)

    try:
        async with aiohttp.ClientSession() as session:
            client = NordPoolClient(session=session)
            data = await client.async_get_delivery_periods(
                dates=[today, tomorrow],
                currency=Currency.EUR,
                areas=[area],
            )
    except NordPoolError as exc:
        raise FetchError(f"Nordpool API error: {exc}") from exc

    steps: list[dict[str, Any]] = []
    for day_data in data.entries:
        for entry in day_data.entries:
            if entry.start < now:
                # Skip hours that have already started or passed.
                continue
            if area not in entry.entry:
                raise FetchError(
                    f"Area '{area}' not found in Nordpool response. "
                    f"Available areas: {list(entry.entry.keys())}"
                )
            ts: datetime = entry.start
            price_eur_per_kwh = entry.entry[area] / 1000.0
            import_price = import_fn(ts, price_eur_per_kwh)
            export_price = export_fn(ts, price_eur_per_kwh)
            steps.append(
                {
                    "ts": ts.isoformat(),
                    "import_eur_per_kwh": round(import_price, 6),
                    "export_eur_per_kwh": round(export_price, 6),
                    "confidence": 1.0,
                }
            )

    return sorted(steps, key=lambda s: s["ts"])

    """Fetch day-ahead prices for today and tomorrow where available.

    A single API request is made for both calendar days. If tomorrow's prices
    have not yet been published by Nordpool, the call silently returns today's
    prices only — no special configuration is required to handle this case.

    Only steps whose start time is at or after the current UTC hour are returned.
    This means a midnight trigger yields a full 24-hour (or 48-hour) payload,
    while an afternoon trigger yields only the remaining hours of the day plus
    all of tomorrow (if available).

    Prices are fetched in EUR and divided by 1000 to convert from EUR/MWh to
    EUR/kWh. The vat_multiplier and grid tariffs are applied after conversion.

    Args:
        area: Nordpool area code (e.g. "NO2", "NL", "SE3").
        vat_multiplier: Multiplier applied to the raw spot price. 1.0 means
            no markup; 1.25 adds 25 %.
        grid_tariff_import_eur_per_kwh: Flat import network tariff in EUR/kWh,
            added to every step's import price.
        grid_tariff_export_eur_per_kwh: Flat export network tariff in EUR/kWh,
            subtracted from every step's export price.

    Returns:
        Sorted list of step dicts. Each dict has:
        - ts: ISO 8601 UTC timestamp for the start of the price period.
        - import_eur_per_kwh: All-in import price including VAT and tariff.
        - export_eur_per_kwh: All-in export price including VAT minus tariff.
        - confidence: Always 1.0 for confirmed day-ahead prices.

    Raises:
        FetchError: If the Nordpool API returns an error, times out, or the
            requested area is absent from the response.
    """
    now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    today = now.replace(hour=0)
    tomorrow = today + timedelta(days=1)

    try:
        async with aiohttp.ClientSession() as session:
            client = NordPoolClient(session=session)
            data = await client.async_get_delivery_periods(
                dates=[today, tomorrow],
                currency=Currency.EUR,
                areas=[area],
            )
    except NordPoolError as exc:
        raise FetchError(f"Nordpool API error: {exc}") from exc

    steps: list[dict[str, Any]] = []
    for day_data in data.entries:
        for entry in day_data.entries:
            if entry.start < now:
                # Skip hours that have already started or passed.
                continue
            if area not in entry.entry:
                raise FetchError(
                    f"Area '{area}' not found in Nordpool response. "
                    f"Available areas: {list(entry.entry.keys())}"
                )
            price_eur_per_kwh = entry.entry[area] / 1000.0
            import_price = (
                price_eur_per_kwh * vat_multiplier + grid_tariff_import_eur_per_kwh
            )
            export_price = (
                price_eur_per_kwh * vat_multiplier - grid_tariff_export_eur_per_kwh
            )
            steps.append(
                {
                    "ts": entry.start.isoformat(),
                    "import_eur_per_kwh": round(import_price, 6),
                    "export_eur_per_kwh": round(export_price, 6),
                    "confidence": 1.0,
                }
            )

    return sorted(steps, key=lambda s: s["ts"])
