"""Unit tests for epexpredictor_prices.fetcher.

requests.get is mocked so no real HTTP traffic is made. Tests verify correct
URL and query-parameter construction (in particular that surcharge/tax are
never derived from configuration), response parsing into FetchResult, the
past-step truncation, and error handling.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from epexpredictor_prices.fetcher import FetchError, FetchResult, fetch_raw_prices


_BASE_URL = "https://epexpredictor.batzill.com"


def _make_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


def _prices_body(entries: list[tuple[str, float]], known_until: str) -> dict:
    return {
        "prices": [{"startsAt": ts, "total": total} for ts, total in entries],
        "knownUntil": known_until,
    }


class TestRequestConstruction:
    def test_always_requests_eur_per_kwh_and_utc(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        params = mock_get.call_args.kwargs["params"]
        assert params["unit"] == "EUR_PER_KWH"
        assert params["timezone"] == "UTC"

    def test_never_sends_nonzero_surcharge_or_tax(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        params = mock_get.call_args.kwargs["params"]
        # Either omitted entirely, or explicitly zero — never anything else.
        assert params.get("surcharge", 0.0) == 0.0
        assert params.get("taxPercent", 0.0) == 0.0

    def test_region_param_equals_configured_area(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="DE", horizon_hours=72, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        assert mock_get.call_args.kwargs["params"]["region"] == "DE"

    def test_hours_param_equals_configured_horizon(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=168, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        assert mock_get.call_args.kwargs["params"]["hours"] == 168

    def test_hourly_interval_sends_hourly_true(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert mock_get.call_args.kwargs["params"]["hourly"] is True

    def test_quarter_hourly_interval_sends_hourly_false(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        assert mock_get.call_args.kwargs["params"]["hourly"] is False

    def test_url_built_from_default_base_url(self) -> None:
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="quarter_hourly", base_url=_BASE_URL
            )
        assert mock_get.call_args.args[0] == f"{_BASE_URL}/prices"

    def test_url_built_from_custom_self_hosted_base_url(self) -> None:
        custom = "https://self-hosted.example.com"
        resp = _make_response(200, _prices_body([], "2026-09-10T21:45:00Z"))
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp) as mock_get:
            fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="quarter_hourly", base_url=custom
            )
        assert mock_get.call_args.args[0] == f"{custom}/prices"


class TestResponseParsing:
    def test_returns_sorted_utc_aware_steps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "epexpredictor_prices.fetcher.datetime",
            _make_datetime_mock(now),
        )
        body = _prices_body(
            [
                ("2026-09-10T12:00:00Z", 0.15),
                ("2026-09-10T11:00:00Z", 0.10),
            ],
            "2026-09-10T21:45:00Z",
        )
        resp = _make_response(200, body)
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            result = fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert isinstance(result, FetchResult)
        assert [ts for ts, _ in result.steps] == sorted(ts for ts, _ in result.steps)
        assert all(ts.tzinfo is not None for ts, _ in result.steps)
        assert result.steps == [
            (datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc), 0.10),
            (datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc), 0.15),
        ]

    def test_known_until_parsed_as_utc_aware(self, monkeypatch: pytest.MonkeyPatch) -> None:
        now = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "epexpredictor_prices.fetcher.datetime",
            _make_datetime_mock(now),
        )
        body = _prices_body([("2026-09-10T11:00:00Z", 0.10)], "2026-09-10T21:45:00Z")
        resp = _make_response(200, body)
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            result = fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert result.known_until == datetime(2026, 9, 10, 21, 45, tzinfo=timezone.utc)

    def test_steps_before_current_utc_hour_are_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 9, 10, 11, 30, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "epexpredictor_prices.fetcher.datetime",
            _make_datetime_mock(now),
        )
        body = _prices_body(
            [
                ("2026-09-10T10:00:00Z", 0.05),  # before current hour -> excluded
                ("2026-09-10T11:00:00Z", 0.10),  # current hour -> included
                ("2026-09-10T12:00:00Z", 0.15),  # future -> included
            ],
            "2026-09-10T21:45:00Z",
        )
        resp = _make_response(200, body)
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            result = fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert len(result.steps) == 2
        assert result.steps[0][0] == datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)

    def test_known_until_unaffected_by_truncation_even_when_in_the_past(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = datetime(2026, 9, 10, 23, 0, tzinfo=timezone.utc)
        monkeypatch.setattr(
            "epexpredictor_prices.fetcher.datetime",
            _make_datetime_mock(now),
        )
        body = _prices_body(
            [("2026-09-10T23:00:00Z", 0.10)],
            "2026-09-10T21:45:00Z",  # known_until is before "now"
        )
        resp = _make_response(200, body)
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            result = fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert result.known_until == datetime(2026, 9, 10, 21, 45, tzinfo=timezone.utc)

    def test_empty_prices_list_returns_empty_steps_with_known_until(self) -> None:
        body = _prices_body([], "2026-09-10T21:45:00Z")
        resp = _make_response(200, body)
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            result = fetch_raw_prices(
                area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
            )
        assert result.steps == []
        assert result.known_until == datetime(2026, 9, 10, 21, 45, tzinfo=timezone.utc)


class TestErrorHandling:
    def test_non_2xx_response_raises_fetch_error(self) -> None:
        resp = _make_response(500, {})
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            with pytest.raises(FetchError):
                fetch_raw_prices(
                    area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
                )

    def test_missing_prices_key_raises_fetch_error(self) -> None:
        resp = _make_response(200, {"knownUntil": "2026-09-10T21:45:00Z"})
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            with pytest.raises(FetchError):
                fetch_raw_prices(
                    area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
                )

    def test_missing_known_until_key_raises_fetch_error(self) -> None:
        resp = _make_response(200, {"prices": []})
        with patch("epexpredictor_prices.fetcher.requests.get", return_value=resp):
            with pytest.raises(FetchError):
                fetch_raw_prices(
                    area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
                )

    def test_network_exception_raises_fetch_error(self) -> None:
        import requests

        with patch(
            "epexpredictor_prices.fetcher.requests.get",
            side_effect=requests.ConnectionError("network down"),
        ):
            with pytest.raises(FetchError):
                fetch_raw_prices(
                    area="NL", horizon_hours=72, price_interval="hourly", base_url=_BASE_URL
                )


def _make_datetime_mock(now: datetime) -> MagicMock:
    """Return a mock that replaces datetime in the fetcher module.

    ``datetime.now(tz=timezone.utc)`` returns the fixed ``now`` value. All
    other uses (``datetime.fromisoformat``) are forwarded to the real class.
    """
    mock = MagicMock(wraps=datetime)
    mock.now.return_value = now
    mock.fromisoformat.side_effect = datetime.fromisoformat
    return mock
