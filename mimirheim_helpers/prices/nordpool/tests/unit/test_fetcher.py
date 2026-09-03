"""Unit tests for nordpool.fetcher.

The NordPoolClient is mocked to isolate the fetcher from live network calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pynordpool.exceptions import NordPoolError

from nordpool.fetcher import FetchError, fetch_prices


def _make_entry(start_utc: datetime, price_eur_per_mwh: float, area: str = "NO2") -> MagicMock:
    """Build a mock DeliveryPeriodEntry."""
    entry = MagicMock()
    entry.start = start_utc
    entry.end = start_utc + timedelta(hours=1)
    entry.entry = {area: price_eur_per_mwh}
    return entry


def _make_day_data(entries: list) -> MagicMock:
    """Build a mock DeliveryPeriodData."""
    day = MagicMock()
    day.entries = entries
    return day


def _make_periods_data(days: list) -> MagicMock:
    """Build a mock DeliveryPeriodsData."""
    periods = MagicMock()
    # pynordpool 0.4.0: entries is dict[date, DeliveryPeriodData], keyed by delivery date.
    # The fetcher iterates .values(), so the keys are irrelevant for these tests.
    periods.entries = {i: day for i, day in enumerate(days)}
    return periods


# A fixed "now" used throughout these tests — always a round UTC hour.
_NOW = datetime(2026, 3, 30, 14, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch NordPoolClient so no real HTTP calls are made."""
    client_instance = AsyncMock()
    client_cls = MagicMock(return_value=client_instance)
    monkeypatch.setattr("nordpool.fetcher.NordPoolClient", client_cls)
    return client_instance


class TestFetchPrices:
    async def test_returns_steps_from_today_and_tomorrow(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "nordpool.fetcher.datetime",
            _make_datetime_mock(_NOW),
        )
        today_entries = [_make_entry(_NOW + timedelta(hours=i), 50.0) for i in range(10)]
        tomorrow_entries = [
            _make_entry(_NOW + timedelta(hours=10 + i), 45.0) for i in range(24)
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(today_entries), _make_day_data(tomorrow_entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
        )
        assert len(steps) == 34
        assert steps[0]["ts"] == _NOW.isoformat()

    async def test_filters_out_past_hours(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_entry(_NOW - timedelta(hours=2), 50.0),  # in the past
                   _make_entry(_NOW, 50.0),                        # current hour — included
                   _make_entry(_NOW + timedelta(hours=1), 50.0)]   # future — included
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
        )
        assert len(steps) == 2
        assert steps[0]["ts"] == _NOW.isoformat()

    async def test_applies_import_formula(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_entry(_NOW, 100.0)]  # 100 EUR/MWh = 0.1 EUR/kWh
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        # Tibber-style: ((price + 0.09161) * 1.21) + 0.0248
        steps = await fetch_prices(
            area="NO2",
            import_formula="((price + 0.09161) * 1.21) + 0.0248",
            export_formula="price",
        )
        expected = ((0.1 + 0.09161) * 1.21) + 0.0248
        assert steps[0]["import_eur_per_kwh"] == pytest.approx(expected)
        assert steps[0]["export_eur_per_kwh"] == pytest.approx(0.1)

    async def test_applies_export_formula(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_entry(_NOW, 100.0)]  # 0.1 EUR/kWh
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price * 0.9",
        )
        assert steps[0]["import_eur_per_kwh"] == pytest.approx(0.1)
        assert steps[0]["export_eur_per_kwh"] == pytest.approx(0.09)

    async def test_confidence_is_always_one(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_entry(_NOW, 80.0)]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
        )
        assert steps[0]["confidence"] == 1.0

    async def test_raises_fetch_error_on_nordpool_error(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        mock_client.async_get_delivery_periods.side_effect = NordPoolError("down")
        with pytest.raises(FetchError):
            await fetch_prices(
                area="NO2",
                import_formula="price",
                export_formula="price",
            )

    async def test_raises_fetch_error_when_area_missing_from_response(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_entry(_NOW, 80.0, area="SE3")]  # SE3, asked for NO2
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        with pytest.raises(FetchError, match="NO2"):
            await fetch_prices(
                area="NO2",
                import_formula="price",
                export_formula="price",
            )

    async def test_steps_are_sorted_by_ts(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        # Deliberately insert in reverse order to verify sorting
        entries_today = [_make_entry(_NOW + timedelta(hours=5), 60.0),
                         _make_entry(_NOW, 50.0)]
        entries_tomorrow = [_make_entry(_NOW + timedelta(hours=10), 40.0)]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries_today), _make_day_data(entries_tomorrow)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
        )
        tss = [s["ts"] for s in steps]
        assert tss == sorted(tss)


