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
# A fixed "now" that falls mid-quarter (10:17), used for quarterly-step tests.
_NOW_MID = datetime(2026, 5, 28, 10, 17, 0, tzinfo=timezone.utc)


def _make_raw_entry(start_utc: datetime, price_raw: int, price_excl_raw: int) -> dict:
    """Build a raw Zonneplan price_per_hour entry."""
    return {
        "datetime": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000000Z"),
        "electricity_price": price_raw,
        "electricity_price_excl_tax": price_excl_raw,
        "tariff_group": "low",
        "solar_percentage": 0,
        "solar_yield": 0,
        "sustainability_score": 1000,
    }


def _make_client(entries: list[dict]) -> MagicMock:
    """Build a mock ZonneplanClient that returns the given price_per_hour list."""
    client = MagicMock()
    client.get_summary.return_value = {"price_per_hour": entries}
    return client


class TestFetchPrices:
    def test_all_future_steps_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_raw_entry(_NOW + timedelta(hours=i), 1_000_000, 500_000)
            for i in range(5)
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 5

    def test_past_steps_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [
            _make_raw_entry(_NOW - timedelta(hours=1), 1_000_000, 500_000),  # past — excluded
            _make_raw_entry(_NOW, 1_000_000, 500_000),                        # current hour — included
            _make_raw_entry(_NOW + timedelta(hours=1), 1_000_000, 500_000),   # future — included
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 2

    def test_price_scale_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_546_185, 437_704)]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        assert len(steps) == 1
        assert abs(steps[0]["import_eur_per_kwh"] - 1_546_185 * _SCALE) < 1e-10

    def test_price_excl_tax_scale_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_546_185, 437_704)]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price_excl_tax",
            export_formula="0.0",
        )
        assert abs(steps[0]["import_eur_per_kwh"] - 437_704 * _SCALE) < 1e-10

    def test_import_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_000_000, 800_000)]  # price = 0.1, excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price * 1.1",
            export_formula="0.0",
        )
        expected = 1_000_000 * _SCALE * 1.1
        assert abs(steps[0]["import_eur_per_kwh"] - expected) < 1e-10

    def test_excl_tax_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_000_000, 800_000)]  # excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price_excl_tax * 1.21 + 0.05",
            export_formula="0.0",
        )
        expected = 800_000 * _SCALE * 1.21 + 0.05
        assert abs(steps[0]["import_eur_per_kwh"] - expected) < 1e-10

    def test_export_formula_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_000_000, 800_000)]  # excl = 0.08
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="price_excl_tax * 0.8",
        )
        expected = 800_000 * _SCALE * 0.8
        assert abs(steps[0]["export_eur_per_kwh"] - expected) < 1e-10

    def test_empty_price_list_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        steps = fetch_prices(
            client=_make_client([]),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        assert steps == []

    def test_confidence_always_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_000_000, 500_000)]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        assert steps[0]["confidence"] == 1.0

    def test_ts_field_is_iso8601_utc(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        entries = [_make_raw_entry(_NOW, 1_000_000, 500_000)]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        # Must be parseable and UTC-aware.
        parsed = datetime.fromisoformat(steps[0]["ts"])
        assert parsed.tzinfo is not None

    def test_fetch_error_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        client = MagicMock()
        client.get_summary.side_effect = FetchError("network failure")
        with pytest.raises(FetchError):
            fetch_prices(
                client=client,
                connection_uuid="conn-1",
                import_formula="price",
                export_formula="0.0",
            )

    def test_quarterly_past_steps_excluded_mid_quarter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Quarterly steps before the current 15-min block are excluded.

        When 'now' is 10:17, floor_to_15min gives 10:15. Steps at 10:00 and
        earlier must be excluded; the step at 10:15 (the current slot) and
        beyond must be included.
        """
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW_MID))
        entries = [
            _make_raw_entry(_NOW_MID.replace(minute=0, second=0),  1_000_000, 500_000),  # 10:00 — past
            _make_raw_entry(_NOW_MID.replace(minute=15, second=0), 1_000_000, 500_000),  # 10:15 — current
            _make_raw_entry(_NOW_MID.replace(minute=30, second=0), 1_000_000, 500_000),  # 10:30 — future
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        tss = [s["ts"] for s in steps]
        assert len(steps) == 2, f"Expected 2 steps (10:15 + 10:30), got: {tss}"
        assert all("T10:1" in ts or "T10:3" in ts for ts in tss)

    def test_steps_sorted_by_ts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("zonneplan_prices.fetcher.datetime", _make_datetime_mock(_NOW))
        # Supply entries in reverse order to verify sorting.
        entries = [
            _make_raw_entry(_NOW + timedelta(hours=2), 1_000_000, 500_000),
            _make_raw_entry(_NOW + timedelta(hours=0), 1_000_000, 500_000),
            _make_raw_entry(_NOW + timedelta(hours=1), 1_000_000, 500_000),
        ]
        steps = fetch_prices(
            client=_make_client(entries),
            connection_uuid="conn-1",
            import_formula="price",
            export_formula="0.0",
        )
        tss = [s["ts"] for s in steps]
        assert tss == sorted(tss)


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
