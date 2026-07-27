"""Unit tests for zonneplan_prices.fetcher.

The ZonneplanClient is mocked to isolate the fetcher from live network calls.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from zonneplan_prices.api import FetchError
from zonneplan_prices.fetcher import fetch_prices


# Zonneplan raw price scale: integer × 0.0000001 = EUR/kWh.
_SCALE = 0.0000001

# A fixed "now" used throughout — always a round UTC hour.
_NOW = datetime(2026, 5, 28, 10, 0, 0, tzinfo=timezone.utc)

# A second fixed "now" — 47 minutes into the hour. Used to reproduce the
# regression scenario from the earlier (reverted) fixed time-floor approach:
# an entry that started before "now" but has not yet ended must still be
# included, however far "now" is into its interval.
_NOW_MID = datetime(2026, 5, 28, 10, 47, 0, tzinfo=timezone.utc)


def _make_price_entry(
    start_utc: datetime, end_utc: datetime, price_raw: int, price_excl_raw: int
) -> dict:
    """Build a raw Zonneplan consumer-prices chart entry."""
    return {
        "start_date": start_utc.isoformat(),
        "end_date": end_utc.isoformat(),
        "price_tax_included": {"amount": price_raw},
        "price_tax_excluded": {"amount": price_excl_raw},
        "tariff_group": "low",
        "sustainability_score": {"permille": 1000},
    }


def _make_client(entries: list[dict]) -> MagicMock:
    """Build a mock ZonneplanClient that returns the given price entries."""
    client = MagicMock()
    client.get_consumer_prices.return_value = {
        "chart": {"series": {"prices": entries}}
    }
    return client


def _make_datetime_mock(now: datetime) -> MagicMock:
    """Return a mock that replaces datetime in the fetcher module.

    ``datetime.now(tz=timezone.utc)`` returns the fixed ``now`` value.
    All other uses (e.g. ``datetime.fromisoformat``) are forwarded to the real
    ``datetime`` class so parsing still works.
    """
    mock = MagicMock(wraps=datetime)
    mock.now.return_value = now
    mock.fromisoformat.side_effect = datetime.fromisoformat
    return mock


class TestFetchPrices:
    def test_all_future_steps_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(
                _NOW + timedelta(hours=i),
                _NOW + timedelta(hours=i + 1),
                1_000_000,
                500_000,
            )
            for i in range(5)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 5

    def test_expired_entry_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            # Fully elapsed — end_date in the past — excluded.
            _make_price_entry(
                _NOW - timedelta(hours=2), _NOW - timedelta(hours=1), 1_000_000, 500_000
            ),
            # Current step, not yet elapsed — included.
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 500_000),
            # Future — included.
            _make_price_entry(
                _NOW + timedelta(hours=1), _NOW + timedelta(hours=2), 1_000_000, 500_000
            ),
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 2

    def test_in_progress_entry_included(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression test: an entry already 47 minutes underway, but not yet
        ended, must still be included. A fixed time-floor cutoff (the earlier,
        reverted approach) would incorrectly exclude this entry, producing no
        price data until the next batch arrives."""
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW_MID))
        entries = [
            _make_price_entry(
                _NOW, _NOW + timedelta(hours=1), 1_000_000, 500_000
            ),  # 10:00–11:00, now is 10:47 — still in progress.
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 1

    def test_price_scale_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_546_185, 437_704)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 1
        assert abs(steps[0]["import_eur_per_kwh"] - 1_546_185 * _SCALE) < 1e-10

    def test_price_excl_tax_scale_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_546_185, 437_704)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price_excl_tax",
            export_formula="0.0",
        )
        assert abs(steps[0]["import_eur_per_kwh"] - 437_704 * _SCALE) < 1e-10

    def test_import_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 800_000)
        ]  # price = 0.1, excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price * 1.1",
            export_formula="0.0",
        )
        expected = 1_000_000 * _SCALE * 1.1
        assert abs(steps[0]["import_eur_per_kwh"] - expected) < 1e-10

    def test_excl_tax_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 800_000)
        ]  # excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price_excl_tax * 1.21 + 0.05",
            export_formula="0.0",
        )
        expected = 800_000 * _SCALE * 1.21 + 0.05
        assert abs(steps[0]["import_eur_per_kwh"] - expected) < 1e-10

    def test_export_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 800_000)
        ]  # excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="price_excl_tax * 0.8",
        )
        expected = 800_000 * _SCALE * 0.8
        assert abs(steps[0]["export_eur_per_kwh"] - expected) < 1e-10

    def test_empty_price_list_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        steps = fetch_prices(
            client=_make_client([]),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert steps == []

    def test_confidence_always_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 500_000)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert steps[0]["confidence"] == 1.0

    def test_ts_field_is_iso8601_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(_NOW, _NOW + timedelta(hours=1), 1_000_000, 500_000)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        # Must be parseable and UTC-aware.
        parsed = datetime.fromisoformat(steps[0]["ts"])
        assert parsed.tzinfo is not None

    def test_fetch_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        client = MagicMock()
        client.get_consumer_prices.side_effect = FetchError("network failure")
        with pytest.raises(FetchError):
            fetch_prices(
                client=client,
                price_interval="hourly",
                import_formula="price",
                export_formula="0.0",
            )

    def test_steps_sorted_by_ts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        # Supply entries in reverse order to verify sorting.
        entries = [
            _make_price_entry(
                _NOW + timedelta(hours=2), _NOW + timedelta(hours=3), 1_000_000, 500_000
            ),
            _make_price_entry(
                _NOW + timedelta(hours=0), _NOW + timedelta(hours=1), 1_000_000, 500_000
            ),
            _make_price_entry(
                _NOW + timedelta(hours=1), _NOW + timedelta(hours=2), 1_000_000, 500_000
            ),
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        tss = [s["ts"] for s in steps]
        assert tss == sorted(tss)

    def test_chart_name_mapping_hourly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        client = _make_client([])
        fetch_prices(
            client=client,
            price_interval="hourly",
            import_formula="price",
            export_formula="0.0",
        )
        client.get_consumer_prices.assert_called_once_with("electricity-hourly")

    def test_chart_name_mapping_quarter_hourly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        client = _make_client([])
        fetch_prices(
            client=client,
            price_interval="quarter_hourly",
            import_formula="price",
            export_formula="0.0",
        )
        client.get_consumer_prices.assert_called_once_with("electricity-quarter-hourly")

    def test_quarterly_steps_all_returned_distinctly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_price_entry(
                _NOW + timedelta(minutes=15 * i),
                _NOW + timedelta(minutes=15 * (i + 1)),
                1_000_000 + i,
                500_000 + i,
            )
            for i in range(4)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            price_interval="quarter_hourly",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 4
        tss = [s["ts"] for s in steps]
        assert len(set(tss)) == 4  # all four steps are distinct
