"""Unit tests for zonneplan_prices.token.

Covers load/save/expiry logic and pending-auth state file management.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from zonneplan_prices.token import (
    delete_pending,
    is_pending_fresh,
    is_token_valid,
    load_pending,
    load_token,
    save_pending,
    save_token,
)


class TestLoadToken:
    def test_returns_none_for_absent_file(self, tmp_path: Path) -> None:
        result = load_token(tmp_path / "token.json")
        assert result is None

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text("not valid json{")
        result = load_token(p)
        assert result is None

    def test_returns_dict_for_valid_file(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        expires_at = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
        p.write_text(json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
            "token_type": "Bearer",
            "expires_at": expires_at,
        }))
        result = load_token(p)
        assert result is not None
        assert result["access_token"] == "tok"


class TestSaveToken:
    def test_writes_file_with_expires_at(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        token_response = {
            "access_token": "act",
            "refresh_token": "rft",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        save_token(p, token_response)
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["access_token"] == "act"
        assert "expires_at" in data
        # expires_at should be roughly 59 minutes from now (3600 - 60 = 3540s).
        expires_at = datetime.fromisoformat(data["expires_at"])
        now = datetime.now(tz=timezone.utc)
        delta = expires_at - now
        assert timedelta(seconds=3500) < delta < timedelta(seconds=3600)

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "token.json"
        p.write_text(json.dumps({"access_token": "old"}))
        save_token(p, {"access_token": "new", "refresh_token": "r", "token_type": "Bearer", "expires_in": 300})
        data = json.loads(p.read_text())
        assert data["access_token"] == "new"


class TestIsTokenValid:
    def test_returns_true_for_future_expires_at(self) -> None:
        future = (datetime.now(tz=timezone.utc) + timedelta(hours=1)).isoformat()
        assert is_token_valid({"expires_at": future}) is True

    def test_returns_false_for_past_expires_at(self) -> None:
        past = (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat()
        assert is_token_valid({"expires_at": past}) is False

    def test_returns_false_when_expires_at_missing(self) -> None:
        assert is_token_valid({}) is False


class TestPendingState:
    def test_load_pending_returns_none_for_absent_file(self, tmp_path: Path) -> None:
        result = load_pending(tmp_path / "pending.json")
        assert result is None

    def test_save_pending_writes_file(self, tmp_path: Path) -> None:
        p = tmp_path / "pending.json"
        save_pending(p, uuid="test-uuid", email="a@b.com")
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["uuid"] == "test-uuid"
        assert data["email"] == "a@b.com"
        assert "requested_at" in data

    def test_save_pending_sets_requested_at_to_now(self, tmp_path: Path) -> None:
        p = tmp_path / "pending.json"
        before = datetime.now(tz=timezone.utc)
        save_pending(p, uuid="u", email="e@f.com")
        after = datetime.now(tz=timezone.utc)
        data = json.loads(p.read_text())
        requested_at = datetime.fromisoformat(data["requested_at"])
        assert before <= requested_at <= after

    def test_delete_pending_removes_file(self, tmp_path: Path) -> None:
        p = tmp_path / "pending.json"
        p.write_text(json.dumps({"uuid": "x"}))
        delete_pending(p)
        assert not p.exists()

    def test_delete_pending_is_noop_for_absent_file(self, tmp_path: Path) -> None:
        p = tmp_path / "pending.json"
        delete_pending(p)  # must not raise


class TestIsPendingFresh:
    def test_returns_true_when_requested_at_is_1_minute_ago(self) -> None:
        requested_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=1)).isoformat()
        assert is_pending_fresh({"requested_at": requested_at}) is True

    def test_returns_false_when_requested_at_is_6_minutes_ago(self) -> None:
        requested_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=6)).isoformat()
        assert is_pending_fresh({"requested_at": requested_at}) is False

    def test_boundary_just_inside_fresh(self) -> None:
        # 3 minutes 50 seconds ago — still fresh (< 4 minutes).
        requested_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=3, seconds=50)).isoformat()
        assert is_pending_fresh({"requested_at": requested_at}) is True

    def test_boundary_just_outside_fresh(self) -> None:
        # 4 minutes 10 seconds ago — stale.
        requested_at = (datetime.now(tz=timezone.utc) - timedelta(minutes=4, seconds=10)).isoformat()
        assert is_pending_fresh({"requested_at": requested_at}) is False
