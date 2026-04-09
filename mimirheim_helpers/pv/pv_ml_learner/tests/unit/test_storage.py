"""Tests for pv_ml_learner.storage.

All tests use an in-memory SQLite connection created via SQLAlchemy.
No files are written to disk.
"""

from __future__ import annotations

import sqlalchemy as sa
import pytest


@pytest.fixture()
def conn():
    """Yield an in-memory SQLite connection with the schema initialised."""
    from pv_ml_learner.storage import create_schema

    engine = sa.create_engine("sqlite:///:memory:")
    with engine.connect() as c:
        create_schema(c)
        c.commit()
        yield c


@pytest.fixture()
def knmi_row():
    from pv_ml_learner.storage import KnmiRow

    return KnmiRow(
        hour_utc=1_700_000_000,
        station_id=260,
        ghi_wm2=150.0,
        wind_ms=3.5,
        temp_c=12.0,
        rain_mm=0.0,
    )


@pytest.fixture()
def mc_row():
    from pv_ml_learner.storage import McRow

    return McRow(
        step_ts=1_700_010_000,
        ghi_wm2=200.0,
        temp_c=14.0,
        wind_ms=4.0,
        rain_mm=0.5,
        cloud_pct=30.0,
    )


@pytest.fixture()
def pv_row():
    from pv_ml_learner.storage import PvActualRow

    return PvActualRow(array_name="main", hour_utc=1_700_000_000, kwh=2.5)


class TestKnmiUpsert:
    def test_insert_and_retrieve(self, conn, knmi_row) -> None:
        from pv_ml_learner.storage import upsert_knmi_hours, get_knmi_range

        upsert_knmi_hours(conn, [knmi_row])
        conn.commit()
        rows = get_knmi_range(conn, knmi_row.hour_utc - 1, knmi_row.hour_utc + 1)
        assert len(rows) == 1
        assert rows[0].ghi_wm2 == pytest.approx(150.0)

    def test_upsert_is_idempotent(self, conn, knmi_row) -> None:
        from pv_ml_learner.storage import upsert_knmi_hours, get_knmi_range

        upsert_knmi_hours(conn, [knmi_row])
        conn.commit()
        # Insert again with updated value — should overwrite, not raise.
        from pv_ml_learner.storage import KnmiRow

        updated = KnmiRow(
            hour_utc=knmi_row.hour_utc,
            station_id=260,
            ghi_wm2=200.0,
            wind_ms=knmi_row.wind_ms,
            temp_c=knmi_row.temp_c,
            rain_mm=knmi_row.rain_mm,
        )
        upsert_knmi_hours(conn, [updated])
        conn.commit()
        rows = get_knmi_range(conn, knmi_row.hour_utc - 1, knmi_row.hour_utc + 1)
        assert len(rows) == 1
        assert rows[0].ghi_wm2 == pytest.approx(200.0)

    def test_get_knmi_range_filters_correctly(self, conn) -> None:
        from pv_ml_learner.storage import upsert_knmi_hours, get_knmi_range, KnmiRow

        base = 1_700_000_000
        rows = [
            KnmiRow(hour_utc=base + i * 3600, station_id=260,
                    ghi_wm2=float(i * 10), wind_ms=None, temp_c=None, rain_mm=0.0)
            for i in range(5)
        ]
        upsert_knmi_hours(conn, rows)
        conn.commit()

        # Request only middle three
        result = get_knmi_range(conn, base + 3600, base + 3 * 3600)
        assert len(result) == 3
        assert result[0].hour_utc == base + 3600
        assert result[-1].hour_utc == base + 3 * 3600

    def test_get_latest_knmi_ts_empty_returns_none(self, conn) -> None:
        from pv_ml_learner.storage import get_latest_knmi_ts

        assert get_latest_knmi_ts(conn) is None

    def test_get_latest_knmi_ts_returns_max(self, conn) -> None:
        from pv_ml_learner.storage import upsert_knmi_hours, get_latest_knmi_ts, KnmiRow

        base = 1_700_000_000
        rows = [
            KnmiRow(hour_utc=base + i * 3600, station_id=260,
                    ghi_wm2=10.0, wind_ms=None, temp_c=None, rain_mm=0.0)
            for i in range(3)
        ]
        upsert_knmi_hours(conn, rows)
        conn.commit()
        assert get_latest_knmi_ts(conn) == base + 2 * 3600

    def test_nullable_wind_and_temp(self, conn) -> None:
        from pv_ml_learner.storage import upsert_knmi_hours, get_knmi_range, KnmiRow

        row = KnmiRow(
            hour_utc=1_700_000_000, station_id=260,
            ghi_wm2=50.0, wind_ms=None, temp_c=None, rain_mm=0.1,
        )
        upsert_knmi_hours(conn, [row])
        conn.commit()
        result = get_knmi_range(conn, row.hour_utc - 1, row.hour_utc + 1)
        assert result[0].wind_ms is None
        assert result[0].temp_c is None