class TestPriceInterval:
    """Aggregation of the 15-minute market time unit into hourly steps."""

    async def test_defaults_to_native_15_minute_steps(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
        )
        assert len(steps) == 4
        assert [s["import_eur_per_kwh"] for s in steps] == pytest.approx(
            [0.04, 0.08, 0.12, 0.16]
        )

    async def test_hourly_averages_the_four_quarters(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        # 40, 80, 120, 160 EUR/MWh averages to 100 EUR/MWh = 0.1 EUR/kWh.
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="hourly",
        )
        assert len(steps) == 1
        assert steps[0]["ts"] == _NOW.isoformat()
        assert steps[0]["import_eur_per_kwh"] == pytest.approx(0.1)
        assert steps[0]["export_eur_per_kwh"] == pytest.approx(0.1)

    async def test_hourly_buckets_by_clock_hour(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        # Two full hours: the first averages to 100, the second to 200 EUR/MWh.
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0, 140.0, 180.0, 220.0, 260.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="hourly",
        )
        assert [s["ts"] for s in steps] == [
            _NOW.isoformat(),
            (_NOW + timedelta(hours=1)).isoformat(),
        ]
        assert [s["import_eur_per_kwh"] for s in steps] == pytest.approx([0.1, 0.2])

    async def test_explicit_quarter_hourly_matches_the_default(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="quarter_hourly",
        )
        assert [s["ts"] for s in steps] == [
            (_NOW + timedelta(minutes=15 * i)).isoformat() for i in range(4)
        ]
        assert [s["import_eur_per_kwh"] for s in steps] == pytest.approx(
            [0.04, 0.08, 0.12, 0.16]
        )

    async def test_formula_is_applied_to_the_hourly_average(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        # A non-affine formula distinguishes "average then apply" from
        # "apply then average": mean of the squares is not the square of the mean.
        steps = await fetch_prices(
            area="NO2",
            import_formula="price ** 2",
            export_formula="price",
            price_interval="hourly",
        )
        assert steps[0]["import_eur_per_kwh"] == pytest.approx(0.1**2)

    async def test_hourly_averages_a_partial_bucket_over_what_is_present(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        # The last hour of the horizon only has two of its four quarters.
        entries = [
            _make_entry(_NOW + timedelta(minutes=15 * i), price)
            for i, price in enumerate([40.0, 80.0, 120.0, 160.0, 100.0, 300.0])
        ]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="hourly",
        )
        assert len(steps) == 2
        assert steps[1]["import_eur_per_kwh"] == pytest.approx(0.2)

    async def test_hourly_source_data_is_unchanged_by_hourly_resolution(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        # An area still quoted hourly already has one period per bucket.
        entries = [_make_entry(_NOW + timedelta(hours=i), 50.0 + i) for i in range(3)]
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(entries)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="hourly",
        )
        assert [s["import_eur_per_kwh"] for s in steps] == pytest.approx(
            [0.05, 0.051, 0.052]
        )

    async def test_hourly_steps_span_days_and_stay_sorted(
        self, mock_client: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("nordpool.fetcher.datetime", _make_datetime_mock(_NOW))
        today = [_make_entry(_NOW + timedelta(minutes=15 * i), 100.0) for i in range(4)]
        tomorrow = [
            _make_entry(_NOW + timedelta(hours=1, minutes=15 * i), 200.0)
            for i in range(4)
        ]
        # Tomorrow's day bucket is returned first to prove the output is sorted.
        mock_client.async_get_delivery_periods.return_value = _make_periods_data(
            [_make_day_data(tomorrow), _make_day_data(today)]
        )
        steps = await fetch_prices(
            area="NO2",
            import_formula="price",
            export_formula="price",
            price_interval="hourly",
        )
        assert [s["ts"] for s in steps] == sorted(s["ts"] for s in steps)
        assert [s["import_eur_per_kwh"] for s in steps] == pytest.approx([0.1, 0.2])


def _make_datetime_mock(fixed_now: datetime) -> MagicMock:
    """Return a mock for the datetime class that overrides now() but passes through other calls."""
    dt_mock = MagicMock(wraps=datetime)
    dt_mock.now.return_value = fixed_now
    return dt_mock
