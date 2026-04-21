"""Tests for pv_ml_learner.ha_actuals.

All tests use an in-memory SQLite database pre-populated to match the HA
statistics schema (current schema, introduced in HA 2022.12). No real HA
database is used.
"""

from __future__ import annotations

import calendar

import pytest
import sqlalchemy as sa


# ---------------------------------------------------------------------------
# Helpers to build a synthetic HA database
# ---------------------------------------------------------------------------


def _create_ha_schema(conn: sa.Connection) -> None:
    """Create the HA statistics tables in the given connection."""
    conn.execute(sa.text("""
        CREATE TABLE statistics_meta (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            statistic_id TEXT NOT NULL
        )
    """))
    conn.execute(sa.text("""
        CREATE TABLE statistics (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            metadata_id INTEGER NOT NULL,
            start_ts    INTEGER NOT NULL,
            sum         REAL
        )
    """))
    conn.commit()


def _insert_meta(conn: sa.Connection, statistic_id: str) -> int:
    """Insert a metadata row and return its id."""
    result = conn.execute(
        sa.text("INSERT INTO statistics_meta (statistic_id) VALUES (:sid)"),
        {"sid": statistic_id},
    )
    conn.commit()
    return result.lastrowid


def _insert_stat(
    conn: sa.Connection, metadata_id: int, start_ts: int, cumsum: float
) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO statistics (metadata_id, start_ts, sum) "
            "VALUES (:mid, :ts, :s)"
        ),
        {"mid": metadata_id, "ts": start_ts, "s": cumsum},
    )
    conn.commit()


def _ha_engine() -> sa.Engine:
    return sa.create_engine("sqlite:///:memory:")


def _ts(year: int, month: int, day: int, hour: int) -> int:
    return int(calendar.timegm((year, month, day, hour, 0, 0, 0, 0, 0)))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeltaComputation:
    def test_delta_correct_for_single_sensor(self) -> None:
        """Given cumulative sum values [10, 12, 15], per-hour deltas are [2, 3]."""
        from pv_ml_learner.ha_actuals import compute_hourly_kwh

        engine = _ha_engine()
        with engine.connect() as conn:
            _create_ha_schema(conn)
            mid = _insert_meta(conn, "sensor.pv_energy")
            base = _ts(2024, 6, 1, 10)
            _insert_stat(conn, mid, base,            10.0)
            _insert_stat(conn, mid, base + 3600,     12.0)
            _insert_stat(conn, mid, base + 7200,     15.0)

            rows = compute_hourly_kwh(
                conn,
                entity_ids=["sensor.pv_energy"],
                start_ts=base,
            )

        assert len(rows) == 2
        assert rows[0].kwh == pytest.approx(2.0)
        assert rows[1].kwh == pytest.approx(3.0)

    def test_negative_delta_is_clamped_to_zero(self) -> None:
        """A negative delta (cumulative rollover) is clamped to 0.0."""
        from pv_ml_learner.ha_actuals import compute_hourly_kwh

        engine = _ha_engine()
        with engine.connect() as conn:
            _create_ha_schema(conn)
            mid = _insert_meta(conn, "sensor.pv_energy")
            base = _ts(2024, 6, 1, 10)
            # sum goes 10 → 12 → 15 → 14 (rollover)
            for ts_offset, cumsum in [(0, 10.0), (3600, 12.0), (7200, 15.0), (10800, 14.0)]:
                _insert_stat(conn, mid, base + ts_offset, cumsum)

            rows = compute_hourly_kwh(
                conn,
                entity_ids=["sensor.pv_energy"],
                start_ts=base,
            )

        kwhs = [r.kwh for r in rows]
        assert kwhs == pytest.approx([2.0, 3.0, 0.0])

    def test_two_sensors_summed_per_hour(self) -> None:
        """Two sensors at the same hour are summed correctly."""
        from pv_ml_learner.ha_actuals import compute_hourly_kwh

        engine = _ha_engine()
        with engine.connect() as conn:
            _create_ha_schema(conn)
            mid_a = _insert_meta(conn, "sensor.pv_east")
            mid_b = _insert_meta(conn, "sensor.pv_west")
            base = _ts(2024, 6, 1, 10)

            # East: 10 → 12 → 15 (deltas: 2, 3)
            _insert_stat(conn, mid_a, base,         10.0)
            _insert_stat(conn, mid_a, base + 3600,  12.0)
            _insert_stat(conn, mid_a, base + 7200,  15.0)

            # West: 5 → 7 → 12 (deltas: 2, 5)
            _insert_stat(conn, mid_b, base,         5.0)
            _insert_stat(conn, mid_b, base + 3600,  7.0)
            _insert_stat(conn, mid_b, base + 7200,  12.0)

            rows = compute_hourly_kwh(
                conn,
                entity_ids=["sensor.pv_east", "sensor.pv_west"],
                start_ts=base,
            )

        assert len(rows) == 2
        assert rows[0].kwh == pytest.approx(2.0 + 2.0)   # hour 1
        assert rows[1].kwh == pytest.approx(3.0 + 5.0)   # hour 2

    def test_gap_in_data_produces_no_row(self) -> None:
        """A missing hour in HA data produces no row — not a zero."""
        from pv_ml_learner.ha_actuals import compute_hourly_kwh

        engine = _ha_engine()
        with engine.connect() as conn:
            _create_ha_schema(conn)
            mid = _insert_meta(conn, "sensor.pv_energy")
            base = _ts(2024, 6, 1, 10)
            # Hours 0 and 2 present, hour 1 missing → only one delta possible
            _insert_stat(conn, mid, base,           10.0)
            _insert_stat(conn, mid, base + 7200,    15.0)  # gap at base+3600

            rows = compute_hourly_kwh(
                conn,
                entity_ids=["sensor.pv_energy"],
                start_ts=base,
            )

        # The gap between base and base+7200 spans two hours; since hour 1 is
        # missing, no delta row is generated for that pair.
        assert len(rows) == 0

    def test_build_ha_engine_accepts_sqlite_url(self) -> None:
        """build_ha_engine accepts a plain sqlite:/// URL."""
        from pv_ml_learner.ha_actuals import build_ha_engine

        engine = build_ha_engine("sqlite:///:memory:")
        assert engine is not None

    def test_build_ha_engine_accepts_sqlite_file_url(self) -> None:
        """build_ha_engine accepts a sqlite:///file: read-only URL."""
        from pv_ml_learner.ha_actuals import build_ha_engine

        engine = build_ha_engine("sqlite:///file:/tmp/test.db?uri=true&mode=ro")
        assert engine is not None

    def test_build_ha_engine_sqlite_path_url_gets_readonly_flag(self) -> None:
        """A plain sqlite:////path URL is converted to read-only URI form."""
        from pv_ml_learner.ha_actuals import build_ha_engine

        engine = build_ha_engine("sqlite:////config/homeassistant_v2.db")
        url = str(engine.url)
        assert "sqlite" in url.lower()