class TestMeteoserverFetch:
    def test_insert_and_retrieve_latest(self, conn, mc_row) -> None:
        from pv_ml_learner.storage import insert_meteoserver_fetch, get_latest_meteoserver_fetch

        insert_meteoserver_fetch(conn, fetch_ts=1_700_000_000, rows=[mc_row])
        conn.commit()
        result = get_latest_meteoserver_fetch(conn)
        assert result is not None
        assert len(result) == 1
        assert result[0].ghi_wm2 == pytest.approx(200.0)

    def test_get_latest_returns_most_recent_fetch(self, conn, mc_row) -> None:
        from pv_ml_learner.storage import insert_meteoserver_fetch, get_latest_meteoserver_fetch, McRow

        old_row = McRow(step_ts=1_700_010_000, ghi_wm2=100.0,
                        temp_c=10.0, wind_ms=2.0, rain_mm=0.0, cloud_pct=50.0)
        new_row = McRow(step_ts=1_700_010_000, ghi_wm2=250.0,
                        temp_c=15.0, wind_ms=5.0, rain_mm=0.0, cloud_pct=20.0)

        insert_meteoserver_fetch(conn, fetch_ts=1_700_000_000, rows=[old_row])
        conn.commit()
        insert_meteoserver_fetch(conn, fetch_ts=1_700_004_000, rows=[new_row])
        conn.commit()

        result = get_latest_meteoserver_fetch(conn)
        assert result is not None
        assert result[0].ghi_wm2 == pytest.approx(250.0)

    def test_get_latest_returns_none_when_empty(self, conn) -> None:
        from pv_ml_learner.storage import get_latest_meteoserver_fetch

        assert get_latest_meteoserver_fetch(conn) is None

    def test_prune_keeps_most_recent_fetches(self, conn, mc_row) -> None:
        from pv_ml_learner.storage import insert_meteoserver_fetch, prune_meteoserver, get_latest_meteoserver_fetch
        import sqlalchemy as sa

        for i in range(5):
            from pv_ml_learner.storage import McRow
            row = McRow(step_ts=1_700_010_000 + i, ghi_wm2=float(i * 10),
                        temp_c=10.0, wind_ms=2.0, rain_mm=0.0, cloud_pct=30.0)
            insert_meteoserver_fetch(conn, fetch_ts=1_700_000_000 + i * 1000, rows=[row])
        conn.commit()

        prune_meteoserver(conn, keep_fetches=2)
        conn.commit()

        # Only the 2 most recent fetches should remain.
        from pv_ml_learner import storage as st
        result = conn.execute(sa.select(sa.func.count()).select_from(
            sa.text("meteoserver_forecast")
        )).scalar_one()
        assert result == 2


class TestPvActuals:
    def test_upsert_and_retrieve(self, conn, pv_row) -> None:
        from pv_ml_learner.storage import upsert_pv_actuals, get_pv_actuals_range

        upsert_pv_actuals(conn, [pv_row])
        conn.commit()
        rows = get_pv_actuals_range(conn, "main", pv_row.hour_utc - 1, pv_row.hour_utc + 1)
        assert len(rows) == 1
        assert rows[0].kwh == pytest.approx(2.5)

    def test_upsert_is_idempotent(self, conn, pv_row) -> None:
        from pv_ml_learner.storage import upsert_pv_actuals, get_pv_actuals_range, PvActualRow

        upsert_pv_actuals(conn, [pv_row])
        conn.commit()
        upsert_pv_actuals(conn, [PvActualRow(array_name="main", hour_utc=pv_row.hour_utc, kwh=9.9)])
        conn.commit()
        rows = get_pv_actuals_range(conn, "main", pv_row.hour_utc - 1, pv_row.hour_utc + 1)
        assert len(rows) == 1
        assert rows[0].kwh == pytest.approx(9.9)

    def test_get_latest_actuals_ts_empty_returns_none(self, conn) -> None:
        from pv_ml_learner.storage import get_latest_actuals_ts

        assert get_latest_actuals_ts(conn, "main") is None

    def test_get_earliest_actuals_ts_empty_returns_none(self, conn) -> None:
        from pv_ml_learner.storage import get_earliest_actuals_ts

        assert get_earliest_actuals_ts(conn) is None

    def test_get_earliest_actuals_ts_returns_global_min(self, conn) -> None:
        from pv_ml_learner.storage import get_earliest_actuals_ts, upsert_pv_actuals, PvActualRow

        rows = [
            PvActualRow(array_name="east", hour_utc=1000, kwh=1.0),
            PvActualRow(array_name="east", hour_utc=2000, kwh=1.0),
            PvActualRow(array_name="west", hour_utc=500, kwh=1.0),
            PvActualRow(array_name="west", hour_utc=3000, kwh=1.0),
        ]
        upsert_pv_actuals(conn, rows)
        conn.commit()

        # Global minimum across all arrays is 500.
        assert get_earliest_actuals_ts(conn) == 500

    def test_count_distinct_months_correct(self, conn) -> None:
        from pv_ml_learner.storage import upsert_pv_actuals, count_distinct_months, PvActualRow
        import calendar

        # Insert one row per month spanning 14 months (two overlap in year boundary)
        rows = []
        for month in range(1, 13):
            # First day of each month in 2024, noon UTC
            ts = int(calendar.timegm((2024, month, 1, 12, 0, 0, 0, 0, 0)))
            rows.append(PvActualRow(array_name="main", hour_utc=ts, kwh=1.0))
        # Add a 13th entry in a different year, same month as row 1 — still 12 distinct months
        ts_extra = int(calendar.timegm((2025, 1, 1, 12, 0, 0, 0, 0, 0)))
        rows.append(PvActualRow(array_name="main", hour_utc=ts_extra, kwh=1.0))

        upsert_pv_actuals(conn, rows)
        conn.commit()

        assert count_distinct_months(conn, "main") == 12
