"""Unit tests for baseload_ha.fetcher.

Tests exercise fetch_statistics against real (in-process) SQLite databases
created via SQLAlchemy, so no mocking of database internals is required. Each
test that needs a database receives a ``db_url`` fixture that points to a fresh
temporary file populated with the minimal HA statistics schema and a small set
of synthetic rows.

After Plan 51, ``fetch_statistics`` returns kWh/h values regardless of whether
the entity is a power sensor (mean-based) or an energy sensor (sum-delta-based).
All unit conversion and outlier detection happens inside the fetcher.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine, text

from baseload_ha_db.fetcher import FetchError, fetch_entity_units, fetch_statistics


# ---------------------------------------------------------------------------
# Reference timestamps used in synthetic rows.
# 2026-03-29 13:00 UTC  →  1743253200
# 2026-03-29 14:00 UTC  →  1743256800
# ---------------------------------------------------------------------------
_TS_1300 = datetime(2026, 3, 29, 13, 0, 0, tzinfo=timezone.utc).timestamp()
_TS_1400 = datetime(2026, 3, 29, 14, 0, 0, tzinfo=timezone.utc).timestamp()


def _make_db(
    tmp_path,
    *,
    entities: list[tuple[int, str, str]],
    rows: list[tuple[int, int, float, float | None, float | None]],
) -> str:
    """Create a minimal HA statistics DB with given entities and statistics rows.

    Args:
        tmp_path: pytest tmp_path fixture (or any Path).
        entities: List of (id, statistic_id, unit_of_measurement) tuples.
        rows: List of (id, metadata_id, start_ts, mean, sum) tuples.
            Use None for columns not relevant to the test.

    Returns:
        SQLAlchemy URL string for the created database.
    """
    path = tmp_path / f"ha_{id(entities)}.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE TABLE statistics_meta "
            "(id INTEGER PRIMARY KEY, statistic_id TEXT NOT NULL, unit_of_measurement TEXT)"
        ))
        conn.execute(text(
            "CREATE TABLE statistics "
            "(id INTEGER PRIMARY KEY, metadata_id INTEGER NOT NULL, "
            "start_ts REAL NOT NULL, mean REAL, sum REAL)"
        ))
        for eid, sid, unit in entities:
            conn.execute(text(
                "INSERT INTO statistics_meta VALUES (:id, :sid, :unit)"
            ), {"id": eid, "sid": sid, "unit": unit})
        for rid, mid, ts, mean, s in rows:
            conn.execute(text(
                "INSERT INTO statistics VALUES (:id, :mid, :ts, :mean, :sum)"
            ), {"id": rid, "mid": mid, "ts": ts, "mean": mean, "sum": s})
        conn.commit()
    engine.dispose()
    return url


def _create_ha_schema(db_url: str) -> None:
    """Create the HA recorder statistics tables and insert synthetic test rows.

    The schema is a minimal replica of the tables used by the HA recorder.
    Only the columns queried by fetch_statistics are present; the rest are
    omitted to keep the fixture lightweight.

    Args:
        db_url: SQLAlchemy URL for the database to initialise.
    """
    engine = create_engine(db_url)
    with engine.connect() as conn:
        conn.execute(text(
            """
            CREATE TABLE statistics_meta (
                id       INTEGER PRIMARY KEY,
                statistic_id TEXT NOT NULL,
                unit_of_measurement TEXT
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE statistics (
                id          INTEGER PRIMARY KEY,
                metadata_id INTEGER NOT NULL,
                start_ts    REAL    NOT NULL,
                mean        REAL,
                sum         REAL
            )
            """
        ))
        # Two power entities.
        conn.execute(text(
            "INSERT INTO statistics_meta VALUES (1, 'sensor.power_l1_w', 'W')"
        ))
        conn.execute(text(
            "INSERT INTO statistics_meta VALUES (2, 'sensor.battery_w', 'W')"
        ))
        # sensor.power_l1_w: readings at 13:00 and 14:00.
        conn.execute(text(
            f"INSERT INTO statistics VALUES (1, 1, {_TS_1300}, 500.0, NULL)"
        ))
        conn.execute(text(
            f"INSERT INTO statistics VALUES (2, 1, {_TS_1400}, 600.0, NULL)"
        ))
        # sensor.battery_w: one reading at 13:00; one NULL mean row (must be excluded).
        conn.execute(text(
            f"INSERT INTO statistics VALUES (3, 2, {_TS_1300}, 100.0, NULL)"
        ))
        conn.execute(text(
            f"INSERT INTO statistics VALUES (4, 2, {_TS_1400}, NULL, NULL)"
        ))
        conn.commit()
    engine.dispose()


@pytest.fixture()
def db_url(tmp_path: pytest.TempPathFactory) -> str:
    """Return a SQLAlchemy URL for a temporary SQLite database with HA statistics."""
    path = tmp_path / "ha.db"
    url = f"sqlite:///{path}"
    _create_ha_schema(url)
    return url


@freeze_time("2026-03-30")
class TestFetchStatisticsPowerSensors:
    def test_power_sensor_returns_kwh_per_hour_values(self, db_url: str) -> None:
        """Power sensor mean values are converted to kWh/h (W * 0.001)."""
        result = fetch_statistics(
            db_url=db_url,
            entity_ids=["sensor.power_l1_w"],
            lookback_days=30,
        )
        # 500 W * 0.001 = 0.5 kWh/h; 600 W * 0.001 = 0.6 kWh/h
        assert len(result["sensor.power_l1_w"]) == 2
        assert result["sensor.power_l1_w"][0]["mean"] == pytest.approx(0.5)
        assert result["sensor.power_l1_w"][1]["mean"] == pytest.approx(0.6)

    def test_null_mean_rows_are_excluded(self, db_url: str) -> None:
        result = fetch_statistics(
            db_url=db_url,
            entity_ids=["sensor.battery_w"],
            lookback_days=30,
        )
        # There are two rows for sensor.battery_w but one has mean=NULL.
        assert len(result["sensor.battery_w"]) == 1
        assert result["sensor.battery_w"][0]["mean"] == pytest.approx(0.1)

    def test_unknown_entity_returns_empty_list(self, db_url: str) -> None:
        result = fetch_statistics(
            db_url=db_url,
            entity_ids=["sensor.does_not_exist"],
            lookback_days=30,
        )
        assert result["sensor.does_not_exist"] == []

    def test_start_field_is_iso8601_utc_string(self, db_url: str) -> None:
        result = fetch_statistics(
            db_url=db_url,
            entity_ids=["sensor.power_l1_w"],
            lookback_days=30,
        )
        start = result["sensor.power_l1_w"][0]["start"]
        dt = datetime.fromisoformat(start)
        assert dt.tzinfo is not None

    def test_lookback_window_excludes_old_rows(self, tmp_path) -> None:
        """Rows outside the lookback window must not be returned."""
        old_ts = datetime(2020, 1, 1, 0, 0, 0, tzinfo=timezone.utc).timestamp()
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.old", "W")],
            rows=[(1, 1, old_ts, 999.0, None)],
        )
        result = fetch_statistics(db_url=url, entity_ids=["sensor.old"], lookback_days=7)
        assert result["sensor.old"] == []

    def test_raises_fetch_error_on_bad_url(self) -> None:
        with pytest.raises(FetchError):
            fetch_statistics(
                db_url="sqlite:////nonexistent/path/ha.db",
                entity_ids=["sensor.p"],
                lookback_days=7,
            )

    def test_raises_fetch_error_on_missing_table(self, tmp_path) -> None:
        url = f"sqlite:///{tmp_path / 'empty.db'}"
        engine = create_engine(url)
        with engine.connect() as conn:  # noqa: F841
            pass
        engine.dispose()
        with pytest.raises(FetchError):
            fetch_statistics(db_url=url, entity_ids=["sensor.p"], lookback_days=7)


@freeze_time("2026-03-30")
class TestFetchStatisticsEnergySensors:
    def test_energy_sensor_uses_sum_deltas(self, tmp_path) -> None:
        """An entity with unit kWh returns sum-delta kWh values."""
        # sum goes 0 -> 1.5 -> 3.0 kWh over two hours
        ts0 = _TS_1300 - 3600  # one hour before window to seed the first delta
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.energy_kwh", "kWh")],
            rows=[
                (1, 1, ts0,     None, 0.0),
                (2, 1, _TS_1300, None, 1.5),
                (3, 1, _TS_1400, None, 3.0),
            ],
        )
        # lookback_days=30 covers both rows
        result = fetch_statistics(db_url=url, entity_ids=["sensor.energy_kwh"], lookback_days=30)
        readings = result["sensor.energy_kwh"]
        assert len(readings) == 2
        assert readings[0]["mean"] == pytest.approx(1.5)
        assert readings[1]["mean"] == pytest.approx(1.5)

    def test_energy_entity_fetches_extra_pre_window_row(self, tmp_path) -> None:
        """The first delta in the window uses the row just before window start."""
        now = datetime.now(tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
        window_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0)
        pre_window_ts = (window_start - timedelta(hours=1)).timestamp()
        in_window_ts = window_start.timestamp()

        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.e", "kWh")],
            rows=[
                (1, 1, pre_window_ts, None, 10.0),
                (2, 1, in_window_ts,  None, 11.5),
            ],
        )
        result = fetch_statistics(db_url=url, entity_ids=["sensor.e"], lookback_days=1)
        readings = result["sensor.e"]
        # Without the pre-window row the first reading would be missing.
        assert len(readings) == 1
        assert readings[0]["mean"] == pytest.approx(1.5)

    def test_negative_energy_delta_is_discarded(self, tmp_path) -> None:
        """A negative sum delta is dropped and does not appear in returned readings."""
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.e", "kWh")],
            rows=[
                (1, 1, _TS_1300 - 3600, None, 5.0),
                (2, 1, _TS_1300,         None, 4.0),  # negative delta (-1.0) — corrupted
                (3, 1, _TS_1400,         None, 5.5),  # positive delta (+1.5) — valid
            ],
        )
        result = fetch_statistics(db_url=url, entity_ids=["sensor.e"], lookback_days=30)
        readings = result["sensor.e"]
        assert len(readings) == 1
        assert readings[0]["mean"] == pytest.approx(1.5)

    def test_unknown_unit_raises_fetch_error(self, tmp_path) -> None:
        """An entity whose unit is not power or energy raises FetchError."""
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.temp", "°C")],
            rows=[(1, 1, _TS_1300, 21.5, None)],
        )
        with pytest.raises(FetchError, match="°C"):
            fetch_statistics(db_url=url, entity_ids=["sensor.temp"], lookback_days=7)

    def test_wh_energy_unit_converted_to_kwh(self, tmp_path) -> None:
        """An entity with unit Wh has deltas divided by 1000."""
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.e_wh", "Wh")],
            rows=[
                (1, 1, _TS_1300 - 3600, None, 0.0),
                (2, 1, _TS_1300,         None, 2000.0),  # delta = 2000 Wh = 2.0 kWh
            ],
        )
        result = fetch_statistics(db_url=url, entity_ids=["sensor.e_wh"], lookback_days=30)
        assert result["sensor.e_wh"][0]["mean"] == pytest.approx(2.0)

    def test_mwh_energy_unit_converted_to_kwh(self, tmp_path) -> None:
        """An entity with unit MWh has deltas multiplied by 1000."""
        url = _make_db(
            tmp_path,
            entities=[(1, "sensor.e_mwh", "MWh")],
            rows=[
                (1, 1, _TS_1300 - 3600, None, 0.0),
                (2, 1, _TS_1300,         None, 0.003),  # delta = 0.003 MWh = 3 kWh
            ],
        )
        result = fetch_statistics(db_url=url, entity_ids=["sensor.e_mwh"], lookback_days=30)
        assert result["sensor.e_mwh"][0]["mean"] == pytest.approx(3.0)


@freeze_time("2026-03-30")
class TestOutlierDetection:
    def _make_power_db_with_outlier(self, tmp_path, *, outlier_w: float) -> str:
        """30 normal 1000 W readings plus one outlier."""
        base_ts = _TS_1300
        rows = [(i + 1, 1, base_ts + i * 3600, 1000.0, None) for i in range(30)]
        rows.append((31, 1, base_ts + 30 * 3600, outlier_w, None))
        return _make_db(tmp_path, entities=[(1, "sensor.p", "W")], rows=rows)

    def test_outlier_above_p99_threshold_is_dropped(self, tmp_path) -> None:
        """A power reading > P99 * outlier_factor is excluded from returned readings."""
        # 30 readings at 1000 W; P99 (index 29/31) = 1000 W; threshold = 1000 * 10 = 10000 W
        # outlier at 20000 W > 10000 W threshold -> dropped
        url = self._make_power_db_with_outlier(tmp_path, outlier_w=20000.0)
        result = fetch_statistics(db_url=url, entity_ids=["sensor.p"], lookback_days=30)
        values = [r["mean"] for r in result["sensor.p"]]
        assert all(v == pytest.approx(1.0) for v in values)
        assert len(values) == 30

    def test_reading_just_below_threshold_is_kept(self, tmp_path) -> None:
        """A reading just below P99 * outlier_factor is retained."""
        # P99 = 1000 W; threshold = 10000 W; reading at 9999 W -> kept
        url = self._make_power_db_with_outlier(tmp_path, outlier_w=9999.0)
        result = fetch_statistics(db_url=url, entity_ids=["sensor.p"], lookback_days=30)
        assert len(result["sensor.p"]) == 31

    def test_zero_inflated_distribution_uses_p99_nonzero_as_threshold_base(self, tmp_path) -> None:
        """When P99 == 0 (device idle >=99% of the time), P99 of non-zero values is used."""
        # 98 readings at 0 W; 1 reading at 2000 W; 1 outlier at 50000 W (total 100)
        # P99 of all 100 = sorted[98] = 0 -> zero-inflation guard triggers
        # P99 of non-zero [2000, 50000] = 2000 W -> threshold = 2000 * 10 = 20000 W
        # 50000 W > 20000 W -> dropped; 2000 W -> kept
        rows = [(i + 1, 1, _TS_1300 + i * 3600, 0.0, None) for i in range(98)]
        rows.append((99, 1, _TS_1300 + 98 * 3600, 2000.0, None))
        rows.append((100, 1, _TS_1300 + 99 * 3600, 50000.0, None))
        url = _make_db(tmp_path, entities=[(1, "sensor.bat", "W")], rows=rows)
        result = fetch_statistics(db_url=url, entity_ids=["sensor.bat"], lookback_days=30)
        values = [r["mean"] for r in result["sensor.bat"]]
        assert 50.0 not in values  # 50000 W = 50.0 kWh/h should be dropped
        assert len(values) == 99   # 98 zeros + 1 normal reading retained

    def test_fewer_than_24_samples_skips_detection(self, tmp_path) -> None:
        """With < 24 samples no values are dropped regardless of magnitude."""
        rows = [(i + 1, 1, _TS_1300 + i * 3600, 500.0, None) for i in range(10)]
        rows.append((11, 1, _TS_1300 + 10 * 3600, 99999.0, None))
        url = _make_db(tmp_path, entities=[(1, "sensor.p", "W")], rows=rows)
        result = fetch_statistics(db_url=url, entity_ids=["sensor.p"], lookback_days=30)
        # 99999 W = 99.999 kWh/h should survive because < 24 samples
        assert len(result["sensor.p"]) == 11


class TestFetchEntityUnits:
    def test_returns_unit_for_known_entities(self, db_url: str) -> None:
        result = fetch_entity_units(
            db_url=db_url,
            entity_ids=["sensor.power_l1_w", "sensor.battery_w"],
        )
        assert result["sensor.power_l1_w"] == "W"
        assert result["sensor.battery_w"] == "W"

    def test_returns_none_for_unknown_entity(self, db_url: str) -> None:
        result = fetch_entity_units(
            db_url=db_url,
            entity_ids=["sensor.does_not_exist"],
        )
        assert result["sensor.does_not_exist"] is None

    def test_raises_fetch_error_on_bad_url(self) -> None:
        with pytest.raises(FetchError):
            fetch_entity_units(
                db_url="sqlite:////nonexistent/path/ha.db",
                entity_ids=["sensor.p"],
            )

    def test_raises_fetch_error_on_missing_table(self, tmp_path: pytest.TempPathFactory) -> None:
        url = f"sqlite:///{tmp_path / 'empty2.db'}"
        engine = create_engine(url)
        with engine.connect() as conn:  # noqa: F841
            pass
        engine.dispose()

        with pytest.raises(FetchError):
            fetch_entity_units(db_url=url, entity_ids=["sensor.p"])

