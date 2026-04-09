"""KNMI hourly observations fetcher for pv_ml_learner.

Fetches historical hourly weather data from KNMI via the knmi-py library and
converts it into ``KnmiRow`` objects ready for storage.

What this module does not do:
- It does not write to the database. Callers pass results to
  ``storage.upsert_knmi_hours``.
- It does not manage connections, transactions, or retry loops.
- It does not import from any other pv_ml_learner module except ``storage``
  (for the ``KnmiRow`` dataclass).
"""

from __future__ import annotations

import calendar
import logging
from datetime import datetime, timezone

import pandas as pd

import knmi

from pv_ml_learner.storage import KnmiRow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class FetchError(Exception):
    """Raised when a KNMI fetch fails due to a network or HTTP error.

    This error is retriable: the caller should log it and try again on the
    next scheduled ingest cycle. Do not retry inside this module.
    """


class ConfigurationError(Exception):
    """Raised when the KNMI station ID is not recognised or the configuration
    is otherwise invalid.

    This error is not retriable without a configuration change.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# KNMI missing-value sentinel for most variables (wind, temperature, etc.).
_KNMI_MISSING = -9999

# KNMI RH == -1 means trace precipitation (< 0.05 mm). Store as 0.0.
_KNMI_RH_TRACE = -1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """Return the column whose stripped name matches ``name``.

    knmi-py returns columns with leading/trailing spaces from the KNMI CSV
    header (e.g. ``"   Q"`` instead of ``"Q"``). Stripping handles this.

    Args:
        df: DataFrame returned by knmi-py.
        name: Column name without whitespace (e.g. ``"Q"``).

    Returns:
        The matching pandas Series.

    Raises:
        KeyError: If no column with that stripped name exists.
    """
    mapping = {c.strip(): c for c in df.columns}
    return df[mapping[name]]


def fetch_knmi_hours(
    station_id: int,
    start_ts: int,
    end_ts: int,
) -> list[KnmiRow]:
    """Fetch KNMI hourly observations for a station and time window.

    Calls ``knmi.get_hour_data_dataframe`` for the given station, converts
    the four weather variables to final units, drops rows with missing
    radiation (Q == -1), and returns a list of ``KnmiRow`` objects.

    Callers are responsible for determining the appropriate ``start_ts`` and
    ``end_ts``. The KNMI publication delay (24–48 hours) means recent data
    may be absent; callers should request data only up to ``now - 48h``.

    Args:
        station_id: KNMI station number (e.g. 260 for De Bilt).
        start_ts: Start of the window as a UTC Unix timestamp (inclusive).
        end_ts: End of the window as a UTC Unix timestamp (inclusive).

    Returns:
        List of ``KnmiRow`` with converted values. Rows where Q == -1
        (missing radiation) are excluded. May be empty.

    Raises:
        FetchError: If knmi-py raises any exception (network or HTTP error).
        ConfigurationError: If the station ID produces no data for any period
            (typically indicates an unrecognised station ID).
    """
    start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(end_ts, tz=timezone.utc)

    # knmi-py expects "YYYYMMDDHH" strings (hour in 1–24 original KNMI format,
    # but the library accepts 0-based hours in its start/end strings too).
    start_str = start_dt.strftime("%Y%m%d%H")
    end_str = end_dt.strftime("%Y%m%d%H")

    try:
        df = knmi.get_hour_data_dataframe(
            stations=[station_id],
            start=start_str,
            end=end_str,
            variables=["Q", "FH", "T", "RH"],
        )
    except Exception as exc:
        raise FetchError(f"KNMI fetch failed for station {station_id}: {exc}") from exc

    if df.empty:
        return []

    rows: list[KnmiRow] = []
    dropped = 0

    q_series = _col(df, "Q")
    fh_series = _col(df, "FH")
    t_series = _col(df, "T")
    rh_series = _col(df, "RH")

    # STN is the station column — use its stripped values to get the station number.
    try:
        stn_series = _col(df, "STN")
    except KeyError:
        # Some responses omit the STN column when only one station is requested.
        stn_series = None

    for idx_ts, q_raw, fh_raw, t_raw, rh_raw in zip(
        df.index, q_series, fh_series, t_series, rh_series
    ):
        q_val = int(q_raw) if not pd.isna(q_raw) else _KNMI_MISSING

        # Drop rows where radiation is missing — they are unusable for training
        # and cannot be used for night detection.
        if q_val == -1:
            dropped += 1
            continue

        ghi_wm2 = q_val * 10_000 / 3_600

        fh_val = int(fh_raw) if not pd.isna(fh_raw) else _KNMI_MISSING
        wind_ms: float | None = None if fh_val == _KNMI_MISSING else fh_val * 0.1

        t_val = int(t_raw) if not pd.isna(t_raw) else _KNMI_MISSING
        temp_c: float | None = None if t_val == _KNMI_MISSING else t_val * 0.1

        rh_val = int(rh_raw) if not pd.isna(rh_raw) else _KNMI_MISSING
        # RH == -1 means trace precipitation (< 0.05 mm); treat as 0.0.
        rain_mm = 0.0 if rh_val == _KNMI_RH_TRACE else rh_val * 0.1

        # Convert the UTC-naive pandas Timestamp (representing UTC) to a Unix
        # timestamp truncated to the hour.
        hour_utc = int(calendar.timegm(idx_ts.timetuple()))

        stn = int(stn_series.iloc[len(rows) + dropped]) if stn_series is not None else station_id

        rows.append(KnmiRow(
            hour_utc=hour_utc,
            station_id=stn,
            ghi_wm2=ghi_wm2,
            wind_ms=wind_ms,
            temp_c=temp_c,
            rain_mm=rain_mm,
        ))

    if dropped:
        logger.info(
            "KNMI ingest: dropped %d row(s) with missing radiation (Q == -1) "
            "for station %d",
            dropped, station_id,
        )

    return rows
