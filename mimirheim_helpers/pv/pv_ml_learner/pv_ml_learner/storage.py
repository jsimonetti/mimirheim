"""Persistent storage for pv_ml_learner: schema, dataclasses, and repository functions.

This module owns the SQLite schema and all SQL queries. It uses SQLAlchemy Core
(not the ORM) for explicit control over time-series queries.

What this module does not do:
- It does not open its own connections or manage transactions. Callers always
  pass a ``sqlalchemy.Connection`` and are responsible for commit/rollback.
- It does not import from any other pv_ml_learner module.
- It does not perform any HTTP or MQTT operations.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass

import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class KnmiRow:
    """One hour of KNMI measured weather data.

    Attributes:
        hour_utc: Unix timestamp truncated to the start of the hour (UTC).
        station_id: KNMI station number.
        ghi_wm2: Global horizontal irradiance in W/m² (converted from J/cm²/h).
        wind_ms: Mean hourly wind speed in m/s. None if the station did not
            report this variable for the hour.
        temp_c: Temperature at 1.5m in °C. None if not reported.
        rain_mm: Precipitation in mm. 0.0 for trace amounts (KNMI RH == -1).
    """

    hour_utc: int
    station_id: int
    ghi_wm2: float
    wind_ms: float | None
    temp_c: float | None
    rain_mm: float


@dataclass
class McRow:
    """One hourly step from a Meteoserver forecast fetch.

    Attributes:
        step_ts: UTC Unix timestamp of this forecast step (from ``tijd``).
        ghi_wm2: Global horizontal irradiance in W/m² (from ``gr``).
        temp_c: Temperature in °C (from ``temp``).
        wind_ms: Wind speed in m/s (from ``winds``).
        rain_mm: Precipitation in mm (from ``neersl``).
        cloud_pct: Total cloud cover in % 0–100 (from ``tw``).
    """

    step_ts: int
    ghi_wm2: float
    temp_c: float
    wind_ms: float
    rain_mm: float
    cloud_pct: float


@dataclass
class PvActualRow:
    """One hour of measured PV production from Home Assistant.

    Attributes:
        array_name: Identifier for the PV array this row belongs to.
        hour_utc: Unix timestamp truncated to the start of the hour (UTC).
        kwh: Energy produced during this hour in kWh.
    """

    array_name: str
    hour_utc: int
    kwh: float


# ---------------------------------------------------------------------------
# SQLAlchemy table definitions
# ---------------------------------------------------------------------------

_metadata = sa.MetaData()

knmi_radiation = sa.Table(
    "knmi_radiation",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("hour_utc", sa.Integer, nullable=False, unique=True),
    sa.Column("station_id", sa.Integer, nullable=False),
    sa.Column("ghi_wm2", sa.Float, nullable=False),
    sa.Column("wind_ms", sa.Float, nullable=True),
    sa.Column("temp_c", sa.Float, nullable=True),
    sa.Column("rain_mm", sa.Float, nullable=False),
)

meteoserver_forecast = sa.Table(
    "meteoserver_forecast",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("fetch_ts", sa.Integer, nullable=False),
    sa.Column("step_ts", sa.Integer, nullable=False),
    sa.Column("ghi_wm2", sa.Float, nullable=False),
    sa.Column("temp_c", sa.Float, nullable=False),
    sa.Column("wind_ms", sa.Float, nullable=False),
    sa.Column("rain_mm", sa.Float, nullable=False),
    sa.Column("cloud_pct", sa.Float, nullable=False),
    sa.Index("ms_fetch", "fetch_ts"),
    sa.Index("ms_step", "step_ts"),
)

pv_actuals = sa.Table(
    "pv_actuals",
    _metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("array_name", sa.Text, nullable=False),
    sa.Column("hour_utc", sa.Integer, nullable=False),
    sa.Column("kwh", sa.Float, nullable=False),
    sa.UniqueConstraint("array_name", "hour_utc", name="uq_pv_actuals_array_hour"),
)


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def create_schema(conn: sa.Connection) -> None:
    """Create all tables if they do not already exist.

    Safe to call on every startup — uses ``CREATE TABLE IF NOT EXISTS``
    semantics via SQLAlchemy's ``checkfirst=True``.

    Args:
        conn: An open SQLAlchemy connection. The caller is responsible for
            committing after this call.
    """
    _metadata.create_all(conn, checkfirst=True)


# ---------------------------------------------------------------------------
# KNMI repository
# ---------------------------------------------------------------------------


def upsert_knmi_hours(conn: sa.Connection, rows: list[KnmiRow]) -> int:
    """Insert or replace KNMI hourly rows into ``knmi_radiation``.

    Uses SQLite's ``INSERT OR REPLACE`` semantics: if a row with the same
    ``hour_utc`` already exists it is overwritten. This is safe to call
    repeatedly on the same data.

    Args:
        conn: An open SQLAlchemy connection.
        rows: KNMI rows to upsert.

    Returns:
        The number of rows affected.
    """
    if not rows:
        return 0
    data = [
        {
            "hour_utc": r.hour_utc,
            "station_id": r.station_id,
            "ghi_wm2": r.ghi_wm2,
            "wind_ms": r.wind_ms,
            "temp_c": r.temp_c,
            "rain_mm": r.rain_mm,
        }
        for r in rows
    ]
    stmt = sa.text(
        "INSERT OR REPLACE INTO knmi_radiation "
        "(hour_utc, station_id, ghi_wm2, wind_ms, temp_c, rain_mm) "
        "VALUES (:hour_utc, :station_id, :ghi_wm2, :wind_ms, :temp_c, :rain_mm)"
    )
    result = conn.execute(stmt, data)
    return result.rowcount


def get_knmi_range(
    conn: sa.Connection, start_ts: int, end_ts: int
) -> list[KnmiRow]:
    """Return KNMI rows where ``start_ts <= hour_utc <= end_ts``.

    Args:
        conn: An open SQLAlchemy connection.
        start_ts: Inclusive start Unix timestamp.
        end_ts: Inclusive end Unix timestamp.

    Returns:
        List of matching ``KnmiRow`` objects ordered by ``hour_utc`` ascending.
    """
    stmt = (
        sa.select(knmi_radiation)
        .where(knmi_radiation.c.hour_utc >= start_ts)
        .where(knmi_radiation.c.hour_utc <= end_ts)
        .order_by(knmi_radiation.c.hour_utc)
    )
    return [
        KnmiRow(
            hour_utc=row.hour_utc,
            station_id=row.station_id,
            ghi_wm2=row.ghi_wm2,
            wind_ms=row.wind_ms,
            temp_c=row.temp_c,
            rain_mm=row.rain_mm,
        )
        for row in conn.execute(stmt)
    ]


def get_latest_knmi_ts(conn: sa.Connection) -> int | None:
    """Return the most recent ``hour_utc`` in ``knmi_radiation``, or None.

    Args:
        conn: An open SQLAlchemy connection.

    Returns:
        The maximum ``hour_utc`` value, or None if the table is empty.
    """
    result = conn.execute(
        sa.select(sa.func.max(knmi_radiation.c.hour_utc))
    ).scalar_one_or_none()
    return result


# ---------------------------------------------------------------------------
# Meteoserver repository
# ---------------------------------------------------------------------------


def insert_meteoserver_fetch(
    conn: sa.Connection, fetch_ts: int, rows: list[McRow]
) -> None:
    """Insert one Meteoserver forecast fetch into ``meteoserver_forecast``.

    Each call stores a complete forecast snapshot keyed by ``fetch_ts``.
    Old fetches are not removed here; use ``prune_meteoserver`` separately.

    Args:
        conn: An open SQLAlchemy connection.
        fetch_ts: UTC Unix timestamp of when the forecast was fetched.
        rows: The hourly forecast steps to store.
    """
    if not rows:
        return
    data = [
        {
            "fetch_ts": fetch_ts,
            "step_ts": r.step_ts,
            "ghi_wm2": r.ghi_wm2,
            "temp_c": r.temp_c,
            "wind_ms": r.wind_ms,
            "rain_mm": r.rain_mm,
            "cloud_pct": r.cloud_pct,
        }
        for r in rows
    ]
    conn.execute(meteoserver_forecast.insert(), data)


def get_latest_meteoserver_fetch(conn: sa.Connection) -> list[McRow] | None:
    """Return rows from the most recent Meteoserver fetch, or None.

    If multiple fetches exist, only the rows belonging to the highest
    ``fetch_ts`` are returned.

    Args:
        conn: An open SQLAlchemy connection.

    Returns:
        List of ``McRow`` from the latest fetch, ordered by ``step_ts``.
        None if no data has been stored yet.
    """
    max_fetch = conn.execute(
        sa.select(sa.func.max(meteoserver_forecast.c.fetch_ts))
    ).scalar_one_or_none()
    if max_fetch is None:
        return None
    stmt = (
        sa.select(meteoserver_forecast)
        .where(meteoserver_forecast.c.fetch_ts == max_fetch)
        .order_by(meteoserver_forecast.c.step_ts)
    )
    rows = [
        McRow(
            step_ts=row.step_ts,
            ghi_wm2=row.ghi_wm2,
            temp_c=row.temp_c,
            wind_ms=row.wind_ms,
            rain_mm=row.rain_mm,
            cloud_pct=row.cloud_pct,
        )
        for row in conn.execute(stmt)
    ]
    return rows if rows else None


def prune_meteoserver(conn: sa.Connection, keep_fetches: int = 10) -> None:
    """Remove all but the ``keep_fetches`` most recent Meteoserver fetches.

    This prevents unbounded growth of the ``meteoserver_forecast`` table.
    The most recent ``keep_fetches`` distinct ``fetch_ts`` values are retained;
    all others are deleted.

    Args:
        conn: An open SQLAlchemy connection.
        keep_fetches: Number of most-recent fetches to keep. Default 10.
    """
    # Find the cutoff: the (keep_fetches+1)-th most recent fetch_ts from the top.
    subq = (
        sa.select(meteoserver_forecast.c.fetch_ts)
        .distinct()
        .order_by(meteoserver_forecast.c.fetch_ts.desc())
        .limit(keep_fetches)
        .subquery()
    )
    conn.execute(
        meteoserver_forecast.delete().where(
            meteoserver_forecast.c.fetch_ts.notin_(sa.select(subq))
        )
    )


# ---------------------------------------------------------------------------
# PV actuals repository
# ---------------------------------------------------------------------------


def upsert_pv_actuals(conn: sa.Connection, rows: list[PvActualRow]) -> int:
    """Insert or replace PV actual rows into ``pv_actuals``.

    Uses SQLite's ``INSERT OR REPLACE`` semantics. The unique key is
    ``(array_name, hour_utc)``.

    Args:
        conn: An open SQLAlchemy connection.
        rows: PV actual rows to upsert.

    Returns:
        The number of rows affected.
    """
    if not rows:
        return 0
    data = [
        {"array_name": r.array_name, "hour_utc": r.hour_utc, "kwh": r.kwh}
        for r in rows
    ]
    stmt = sa.text(
        "INSERT OR REPLACE INTO pv_actuals (array_name, hour_utc, kwh) "
        "VALUES (:array_name, :hour_utc, :kwh)"
    )
    result = conn.execute(stmt, data)
    return result.rowcount


def get_pv_actuals_range(
    conn: sa.Connection, array_name: str, start_ts: int, end_ts: int
) -> list[PvActualRow]:
    """Return PV actuals for ``array_name`` where ``start_ts <= hour_utc <= end_ts``.

    Args:
        conn: An open SQLAlchemy connection.
        array_name: The array identifier to filter by.
        start_ts: Inclusive start Unix timestamp.
        end_ts: Inclusive end Unix timestamp.

    Returns:
        List of ``PvActualRow`` ordered by ``hour_utc`` ascending.
    """
    stmt = (
        sa.select(pv_actuals)
        .where(pv_actuals.c.array_name == array_name)
        .where(pv_actuals.c.hour_utc >= start_ts)
        .where(pv_actuals.c.hour_utc <= end_ts)
        .order_by(pv_actuals.c.hour_utc)
    )
    return [
        PvActualRow(array_name=row.array_name, hour_utc=row.hour_utc, kwh=row.kwh)
        for row in conn.execute(stmt)
    ]


def get_latest_actuals_ts(conn: sa.Connection, array_name: str) -> int | None:
    """Return the most recent ``hour_utc`` for ``array_name`` in ``pv_actuals``, or None.

    Args:
        conn: An open SQLAlchemy connection.
        array_name: The array identifier to filter by.

    Returns:
        The maximum ``hour_utc`` value for that array, or None if absent.
    """
    return conn.execute(
        sa.select(sa.func.max(pv_actuals.c.hour_utc)).where(
            pv_actuals.c.array_name == array_name
        )
    ).scalar_one_or_none()


def get_earliest_actuals_ts(conn: sa.Connection) -> int | None:
    """Return the oldest ``hour_utc`` across all arrays in ``pv_actuals``, or None.

    Used to align the KNMI fetch start with the beginning of the PV actuals
    history rather than an arbitrary lookback window. Because KNMI data is
    shared across all arrays, this queries the global minimum rather than a
    per-array minimum.

    Args:
        conn: An open SQLAlchemy connection.

    Returns:
        The minimum ``hour_utc`` value across all arrays, or None if the table
        is empty.
    """
    return conn.execute(
        sa.select(sa.func.min(pv_actuals.c.hour_utc))
    ).scalar_one_or_none()


def count_distinct_months(conn: sa.Connection, array_name: str) -> int:
    """Count the number of distinct calendar months in ``pv_actuals`` for one array.

    A month is identified by its position in the calendar (1-12). This is used
    to check whether the training dataset has adequate seasonal coverage.

    Args:
        conn: An open SQLAlchemy connection.
        array_name: The array identifier to filter by.

    Returns:
        The number of distinct calendar-month positions (1-12) represented.
    """
    rows = conn.execute(
        sa.select(pv_actuals.c.hour_utc).where(
            pv_actuals.c.array_name == array_name
        )
    ).scalars().all()
    months = {time.gmtime(ts).tm_mon for ts in rows}
    return len(months)
