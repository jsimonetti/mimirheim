"""Transform Zonneplan API price data into the mimirheim price step format.

This module contains one public function, ``fetch_prices``, which calls the
Zonneplan consumer-prices chart endpoint, applies the operator-configured
import and export price formulas, filters out steps that have already fully
elapsed, and returns a sorted list of step dicts in the format expected by
the mimirheim prices input topic.

Output format per step:

    {
        "ts": "2026-05-28T10:00:00+00:00",    # ISO 8601 UTC step start
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
from datetime import datetime, timezone
from typing import Any

from zonneplan_prices.api import ZonneplanClient
from zonneplan_prices.config import get_export_fn, get_import_fn, ZonneplanApiConfig

logger = logging.getLogger(__name__)

# Zonneplan raw price scale: integer × _PRICE_SCALE = EUR/kWh.
# All integer price fields in the API response use this scale.
_PRICE_SCALE = 0.0000001

# Maps the operator-facing price_interval config value to the Zonneplan API's
# chart_name path segment for GET /api/consumer-prices/charts/{chart_name}.
_CHART_NAMES: dict[str, str] = {
    "hourly": "electricity-hourly",
    "quarter_hourly": "electricity-quarter-hourly",
}


def fetch_prices(
    *,
    client: ZonneplanClient,
    price_interval: str,
    import_formula: str,
    export_formula: str,
) -> list[dict[str, Any]]:
    """Fetch price steps from Zonneplan and return the mimirheim-format list.

    Calls ``GET /api/consumer-prices/charts/{chart_name}`` (account-scoped —
    no connection UUID is needed), converts each price entry using the
    provided formulas, filters out steps whose interval has already fully
    elapsed, and returns the remainder sorted by ``ts``.

    An entry is included as long as its ``end_date`` is still in the future —
    i.e. its interval has not fully elapsed yet — regardless of how far "now"
    currently is into that interval. This intentionally does not use a fixed
    time-floor (e.g. truncating "now" to the current hour or 15-minute
    block): doing so can drop the only currently-available in-progress price
    entry before the next batch of data has arrived, leaving no price data at
    all for a period of time.

    Args:
        client: A ZonneplanClient instance with a valid access token.
        price_interval: ``"hourly"`` or ``"quarter_hourly"`` — selects which
            Zonneplan consumer-price chart to fetch.
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

    chart_name = _CHART_NAMES[price_interval]
    data = client.get_consumer_prices(chart_name)
    raw_entries: list[dict] = (
        data.get("chart", {}).get("series", {}).get("prices", [])
    )

    now = datetime.now(tz=timezone.utc)
    steps: list[dict[str, Any]] = []

    for entry in raw_entries:
        start = datetime.fromisoformat(entry["start_date"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(entry["end_date"].replace("Z", "+00:00"))
        # Exclude entries whose interval has already fully elapsed. Using the
        # entry's own end_date (rather than a fixed time-floor) means an
        # in-progress entry stays available for its entire duration, however
        # far "now" is into it — see the docstring above for why this matters.
        if end <= now:
            continue

        price = entry["price_tax_included"]["amount"] * _PRICE_SCALE
        price_excl_tax = entry["price_tax_excluded"]["amount"] * _PRICE_SCALE

        import_price = import_fn(start, price, price_excl_tax)
        export_price = export_fn(start, price, price_excl_tax)

        steps.append({
            "ts": start.isoformat(),
            "import_eur_per_kwh": import_price,
            "export_eur_per_kwh": export_price,
            "confidence": 1.0,
        })

    steps.sort(key=lambda s: s["ts"])
    logger.debug(
        "Fetched %d price steps from Zonneplan (%s).", len(steps), price_interval
    )
    return steps
