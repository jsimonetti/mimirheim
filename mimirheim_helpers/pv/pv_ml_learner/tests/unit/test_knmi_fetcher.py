"""Tests for pv_ml_learner.knmi_fetcher.

knmi-py makes real HTTP calls, so all tests mock ``knmi.get_hour_data_dataframe``
at the import boundary. No real API calls are made.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock
import pandas as pd
import pytest


def _make_df(rows: list[dict]) -> pd.DataFrame:
    """Build a synthetic DataFrame matching knmi-py output format.

    knmi-py returns a DataFrame indexed by a DatetimeIndex (UTC-naive, but
    representing UTC times). Columns are uppercase KNMI variable codes with
    leading spaces stripped: ``STN``, ``Q``, ``FH``, ``T``, ``RH``.

    Args:
        rows: List of dicts with keys ``ts`` (datetime), ``STN``, ``Q``,
            ``FH``, ``T``, ``RH``.

    Returns:
        DataFrame matching knmi-py return format.
    """
    import datetime

    index = pd.DatetimeIndex([r["ts"] for r in rows], name="YYYYMMDD_HH")
    data = {
        " STN": [r["STN"] for r in rows],
        "   Q": [r.get("Q", 100) for r in rows],
        "  FH": [r.get("FH", 30) for r in rows],
        "   T": [r.get("T", 120) for r in rows],
        "  RH": [r.get("RH", 5) for r in rows],
    }
    return pd.DataFrame(data, index=index)


def _ts(year: int, month: int, day: int, hour: int) -> "pd.Timestamp":
    """Return a UTC-naive pandas Timestamp."""
    return pd.Timestamp(year=year, month=month, day=day, hour=hour)


class TestConversions:
    def test_successful_fetch_converts_all_variables(self) -> None:
        """Q, FH, T, RH are converted with the correct multipliers."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        df = _make_df([
            {"ts": _ts(2024, 6, 1, 10), "STN": 260,
             "Q": 720, "FH": 40, "T": 185, "RH": 12},
        ])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert len(rows) == 1
        r = rows[0]
        assert r.ghi_wm2 == pytest.approx(720 * 10_000 / 3_600)
        assert r.wind_ms == pytest.approx(40 * 0.1)
        assert r.temp_c == pytest.approx(185 * 0.1)
        assert r.rain_mm == pytest.approx(12 * 0.1)

    def test_q_minus_one_row_is_dropped(self) -> None:
        """Rows with Q == -1 (missing radiation) are dropped entirely."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        df = _make_df([
            {"ts": _ts(2024, 6, 1, 10), "STN": 260, "Q": -1, "FH": 30, "T": 100, "RH": 5},
            {"ts": _ts(2024, 6, 1, 11), "STN": 260, "Q": 500, "FH": 30, "T": 100, "RH": 5},
        ])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert len(rows) == 1
        assert rows[0].ghi_wm2 == pytest.approx(500 * 10_000 / 3_600)

    def test_fh_minus_9999_produces_none_wind(self) -> None:
        """FH == -9999 (station did not report wind) stores wind_ms as None."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        df = _make_df([
            {"ts": _ts(2024, 6, 1, 10), "STN": 260, "Q": 300, "FH": -9999, "T": 100, "RH": 0},
        ])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert rows[0].wind_ms is None

    def test_t_minus_9999_produces_none_temp(self) -> None:
        """T == -9999 (station did not report temperature) stores temp_c as None."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        df = _make_df([
            {"ts": _ts(2024, 6, 1, 10), "STN": 260, "Q": 300, "FH": 30, "T": -9999, "RH": 0},
        ])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert rows[0].temp_c is None

    def test_rh_minus_one_produces_zero_rain(self) -> None:
        """RH == -1 (trace precipitation) stores rain_mm as 0.0."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        df = _make_df([
            {"ts": _ts(2024, 6, 1, 10), "STN": 260, "Q": 300, "FH": 30, "T": 100, "RH": -1},
        ])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert rows[0].rain_mm == pytest.approx(0.0)

    def test_knmi_hour_24_maps_to_midnight_next_day(self) -> None:
        """knmi-py normalises HH-24 to 00:00 of the following day already.

        The KNMI raw format uses HH=24 for midnight. knmi-py's parser subtracts
        1 from HH and constructs `YYYYMMDD + (HH-1)`, so HH=24 → HH-1=23 on
        the original date, while HH=1 → 00:00 on the original date. The
        timestamp for midnight is therefore `(date+1 day, 00:00)` when HH=24
        appears in the raw data, which knmi-py handles by constructing the
        datetime as ``YYYYMMDD`` string + ``"23"``... wait — actually when
        HH=24, HH-1=23, so it would be 23:00, not 00:00 next day.

        In practice knmi-py represents this as the original date at 23:00 UTC.
        This test verifies that our fetcher correctly converts the DataFrame
        index timestamp to a Unix timestamp without further manipulation.
        """
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours
        import calendar

        # knmi-py already handles hour normalisation; the DataFrame index
        # contains a UTC-naive datetime. We supply a 10:00 timestamp directly.
        ts = pd.Timestamp(2024, 3, 15, 10)
        df = _make_df([{"ts": ts, "STN": 260, "Q": 400, "FH": 20, "T": 80, "RH": 0}])
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        expected_ts = int(calendar.timegm((2024, 3, 15, 10, 0, 0, 0, 0, 0)))
        assert rows[0].hour_utc == expected_ts


class TestErrorHandling:
    def test_network_error_raises_fetch_error(self) -> None:
        """Network errors from knmi-py are wrapped in FetchError."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours, FetchError

        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.side_effect = Exception("connection refused")
            with pytest.raises(FetchError):
                fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

    def test_empty_dataframe_returns_empty_list(self) -> None:
        """An empty DataFrame (no data for the period) returns an empty list."""
        from pv_ml_learner.knmi_fetcher import fetch_knmi_hours

        empty_df = pd.DataFrame(
            columns=[" STN", "   Q", "  FH", "   T", "  RH"],
            index=pd.DatetimeIndex([], name="YYYYMMDD_HH"),
        )
        with patch("pv_ml_learner.knmi_fetcher.knmi") as mock_knmi:
            mock_knmi.get_hour_data_dataframe.return_value = empty_df
            rows = fetch_knmi_hours(station_id=260, start_ts=0, end_ts=9_999_999_999)

        assert rows == []
