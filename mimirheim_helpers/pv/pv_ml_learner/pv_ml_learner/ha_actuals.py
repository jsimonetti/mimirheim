"""Home Assistant database reader for pv_ml_learner.

Reads historic PV production from the Home Assistant ``statistics``
table and converts cumulative energy totals into per-hour kWh values.

The ``statistics`` and ``statistics_meta`` tables are the current HA schema
(introduced in HA 2022.12, replacing the older ``long_term_statistics`` and
``long_term_statistics_meta`` tables). Column names are identical across both
schemas; only the table names differ.

What this module does not do:
- It never writes to the HA database.
- It does not write to the pv_ml_learner SQLite database directly; callers pass
  results to ``storage.upsert_pv_actuals``.
- It does not perform any MQTT or HTTP operations.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from pv_ml_learner.storage import PvActualRow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection factory
# ---------------------------------------------------------------------------


def build_ha_engine(db_path: str) -> sa.Engine:
    """Build a read-only SQLAlchemy engine for the HA SQLite database.

    Opens the database in read-only mode using SQLite URI syntax. This
    prevents accidental writes even if the calling code has a bug.

    Args:
        db_path: Filesystem path to the HA SQLite database file.

    Returns:
        A SQLAlchemy ``Engine`` connected to the HA database in read-only mode.
    """
    # Encode the path into a SQLite URI with mode=ro (read-only).
    # check_same_thread=False is required for use from multiple threads
    # (the apscheduler ingest job runs on a background thread).
    encoded_path = db_path.replace(" ", "%20")
    url = f"sqlite:///file:{encoded_path}?uri=true&mode=ro"
    return sa.create_engine(
        url,
        connect_args={"check_same_thread": False},
    )


# ---------------------------------------------------------------------------
# Delta computation
# ---------------------------------------------------------------------------


def compute_hourly_kwh(
    conn: sa.Connection,
    entity_ids: list[str],
    start_ts: int,
    exclude_limiting_entity_ids: list[str] | None = None,
    array_name: str = "",
) -> list[PvActualRow]:
    """Compute per-hour PV production by differencing cumulative HA sums.

    Reads ``statistics`` for each entity ID in ``entity_ids``,
    computes the per-hour delta (this_hour.sum - prev_hour.sum) for each
    entity, sums across entities per hour, and returns ``PvActualRow`` objects
    for each hour where a complete delta is available for all entities.

    Delta computation rules:
    - Negative deltas (cumulative rollover) are clamped to 0.0.
    - A gap in the data (hour with no row for an entity) means no output row
      is produced for that hour — the hour is silently omitted, not zeroed.
    - Only hours after ``start_ts`` are returned (the caller provides the last
      known timestamp to avoid re-ingesting already-stored rows).

    Args:
        conn: An open SQLAlchemy connection to the HA database (read-only).
        entity_ids: Statistic IDs to sum (e.g. ``["sensor.solaredge_energy"]``).
        start_ts: Only return rows with ``start_ts`` strictly greater than this
            value. Typically ``storage.get_latest_actuals_ts()``, or 0 on first run.
        exclude_limiting_entity_ids: Optional list of binary/numeric sensor
            entity IDs. Hours where any of these reads True or > 0 in
            ``statistics`` are excluded. Defaults to None (no exclusion).
        array_name: Identifier written into each returned ``PvActualRow``. Leave
            empty when constructing rows outside the daemon context.

    Returns:
        List of ``PvActualRow`` sorted by ``hour_utc`` ascending. Does not
        include the first row of each entity (no previous row to delta from).
    """
    if not entity_ids:
        return []

    # Fetch the one row before start_ts per entity to enable delta at start_ts.
    # We do this by fetching from a bit earlier: start_ts - one hour is enough.
    fetch_from = max(0, start_ts - 3600)

    # Query statistics for all requested entities.
    rows_by_entity: dict[str, list[tuple[int, float]]] = {eid: [] for eid in entity_ids}

    for entity_id in entity_ids:
        result = conn.execute(
            sa.text("""
                SELECT lts.start_ts, lts.sum
                FROM statistics lts
                JOIN statistics_meta ltm ON lts.metadata_id = ltm.id
                WHERE ltm.statistic_id = :eid
                  AND lts.start_ts >= :from_ts
                ORDER BY lts.start_ts
            """),
            {"eid": entity_id, "from_ts": fetch_from},
        ).fetchall()
        rows_by_entity[entity_id] = [(int(r[0]), float(r[1])) for r in result if r[1] is not None]

    # Compute deltas per entity.
    deltas_by_entity: dict[str, dict[int, float]] = {}
    for entity_id, sorted_rows in rows_by_entity.items():
        deltas: dict[int, float] = {}
        for i in range(1, len(sorted_rows)):
            prev_ts, prev_sum = sorted_rows[i - 1]
            curr_ts, curr_sum = sorted_rows[i]
            # Only include consecutive hourly steps (3600 seconds gap).
            if curr_ts - prev_ts != 3600:
                continue
            delta = max(0.0, curr_sum - prev_sum)
            deltas[curr_ts] = delta
        deltas_by_entity[entity_id] = deltas

    if not deltas_by_entity:
        return []

    # Find hours where all entities have a delta available.
    candidate_hours = set(next(iter(deltas_by_entity.values())).keys())
    for deltas in deltas_by_entity.values():
        candidate_hours &= set(deltas.keys())

    # Apply start_ts filter: only hours strictly after start_ts.
    candidate_hours = {h for h in candidate_hours if h > start_ts}

    if not candidate_hours:
        return []

    # Build exclusion set if limiting sensors are configured.
    excluded_hours: set[int] = set()
    if exclude_limiting_entity_ids:
        for eid in exclude_limiting_entity_ids:
            result = conn.execute(
                sa.text("""
                    SELECT lts.start_ts, lts.sum
                    FROM statistics lts
                    JOIN statistics_meta ltm ON lts.metadata_id = ltm.id
                    WHERE ltm.statistic_id = :eid
                      AND lts.start_ts IN :hours
                """),
                {"eid": eid, "hours": tuple(candidate_hours)},
            ).fetchall()
            for ts, val in result:
                if val is not None and val > 0:
                    excluded_hours.add(int(ts))

    # Sum across entities per hour and build output rows.
    output: list[PvActualRow] = []
    for hour_ts in sorted(candidate_hours - excluded_hours):
        total_kwh = sum(
            deltas_by_entity[eid][hour_ts] for eid in entity_ids
        )
        output.append(PvActualRow(array_name=array_name, hour_utc=hour_ts, kwh=total_kwh))

    return output
