"""Fetch long-term statistics from the Home Assistant recorder database.

This module queries the Home Assistant recorder database via SQLAlchemy and
returns hourly kWh/h readings per entity. It supports any database backend
that HA supports: SQLite (the default), PostgreSQL (via psycopg2 or asyncpg),
and MariaDB/MySQL (via pymysql).

The connection is specified as a SQLAlchemy URL string, for example:

    sqlite:////config/home-assistant_v2.db
    postgresql+psycopg2://user:pass@localhost/homeassistant
    mysql+pymysql://user:pass@localhost/homeassistant

The Home Assistant recorder writes long-term hourly statistics to two tables:

``statistics_meta``
    Maps human-readable entity IDs (``statistic_id`` column) to integer
    primary keys (``id`` column). Also stores the unit of measurement for
    each entity in ``unit_of_measurement``.

``statistics``
    One row per entity per hour. The columns used here are ``metadata_id``
    (foreign key into ``statistics_meta``), ``start_ts`` (Unix timestamp of
    the hour bucket as a float), ``mean`` (average value for that hour, used
    for power sensors), and ``sum`` (cumulative counter, used for energy
    sensors that use HA's ``total_increasing`` state class).

Sensor type detection
---------------------
The column used depends on the entity's unit of measurement, resolved via
``statistics_meta.unit_of_measurement`` or a caller-supplied override:

- **Power sensors** (units: W, kW, MW, GW): read ``mean``, convert to
  kWh/h using the appropriate multiplier.
- **Energy sensors** (units: Wh, kWh, MWh): read ``sum``, compute consecutive
  differences to obtain the energy consumed per hourly bucket, convert to kWh.

Outlier detection
-----------------
After extracting values, each entity's readings are passed through a P95-based
outlier filter before being returned. Any reading whose absolute value exceeds
``P95_effective * outlier_factor`` is dropped and logged at WARNING level.

This module has no forecast, config, or MQTT dependencies.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class FetchError(Exception):
    """Raised when the database query fails.

    Callers should catch this, log the traceback, and leave the existing
    retained MQTT payload unchanged rather than publishing a partial forecast.
    """


# ---------------------------------------------------------------------------
# Unit tables
# ---------------------------------------------------------------------------

# Multipliers to convert power-unit mean values to kWh/h.
# A sensor reporting in watts has a mean power P W over the hour, which
# corresponds to P * 1e-3 kWh of energy consumed in that hour (P W × 1 h / 1000).
_POWER_UNIT_TO_KWH_PER_H: dict[str, float] = {
    "W": 1e-3,
    "kW": 1.0,
    "MW": 1e3,
    "GW": 1e6,
}

# Multipliers to convert energy-unit sum deltas to kWh.
# A sensor reporting in Wh has a delta D Wh over the hour = D / 1000 kWh.
_ENERGY_UNIT_TO_KWH: dict[str, float] = {
    "Wh": 1e-3,
    "kWh": 1.0,
    "MWh": 1e3,
}


def _sensor_type(unit: str) -> str:
    """Return the sensor type string for the given unit.

    Args:
        unit: Unit of measurement string, e.g. ``"W"``, ``"kWh"``.

    Returns:
        ``"power"`` for power units, ``"energy"`` for energy units.

    Raises:
        FetchError: If the unit is not in either table.
    """
    if unit in _POWER_UNIT_TO_KWH_PER_H:
        return "power"
    if unit in _ENERGY_UNIT_TO_KWH:
        return "energy"
    raise FetchError(
        f"Unit {unit!r} is not a recognised power or energy unit. "
        f"Supported power units: {sorted(_POWER_UNIT_TO_KWH_PER_H)}. "
        f"Supported energy units: {sorted(_ENERGY_UNIT_TO_KWH)}."
    )


def _compute_deltas(
    rows: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Compute per-hour energy consumed from a list of cumulative sum rows.

    Args:
        rows: List of (start_ts, sum_value) tuples in ascending timestamp order.
            The first row is the pre-window seed row used to compute the first
            delta; it is not included in the output.

    Returns:
        List of (start_ts, delta_kwh) tuples for each hour in the window.
        Rows with a negative delta are silently dropped; they indicate recorder
        data corruption and the delta cannot be trusted. Rows with a zero delta
        are retained (the sensor was genuinely idle for that hour).
    """
    result: list[tuple[float, float]] = []
    for i in range(1, len(rows)):
        ts = rows[i][0]
        delta = rows[i][1] - rows[i - 1][1]
        if delta < 0:
            # The cumulative sum decreased, which HA should not permit for
            # total_increasing sensors. Treat as a corrupt row and discard.
            logger.debug(
                "Discarding negative energy delta %.3f at ts %.0f (row %d → %d).",
                delta, ts, i - 1, i,
            )
            continue
        result.append((ts, delta))
    return result


