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


# Maps the operator-facing price_interval config value to the length of one
# published price step in minutes.
_INTERVAL_MINUTES: dict[str, int] = {
    "hourly": 60,
    "quarter_hourly": 15,
}


def _aggregate_to_interval(
    raw: list[tuple[datetime, float]],
    interval_minutes: int,
) -> list[tuple[datetime, float]]:
    """Average raw spot prices into buckets of ``interval_minutes``.

    Nordpool has quoted day-ahead prices on a 15-minute market time unit for
    most areas since October 2025. Suppliers whose contract bills a single
    dynamic price per whole hour derive that price as the arithmetic mean of
    the four quarter-hour prices in the hour, so aggregating here reproduces
    the price the meter is actually settled on.

    Buckets are aligned to the UTC clock: with ``interval_minutes=60`` the
    bucket start is the whole hour. Every CET and CEST offset is a whole number
    of hours, so a UTC-aligned bucket is also a local-clock-aligned bucket, and
    a DST transition simply produces a shorter or longer run of buckets rather
    than misaligning them.

    Averaging happens on the raw spot price, before the import and export
    formulas run. That ordering matters for any formula that is not a straight
    affine function of ``price``: the supplier prices the hourly average, so
    the formula must be applied to the hourly average and not the other way
    round. A bucket that is only partially covered — the tail of the horizon,
    or the leading edge after past periods were dropped — is averaged over
    whatever periods are present rather than discarded, so a short bucket still
    yields a usable price instead of a gap in the payload.

    Args:
        raw: Unsorted ``(start time, spot price in EUR/kWh)`` pairs.
        interval_minutes: Bucket length. Must divide 60. A value equal to or
            smaller than the source resolution leaves each period in a bucket
            of its own, which returns the input unchanged apart from sorting.

    Returns:
        Sorted ``(bucket start, mean spot price in EUR/kWh)`` pairs, one per
        bucket that contained at least one source period.
    """
    buckets: dict[datetime, list[float]] = {}
    for ts, price in raw:
        bucket_start = ts.replace(
            minute=(ts.minute // interval_minutes) * interval_minutes,
            second=0,
            microsecond=0,
        )
        buckets.setdefault(bucket_start, []).append(price)

    return [
        (bucket_start, sum(prices) / len(prices))
        for bucket_start, prices in sorted(buckets.items())
    ]


async def fetch_prices(
    *,
    area: str,
    import_formula: str,
    export_formula: str,
    price_interval: str = "quarter_hourly",
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
    EUR/kWh. Raw spot prices are then aggregated to ``price_interval`` (see
    ``_aggregate_to_interval``) and the import and export formulas are applied
    to the aggregated value to derive the all-in prices for the consumer.

    Args:
        area: Nordpool area code (e.g. "NO2", "NL", "SE3").
        import_formula: Python expression string for the all-in import price.
            Available variables: ``price`` (raw spot in EUR/kWh), ``ts`` (datetime UTC).
        export_formula: Python expression string for the net export price.
            Available variables: ``price`` (raw spot in EUR/kWh), ``ts`` (datetime UTC).
        price_interval: Length of one output step — ``"quarter_hourly"`` or
            ``"hourly"``. Nordpool quotes most areas on a 15-minute market time
            unit, which ``"quarter_hourly"`` passes through unchanged, while
            ``"hourly"`` averages each clock hour into a single step for
            suppliers that bill one dynamic price per hour.

    Returns:
        Sorted list of step dicts. Each dict has:
        - ts: ISO 8601 UTC timestamp for the start of the price period.
        - import_eur_per_kwh: All-in import price from import_formula.
        - export_eur_per_kwh: Net export price from export_formula.
        - confidence: Always 1.0 for confirmed day-ahead prices.

    Raises:
        FetchError: If the Nordpool API returns an error, times out, or the
            requested area is absent from the response.
        KeyError: If ``price_interval`` is not a recognised value. The config
            schema constrains it to one of the two, so this only fires on a
            direct call that bypasses config validation.
    """
    interval_minutes = _INTERVAL_MINUTES[price_interval]

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

    raw: list[tuple[datetime, float]] = []
    for day_data in data.entries.values():
        for entry in day_data.entries:
            if entry.start < now:
                # Skip periods that have already started or passed.
                continue
            if area not in entry.entry:
                raise FetchError(
                    f"Area '{area}' not found in Nordpool response. "
                    f"Available areas: {list(entry.entry.keys())}"
                )
            raw.append((entry.start, entry.entry[area] / 1000.0))

    steps: list[dict[str, Any]] = []
    for ts, price_eur_per_kwh in _aggregate_to_interval(raw, interval_minutes):
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

    return steps
