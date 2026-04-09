"""Fetches short-range hourly weather forecasts from the Meteoserver API.

This module provides a single public function, ``fetch_meteoserver_forecast``,
which performs one HTTPS request to the Meteoserver uurverwachting endpoint and
returns a list of ``McRow`` dataclass instances ready for storage.

It does not schedule fetches, write to the database, or publish MQTT messages.
Callers are responsible for passing correct credentials and for persisting the
returned rows via ``pv_ml_learner.storage``.
"""

from __future__ import annotations

import logging
import httpx

from pv_ml_learner.storage import McRow

logger = logging.getLogger(__name__)

_API_URL = "https://data.meteoserver.nl/api/uurverwachting.php"


class FetchError(RuntimeError):
    """Raised for transient retrieval failures: HTTP 5xx, network errors, or
    unexpected response bodies.  The operation is safe to retry after a delay.
    """


class RatelimitError(FetchError):
    """Raised when the API returns HTTP 429.  Back off before retrying."""


class ConfigurationError(RuntimeError):
    """Raised for permanent configuration failures: invalid or unauthorised API
    key (HTTP 401 or 403).  Retrying will not help without fixing the key.
    """


def fetch_meteoserver_forecast(
    api_key: str,
    latitude: float,
    longitude: float,
    horizon_hours: int,
) -> list[McRow]:
    """Fetch hourly weather forecast steps from the Meteoserver API.

    Sends a single GET request to the Meteoserver uurverwachting endpoint and
    parses the response into ``McRow`` dataclass instances.  The result is
    truncated to at most ``horizon_hours`` steps.

    Args:
        api_key: Meteoserver API key.  Must be registered and active.
        latitude: Site latitude in decimal degrees.
        longitude: Site longitude in decimal degrees.
        horizon_hours: Maximum number of hourly steps to return.  Excess steps
            from the API response are discarded.

    Returns:
        A list of ``McRow`` instances in ascending ``step_ts`` order, containing
        at most ``horizon_hours`` entries.

    Raises:
        ConfigurationError: If the API returns HTTP 401 or 403, indicating an
            invalid or unauthorised API key.
        RatelimitError: If the API returns HTTP 429.
        FetchError: For HTTP 5xx responses, network-level errors, malformed JSON,
            or a response body that does not contain the expected ``data`` key.
    """
    params = {"lat": str(latitude), "long": str(longitude), "key": api_key}

    try:
        response = httpx.get(_API_URL, params=params)
    except httpx.RequestError as exc:
        raise FetchError(f"Network error contacting Meteoserver: {exc}") from exc

    if response.status_code in (401, 403):
        raise ConfigurationError(
            f"Meteoserver API key rejected (HTTP {response.status_code})."
        )
    if response.status_code == 429:
        raise RatelimitError("Meteoserver rate limit exceeded (HTTP 429).")
    if response.status_code >= 400:
        raise FetchError(
            f"Meteoserver returned unexpected HTTP {response.status_code}."
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise FetchError(
            f"Meteoserver response is not valid JSON: {exc}"
        ) from exc

    if "data" not in payload:
        raise FetchError(
            "Meteoserver response does not contain the expected 'data' key."
        )

    steps: list[dict] = payload["data"][:horizon_hours]
    rows: list[McRow] = []
    for step in steps:
        rows.append(
            McRow(
                step_ts=int(step["tijd"]),
                ghi_wm2=float(step["gr_w"]),  # W/m² — matches KNMI training unit
                temp_c=float(step["temp"]),
                wind_ms=float(step["winds"]),
                rain_mm=float(step["neersl"]),
                cloud_pct=float(step["tw"]),
            )
        )

    logger.debug("Fetched %d Meteoserver forecast steps.", len(rows))
    return rows