def _detect_outliers(
    values: list[tuple[float, float]],
    outlier_factor: float,
    entity_id: str,
) -> list[tuple[float, float]]:
    """Filter outliers from a list of (start_ts, value) pairs using P99 detection.

    The algorithm:

    1. If fewer than 24 valid samples exist, skip detection entirely. With
       fewer than one full day of data the statistical threshold would be
       unreliable, and mis-filtering a small dataset could introduce more
       error than it removes. A WARNING is logged to alert the operator.
    2. Compute the 99th percentile (P99) of all values. P99 is used rather
       than P95 because sensors with infrequent high-consumption cycles (e.g.
       a washing machine idle at 0.5 Wh/h but consuming 1 kWh/h during a wash)
       can have P95 fall deep in the idle range, causing legitimate active
       readings to be mis-flagged. P99 is still robust: with 1344 readings,
       genuinely corrupt values represent <0.15% of the data and do not
       influence P99.
    3. Apply a zero-inflation guard: if P99 is 0.0 (device idle ≥99% of the
       time), substitute the P99 of the non-zero values. This prevents a
       threshold of 0 from incorrectly flagging all non-zero readings.
    4. Compute threshold = P99_effective * outlier_factor.
    5. Drop any reading whose value exceeds the threshold.

    Args:
        values: List of (start_ts, value) tuples. ``value`` is the kWh/h or
            kWh delta already converted from the sensor's native unit.
        outlier_factor: Threshold multiplier applied to P99_effective.
        entity_id: Entity ID used in log messages.

    Returns:
        Filtered list of (start_ts, value) pairs with outliers removed.
    """
    n = len(values)
    if n < 24:
        if n > 0:
            logger.warning(
                "Entity %s has only %d sample(s) — skipping outlier detection.",
                entity_id, n,
            )
        return values

    raw = sorted(v for _, v in values)
    # Compute the 99th percentile via nearest-rank method.
    p99_idx = max(0, int(0.99 * n) - 1)
    p99 = raw[p99_idx]

    # Zero-inflation guard: if P99 == 0, the device is idle ≥99% of the time.
    # Use the P99 of the non-zero values to set a sensible threshold. The max
    # is not used because it would include the very outlier being detected.
    if p99 == 0.0:
        non_zero = sorted(v for v in raw if v > 0.0)
        if non_zero:
            p99_nz_idx = max(0, int(0.99 * len(non_zero)) - 1)
            p99 = non_zero[p99_nz_idx]

    threshold = p99 * outlier_factor

    filtered: list[tuple[float, float]] = []
    for ts, v in values:
        if v > threshold:
            logger.warning(
                "Outlier dropped for %s at ts %.0f: value=%.4f kWh/h, threshold=%.4f kWh/h "
                "(P99_effective=%.4f, factor=%.1f).",
                entity_id, ts, v, threshold, p99, outlier_factor,
            )
        else:
            filtered.append((ts, v))

    return filtered


