"""Transform Zonneplan API price data into the mimirheim price step format.

This module contains one public function, ``fetch_prices``, which calls the
Zonneplan API, applies the operator-configured import and export price formulas,
filters out past steps, and returns a sorted list of step dicts in the format
expected by the mimirheim prices input topic.

Output format per step:

    {
        "ts": "2026-05-28T10:00:00+00:00",    # ISO 8601 UTC start of hour
        "import_eur_per_kwh": 0.154619,        # after import_formula
        "export_eur_per_kwh": 0.0,             # after export_formula
        "confidence": 1.0                      # always 1.0 for Zonneplan
    }

The raw Zonneplan price integers use the scale: integer × 0.0000001 = EUR/kWh.

This module does not handle authentication or token management. Callers must
ensure the client's access token is valid before calling ``fetch_prices``.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from zonneplan_prices.api import FetchError, ZonneplanClient
from zonneplan_prices.config import get_export_fn, get_import_fn, ZonneplanApiConfig

logger = logging.getLogger(__name__)

# Zonneplan raw price scale: integer × _PRICE_SCALE = EUR/kWh.
# All integer price fields in the API response use this scale.
_PRICE_SCALE = 0.0000001


def fetch_prices(
    *,
    client: ZonneplanClient,
    connection_uuid: str,
    import_formula: str,
    export_formula: str,
) -> list[dict[str, Any]]:
    """Fetch price steps from Zonneplan and return the mimirheim-format list.

    Calls ``GET /connections/{connection_uuid}/summary``, converts each
    ``price_per_hour`` entry using the provided formulas, filters out steps that
    start before the current UTC hour, and returns the remainder sorted by ``ts``.

    Only steps at or after the start of the current 15-minute block are
    included. This ensures mimirheim always receives a forward-looking price
    horizon regardless of whether the API delivers hourly or quarterly steps.

    Args:
        client: A ZonneplanClient instance with a valid access token.
        connection_uuid: The electricity connection UUID to fetch prices for.
        import_formula: Python expression for the all-in import price. Variables:
            ``price`` (incl. tax, EUR/kWh), ``price_excl_tax`` (excl. tax,
            EUR/kWh), ``ts`` (step start datetime, UTC-aware).
        export_formula: Python expression for the net export price. Same
            variables as ``import_formula``.

    Returns:
        Sorted list of step dicts with keys ``ts``, ``import_eur_per_kwh``,
        ``export_eur_per_kwh``, and ``confidence``. An empty list is returned
        when no future steps are available.

    Raises:
        FetchError: On API failure.
    """
    # Build a temporary config to reuse the formula compiler / validator.
    api_config = ZonneplanApiConfig(
        import_formula=import_formula,
        export_formula=export_formula,
    )
    import_fn = get_import_fn(api_config)
    export_fn = get_export_fn(api_config)

    summary = client.get_summary(connection_uuid)
    raw_entries: list[dict] = summary.get("price_per_hour", [])

    _now = datetime.now(tz=timezone.utc)
    # Floor to the nearest 15-minute boundary so that already-completed
    # quarterly slots within the current hour are excluded, not just slots
    # from previous hours. For hourly data this is equivalent to the previous
    # replace(minute=0) because hourly entries are always at :00.
    _discard = timedelta(
        minutes=_now.minute % 15,
        seconds=_now.second,
        microseconds=_now.microsecond,
    )
    now = _now - _discard
    steps: list[dict[str, Any]] = []

    for entry in raw_entries:
        ts = datetime.fromisoformat(
            entry["datetime"].replace("Z", "+00:00")
        )
        # Exclude steps that have already started (before the current hour).
        if ts < now:
            continue

        price = entry["electricity_price"] * _PRICE_SCALE
        price_excl_tax = entry["electricity_price_excl_tax"] * _PRICE_SCALE

        import_price = import_fn(ts, price, price_excl_tax)
        export_price = export_fn(ts, price, price_excl_tax)

        steps.append({
            "ts": ts.isoformat(),
            "import_eur_per_kwh": import_price,
            "export_eur_per_kwh": export_price,
            "confidence": 1.0,
        })

    steps.sort(key=lambda s: s["ts"])
    logger.debug("Fetched %d price steps from Zonneplan.", len(steps))
    return steps
