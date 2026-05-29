"""Persist and validate Zonneplan OAuth tokens and pending-auth state.

This module handles all file I/O for the two state files the daemon needs:

- The token file stores the current OAuth access token, refresh token, and an
  explicit ``expires_at`` timestamp so callers can check validity without
  contacting the server.

- The pending-auth file stores the Zonneplan auth-request UUID while the user
  is expected to click the activation link in their login email. It survives
  container restarts so the daemon can resume polling the same UUID rather than
  sending a new login email on every restart.

This module does not make network calls and does not import from any other
zonneplan_prices module.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Safety margin subtracted from expires_in when computing expires_at.
# Ensures tokens are refreshed before they actually expire, accommodating
# clock skew and slow network conditions.
_EXPIRY_MARGIN_SECONDS = 60

# Maximum age of a pending-auth file before its OTP is considered expired.
# Zonneplan OTP links expire after roughly 5 minutes; 4 minutes gives a
# safety margin to complete the token exchange before the OTP becomes invalid.
_PENDING_FRESH_SECONDS = 4 * 60


def load_token(path: Path) -> dict | None:
    """Load a token dict from disk.

    Args:
        path: Path to the JSON token file.

    Returns:
        The parsed token dict, or None if the file is absent or cannot be parsed.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read token file %s: %s", path, exc)
        return None


def save_token(path: Path, token: dict) -> None:
    """Save a token dict to disk, adding an ``expires_at`` field.

    The ``expires_at`` field is computed as ``now + expires_in - margin`` so
    that callers can check validity with :func:`is_token_valid` without
    requiring a server round-trip.

    Args:
        path: Destination path for the JSON file.
        token: Token dict from the Zonneplan OAuth endpoint. Must contain
            ``access_token``, ``refresh_token``, ``token_type``, and
            ``expires_in`` (seconds as int).
    """
    expires_in = int(token.get("expires_in", 0))
    expires_at = datetime.now(tz=timezone.utc) + timedelta(
        seconds=expires_in - _EXPIRY_MARGIN_SECONDS
    )
    data = {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
        "token_type": token.get("token_type", "Bearer"),
        "expires_at": expires_at.isoformat(),
    }
    path.write_text(json.dumps(data, indent=2))


def is_token_valid(token: dict) -> bool:
    """Return True if the token's ``expires_at`` is in the future.

    Args:
        token: Token dict previously saved by :func:`save_token`. Must contain
            an ``expires_at`` ISO 8601 string.

    Returns:
        True if the token is still valid, False if it has expired or
        ``expires_at`` is absent.
    """
    expires_at_str = token.get("expires_at")
    if not expires_at_str:
        return False
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
    except ValueError:
        return False
    return datetime.now(tz=timezone.utc) < expires_at


def load_pending(path: Path) -> dict | None:
    """Load a pending-auth state dict from disk.

    Args:
        path: Path to the pending-auth JSON file.

    Returns:
        The parsed pending dict, or None if the file is absent or unreadable.
    """
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read pending-auth file %s: %s", path, exc)
        return None


def save_pending(path: Path, uuid: str, email: str) -> None:
    """Save a pending-auth state file.

    Args:
        path: Destination path for the JSON file.
        uuid: The auth-request UUID returned by ``POST /auth/request``.
        email: The email address used to request the login email, stored so
            the pending state is self-describing.
    """
    data = {
        "uuid": uuid,
        "email": email,
        "requested_at": datetime.now(tz=timezone.utc).isoformat(),
    }
    path.write_text(json.dumps(data, indent=2))


def delete_pending(path: Path) -> None:
    """Delete the pending-auth state file if it exists.

    Args:
        path: Path to the pending-auth JSON file to delete. If the file does
            not exist this function returns silently without raising.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_pending_fresh(pending: dict) -> bool:
    """Return True if the pending-auth state is within the OTP validity window.

    Zonneplan OTP links expire after roughly 5 minutes. This function returns
    True only if the ``requested_at`` timestamp in the pending dict is less than
    4 minutes old, giving a safety margin to complete the token exchange before
    the OTP becomes invalid.

    Args:
        pending: Pending-auth dict previously saved by :func:`save_pending`.
            Must contain a ``requested_at`` ISO 8601 string.

    Returns:
        True if the pending state is still fresh, False if it is stale.
    """
    requested_at_str = pending.get("requested_at")
    if not requested_at_str:
        return False
    try:
        requested_at = datetime.fromisoformat(requested_at_str)
    except ValueError:
        return False
    age = datetime.now(tz=timezone.utc) - requested_at
    return age.total_seconds() < _PENDING_FRESH_SECONDS