def fetch_statistics(
    *,
    db_url: str,
    entity_ids: list[str],
    lookback_days: int,
    unit_overrides: dict[str, str] | None = None,
    outlier_factor: float = 10.0,
    outlier_factors: dict[str, float] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch hourly statistics for a set of HA entities and return kWh/h readings.

    For each entity the function:

    1. Determines the working unit from ``unit_overrides[entity_id]`` if
       present, otherwise from ``statistics_meta.unit_of_measurement``.
    2. Detects whether the entity is a power sensor (mean-based) or an energy
       sensor (sum-delta-based) from the working unit.
    3. Fetches the appropriate column (``mean`` for power, ``sum`` for energy).
       For energy sensors one extra row before the window start is fetched to
       seed the first delta.
    4. Applies ``_compute_deltas`` for energy sensors; converts raw values to
       kWh/h using the appropriate unit multiplier.
    5. Runs ``_detect_outliers`` on the converted values. The effective factor
       is ``outlier_factors[entity_id]`` if that dict is provided and contains
       the entity ID, otherwise ``outlier_factor``.
    6. Returns a uniform list of ``{"start": ISO string, "mean": kWh_value}``
       dicts for each entity.

    A new engine is created on every call because this function is invoked at
    most once per trigger cycle (typically once per day). Holding a persistent
    connection pool would gain very little while complicating process lifecycle.

    The query uses only standard SQL (SELECT, JOIN, WHERE), which runs without
    modification on SQLite, PostgreSQL, and MariaDB.

    Args:
        db_url: SQLAlchemy database URL, e.g.
            ``sqlite:////config/home-assistant_v2.db`` or
            ``postgresql+psycopg2://user:pass@host/homeassistant``.
        entity_ids: List of HA statistic IDs to fetch. These are the same
            strings shown in the HA UI as entity IDs, e.g.
            ``sensor.kitchen_power``.
        lookback_days: Number of days of history to request. The window starts
            at midnight UTC ``lookback_days`` days ago and ends now.
        unit_overrides: Optional mapping of entity ID to unit string. When an
            entity ID appears here, this unit is used instead of the value
            stored in ``statistics_meta``. Useful when the database unit is
            incorrect or missing.
        outlier_factor: Default threshold multiplier applied to P95_effective
            for all entities that are not present in ``outlier_factors``.
            Default 10.0 is appropriate for most residential sensors.
        outlier_factors: Optional per-entity outlier factor overrides. When an
            entity ID is present here, its value is used instead of the global
            ``outlier_factor``. Intended for callers who read per-entity config.

    Returns:
        Dict mapping each entity ID to a list of hourly reading dicts. Each
        reading dict has a ``"start"`` key (ISO 8601 string, UTC) and a
        ``"mean"`` key (float, in kWh/h). Entity IDs that have no rows in the
        database are returned as empty lists.

    Raises:
        FetchError: If the engine cannot be created (e.g. missing driver),
            the database cannot be opened, a SQL query fails, or an entity's
            unit is not recognised.
    """
    now = datetime.now(tz=timezone.utc)
    start_time = (now - timedelta(days=lookback_days)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_ts: float = start_time.timestamp()

    try:
        engine = create_engine(db_url)
    except Exception as exc:
        raise FetchError(f"Cannot create database engine for {db_url!r}: {exc}") from exc

    result: dict[str, list[dict[str, Any]]] = {eid: [] for eid in entity_ids}

    # Query the unit and mean column for power sensors.
    _unit_query = text(
        "SELECT unit_of_measurement FROM statistics_meta WHERE statistic_id = :entity_id"
    )
    _power_query = text(
        """
        SELECT s.start_ts, s.mean
          FROM statistics s
          JOIN statistics_meta sm ON s.metadata_id = sm.id
         WHERE sm.statistic_id = :entity_id
           AND s.start_ts >= :start_ts
           AND s.mean IS NOT NULL
         ORDER BY s.start_ts
        """
    )
    # For energy sensors, fetch sum column including one extra row before the
    # window start to seed the first consecutive difference. The extra row
    # uses the largest start_ts that is strictly less than start_ts.
    _energy_query = text(
        """
        SELECT s.start_ts, s.sum
          FROM statistics s
          JOIN statistics_meta sm ON s.metadata_id = sm.id
         WHERE sm.statistic_id = :entity_id
           AND s.sum IS NOT NULL
           AND s.start_ts >= (
               SELECT COALESCE(MAX(s2.start_ts), :start_ts)
                 FROM statistics s2
                 JOIN statistics_meta sm2 ON s2.metadata_id = sm2.id
                WHERE sm2.statistic_id = :entity_id
                  AND s2.start_ts < :start_ts
                  AND s2.sum IS NOT NULL
           )
         ORDER BY s.start_ts
        """
    )

    try:
        with engine.connect() as conn:
            for eid in entity_ids:
                # Resolve working unit.
                if unit_overrides and eid in unit_overrides:
                    unit = unit_overrides[eid]
                else:
                    row = conn.execute(_unit_query, {"entity_id": eid}).fetchone()
                    unit = row[0] if row is not None else None

                if unit is None:
                    logger.debug("Entity %s has no rows in statistics_meta; skipping.", eid)
                    continue

                # Raises FetchError for unrecognised units.
                stype = _sensor_type(unit)

                if stype == "power":
                    multiplier = _POWER_UNIT_TO_KWH_PER_H[unit]
                    rows = conn.execute(
                        _power_query, {"entity_id": eid, "start_ts": start_ts}
                    ).fetchall()
                    # Convert raw power-unit mean to kWh/h.
                    raw_values: list[tuple[float, float]] = [
                        (float(r[0]), float(r[1]) * multiplier) for r in rows
                    ]
                else:
                    # energy sensor
                    multiplier = _ENERGY_UNIT_TO_KWH[unit]
                    rows = conn.execute(
                        _energy_query, {"entity_id": eid, "start_ts": start_ts}
                    ).fetchall()
                    sum_rows = [(float(r[0]), float(r[1])) for r in rows]
                    # Compute consecutive differences to get per-hour energy.
                    # The seed row (first row if it falls before start_ts) is
                    # used only to compute the first delta and is not itself
                    # included in the output.
                    deltas = _compute_deltas(sum_rows)
                    # Convert to kWh and keep only rows within the lookback window.
                    raw_values = [
                        (ts, delta * multiplier)
                        for ts, delta in deltas
                        if ts >= start_ts
                    ]

                # Apply P95 outlier detection. Use per-entity factor if provided.
                entity_factor = (
                    outlier_factors[eid]
                    if outlier_factors and eid in outlier_factors
                    else outlier_factor
                )
                clean_values = _detect_outliers(raw_values, entity_factor, eid)

                result[eid] = [
                    {
                        "start": datetime.fromtimestamp(
                            ts, tz=timezone.utc
                        ).isoformat(),
                        "mean": v,
                    }
                    for ts, v in clean_values
                ]

    except FetchError:
        # Re-raise FetchError from _sensor_type without wrapping it again.
        raise
    except SQLAlchemyError as exc:
        raise FetchError(f"Database query failed: {exc}") from exc
    finally:
        engine.dispose()

    return result


def fetch_entity_units(
    *,
    db_url: str,
    entity_ids: list[str],
) -> dict[str, str | None]:
    """Fetch the unit_of_measurement for each entity from the statistics_meta table.

    Returns a mapping from entity ID to its stored unit string. Entities that
    are not present in statistics_meta are mapped to None. This can happen when
    a sensor has never written long-term statistics to the recorder.

    Args:
        db_url: SQLAlchemy database URL, e.g.
            ``sqlite:////config/home-assistant_v2.db``.
        entity_ids: List of HA statistic IDs to look up.

    Returns:
        Dict mapping each entity ID to its unit string, or None if the entity
        is absent from statistics_meta or has no unit recorded.

    Raises:
        FetchError: If the engine cannot be created or the query fails.
    """
    try:
        engine = create_engine(db_url)
    except Exception as exc:
        raise FetchError(f"Cannot create database engine for {db_url!r}: {exc}") from exc

    result: dict[str, str | None] = {eid: None for eid in entity_ids}

    _query = text(
        "SELECT unit_of_measurement FROM statistics_meta WHERE statistic_id = :entity_id"
    )

    try:
        with engine.connect() as conn:
            for eid in entity_ids:
                row = conn.execute(_query, {"entity_id": eid}).fetchone()
                if row is not None:
                    result[eid] = row[0]  # may be None if the column value is NULL
    except SQLAlchemyError as exc:
        raise FetchError(f"Database unit query failed: {exc}") from exc
    finally:
        engine.dispose()

    return result

