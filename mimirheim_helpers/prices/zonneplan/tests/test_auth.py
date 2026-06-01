"""Unit tests for zonneplan_prices.auth.

The ZonneplanClient and file-system calls are mocked so no network or disk I/O
occurs in these tests.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from zonneplan_prices.api import AuthError
from zonneplan_prices.auth import attempt_auth


def _make_client(
    *,
    login_uuid: str = "req-uuid",
    poll_result: dict | None = None,
) -> MagicMock:
    """Build a mock ZonneplanClient for auth tests."""
    client = MagicMock()
    client.request_login_email.return_value = login_uuid
    client.poll_activation.return_value = poll_result
    return client


def _write_pending(path: Path, uuid: str, email: str, age_seconds: int = 60) -> None:
    """Write a pending-auth file with requested_at set age_seconds in the past."""
    requested_at = (datetime.now(tz=timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    path.write_text(json.dumps({"uuid": uuid, "email": email, "requested_at": requested_at}))


class TestAttemptAuth:
    def test_sends_email_when_no_pending_file(self, tmp_path: Path) -> None:
        client = _make_client(poll_result=None)
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"

        result = attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=0,
        )

        client.request_login_email.assert_called_once_with("user@example.com")
        assert pending_path.exists()
        assert result is None

    def test_does_not_send_new_email_when_fresh_pending_exists(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        _write_pending(pending_path, uuid="existing-uuid", email="user@example.com", age_seconds=60)
        client = _make_client(login_uuid="new-uuid", poll_result=None)

        attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=0,
        )

        client.request_login_email.assert_not_called()

    def test_resumes_polling_existing_uuid_on_restart(self, tmp_path: Path) -> None:
        """Simulates a container restart mid-auth: pending file exists, no token."""
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        _write_pending(pending_path, uuid="persisted-uuid", email="user@example.com", age_seconds=30)
        client = _make_client(poll_result=None)

        attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=1,
        )

        # Must poll the persisted UUID, not send a new email.
        client.request_login_email.assert_not_called()
        assert client.poll_activation.call_args_list[0] == call("persisted-uuid", "user@example.com")

    def test_deletes_stale_pending_and_sends_new_email(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        # Stale: 10 minutes old (OTP expired).
        _write_pending(pending_path, uuid="old-uuid", email="user@example.com", age_seconds=600)
        client = _make_client(login_uuid="new-uuid", poll_result=None)

        attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=0,
        )

        client.request_login_email.assert_called_once_with("user@example.com")
        # New pending file written with the new UUID.
        data = json.loads(pending_path.read_text())
        assert data["uuid"] == "new-uuid"

    def test_returns_token_when_activation_succeeds(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        _write_pending(pending_path, uuid="req-uuid", email="user@example.com", age_seconds=30)
        activated_token = {
            "access_token": "act",
            "refresh_token": "rft",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        client = _make_client(poll_result=activated_token)

        result = attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=1,
        )

        assert result is not None
        assert result["access_token"] == "act"
        assert token_path.exists()
        assert not pending_path.exists()

    def test_returns_none_when_no_activation_in_window(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        _write_pending(pending_path, uuid="req-uuid", email="user@example.com", age_seconds=30)
        client = _make_client(poll_result=None)

        result = attempt_auth(
            client=client,
            email="user@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=1,
        )

        assert result is None
        # Pending file must still exist so the next cycle can resume.
        assert pending_path.exists()

    def test_uses_pending_email_when_resuming_poll(self, tmp_path: Path) -> None:
        token_path = tmp_path / "token.json"
        pending_path = tmp_path / "token_pending.json"
        _write_pending(pending_path, uuid="req-uuid", email="stored@example.com", age_seconds=30)
        client = _make_client(poll_result=None)

        attempt_auth(
            client=client,
            email="config@example.com",
            token_path=token_path,
            pending_path=pending_path,
            poll_window_seconds=1,
        )

        assert client.poll_activation.call_args_list[0] == call("req-uuid", "stored@example.com")
