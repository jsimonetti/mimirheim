"""Unit tests for zonneplan_prices.api.

Network calls are intercepted using unittest.mock so no real HTTP traffic
is made. Tests verify correct URL construction, request headers, and error
handling for each client method.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from zonneplan_prices.api import AuthError, FetchError, ZonneplanClient


_BASE_URL = "https://app-api.zonneplan.nl"


def _make_response(status_code: int, json_body: dict) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status.side_effect = (
        None if status_code < 400 else Exception(f"HTTP {status_code}")
    )
    return resp


class TestRequestLoginEmail:
    def test_calls_correct_url_and_returns_uuid(self) -> None:
        client = ZonneplanClient(access_token=None)
        resp = _make_response(200, {"data": {"uuid": "auth-uuid-123"}})
        with patch("zonneplan_prices.api.requests.post", return_value=resp) as mock_post:
            uuid = client.request_login_email("user@example.com")
        assert uuid == "auth-uuid-123"
        args, kwargs = mock_post.call_args
        assert args[0] == f"{_BASE_URL}/auth/request"
        assert kwargs["json"] == {"email": "user@example.com"}

    def test_raises_fetch_error_on_http_failure(self) -> None:
        client = ZonneplanClient(access_token=None)
        resp = _make_response(500, {})
        with patch("zonneplan_prices.api.requests.post", return_value=resp):
            with pytest.raises(FetchError):
                client.request_login_email("user@example.com")


class TestPollActivation:
    def test_returns_none_when_not_activated(self) -> None:
        client = ZonneplanClient(access_token=None)
        resp = _make_response(200, {"data": {"is_activated": False}})
        with patch("zonneplan_prices.api.requests.get", return_value=resp):
            result = client.poll_activation("req-uuid")
        assert result is None

    def test_returns_token_dict_when_activated(self) -> None:
        client = ZonneplanClient(access_token=None)
        otp = "one-time-pass"
        # poll_activation returns the OTP password; the token exchange is separate.
        resp = _make_response(200, {"data": {"is_activated": True, "password": otp}})
        token_resp = _make_response(200, {
            "access_token": "act",
            "refresh_token": "rft",
            "token_type": "Bearer",
            "expires_in": 3600,
        })
        with patch("zonneplan_prices.api.requests.get", return_value=resp), \
             patch("zonneplan_prices.api.requests.post", return_value=token_resp):
            result = client.poll_activation("req-uuid", "user@example.com")
        assert result is not None
        assert result["access_token"] == "act"

    def test_activated_exchange_includes_email_in_token_request(self) -> None:
        client = ZonneplanClient(access_token=None)
        otp = "one-time-pass"
        resp = _make_response(200, {"data": {"is_activated": True, "password": otp}})
        token_resp = _make_response(200, {
            "access_token": "act",
            "refresh_token": "rft",
            "token_type": "Bearer",
            "expires_in": 3600,
        })
        with patch("zonneplan_prices.api.requests.get", return_value=resp), \
             patch("zonneplan_prices.api.requests.post", return_value=token_resp) as mock_post:
            client.poll_activation("req-uuid", "user@example.com")
        args, kwargs = mock_post.call_args
        assert args[0].endswith("/oauth/token")
        assert kwargs["json"]["email"] == "user@example.com"


class TestGetSummary:
    def test_calls_correct_url_with_bearer_token(self) -> None:
        client = ZonneplanClient(access_token="my-token")
        price_data = {"price_per_hour": []}
        resp = _make_response(200, {"data": price_data})
        with patch("zonneplan_prices.api.requests.get", return_value=resp) as mock_get:
            result = client.get_summary("conn-uuid-abc")
        args, kwargs = mock_get.call_args
        assert args[0] == f"{_BASE_URL}/connections/conn-uuid-abc/summary"
        assert kwargs["headers"]["Authorization"] == "Bearer my-token"
        assert result == price_data

    def test_raises_fetch_error_on_http_failure(self) -> None:
        client = ZonneplanClient(access_token="tok")
        resp = _make_response(503, {})
        with patch("zonneplan_prices.api.requests.get", return_value=resp):
            with pytest.raises(FetchError):
                client.get_summary("conn-uuid")


class TestRefreshToken:
    def test_raises_auth_error_on_http_failure(self) -> None:
        client = ZonneplanClient(access_token=None)
        resp = _make_response(401, {"error": "invalid_grant"})
        with patch("zonneplan_prices.api.requests.post", return_value=resp):
            with pytest.raises(AuthError):
                client.refresh_token("bad-refresh-token")

    def test_returns_token_dict_on_success(self) -> None:
        client = ZonneplanClient(access_token=None)
        token = {"access_token": "new", "refresh_token": "newr", "token_type": "Bearer", "expires_in": 3600}
        resp = _make_response(200, token)
        with patch("zonneplan_prices.api.requests.post", return_value=resp):
            result = client.refresh_token("good-refresh-token")
        assert result["access_token"] == "new"


class TestGetConnectionUuid:
    def test_returns_first_electricity_uuid(self) -> None:
        client = ZonneplanClient(access_token="tok")
        account_data = {
            "data": {
                "address_groups": [{
                    "connections": [
                        {"uuid": "elec-uuid", "market_segment": "electricity"},
                        {"uuid": "gas-uuid", "market_segment": "gas"},
                    ]
                }]
            }
        }
        resp = _make_response(200, account_data)
        with patch("zonneplan_prices.api.requests.get", return_value=resp):
            uuid = client.get_connection_uuid()
        assert uuid == "elec-uuid"

    def test_raises_fetch_error_when_no_electricity_connection(self) -> None:
        client = ZonneplanClient(access_token="tok")
        account_data = {
            "data": {
                "address_groups": [{
                    "connections": [
                        {"uuid": "gas-uuid", "market_segment": "gas"},
                    ]
                }]
            }
        }
        resp = _make_response(200, account_data)
        with patch("zonneplan_prices.api.requests.get", return_value=resp):
            with pytest.raises(FetchError, match="electricity"):
                client.get_connection_uuid()
