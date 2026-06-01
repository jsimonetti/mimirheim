"""In-daemon authentication flow for the Zonneplan API.

This module implements the email OTP auth flow as a single function,
``attempt_auth``, which is called by the daemon's ``_run_cycle`` whenever a
valid access token is not available.

The flow is deliberately stateless with respect to the in-memory daemon: all
state is persisted to disk (token file and pending-auth file) so the daemon can
resume correctly after a container restart at any point in the flow.

Auth flow summary
-----------------
1. No token file, no pending file: send login email, write pending file, start
   polling. Returns None (still waiting for the user to click the link).
2. Fresh pending file: resume polling the existing UUID. Returns None if not yet
   activated, or the token dict when activated (also writes the token file and
   deletes the pending file).
3. Stale pending file (OTP expired): delete it, send a new login email, write a
   new pending file, restart polling. Returns None.

This module does not manage MQTT and does not import from any module above it in
the package hierarchy.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from zonneplan_prices.api import FetchError, ZonneplanClient
from zonneplan_prices.token import (
    delete_pending,
    is_pending_fresh,
    load_pending,
    save_pending,
    save_token,
)

logger = logging.getLogger(__name__)

# Interval between poll attempts within a single call window (seconds).
# Short enough to respond quickly when the user clicks the link,
# long enough not to hammer the Zonneplan API.
_POLL_INTERVAL_SECONDS = 2


def attempt_auth(
    *,
    client: ZonneplanClient,
    email: str,
    token_path: Path,
    pending_path: Path,
    poll_window_seconds: int = 30,
) -> dict | None:
    """Attempt one round of the Zonneplan email OTP auth flow.

    Called by ``_run_cycle`` when no valid token is present. Each call does at
    most ``poll_window_seconds`` of polling so the daemon thread is not blocked
    for long between trigger cycles.

    On the first call with no pending file: sends the login email and starts
    polling. On subsequent calls where a fresh pending file exists (including
    after a container restart mid-auth): resumes polling the existing UUID
    without sending a new email. When the pending file is stale (the OTP link
    has expired): deletes it and sends a new login email.

    Args:
        client: Authenticated (or unauthenticated) ZonneplanClient instance.
        email: Email address registered with the Zonneplan account. Used to
            send the login email when no pending state exists.
        token_path: Path to the token JSON file. Written on successful
            activation.
        pending_path: Path to the pending-auth JSON file. Written when the
            login email is sent; deleted on successful activation.
        poll_window_seconds: Maximum seconds to spend polling within this call.
            Defaults to 30. Use 0 in unit tests to skip the sleep loop.

    Returns:
        The OAuth token dict if the user activated during this call's polling
        window, or None if the user has not yet clicked the link or the
        window expired before activation.

    Raises:
        FetchError: On unrecoverable API failures (e.g. network down).
    """
    pending = load_pending(pending_path)

    if pending is not None and not is_pending_fresh(pending):
        # The OTP link in the previous email has expired. Delete the stale
        # pending state and trigger a new login email below.
        logger.warning(
            "Zonneplan activation link has expired. Sending a new login email..."
        )
        delete_pending(pending_path)
        pending = None

    if pending is None:
        # No pending state — send the login email and create a pending file.
        logger.warning(
            "No Zonneplan token found. Sending login email to %s...", email
        )
        uuid = client.request_login_email(email)
        save_pending(pending_path, uuid=uuid, email=email)
        logger.warning(
            "Login email sent. Check your inbox and click the activation link. "
            "Polling for activation (up to %ds this cycle)...",
            poll_window_seconds,
        )
        pending = load_pending(pending_path)

    uuid = pending["uuid"]  # type: ignore[index]
    pending_email = pending.get("email", email)  # type: ignore[assignment]

    # Poll within the window, stopping as soon as the user activates.
    deadline = time.monotonic() + poll_window_seconds
    while time.monotonic() < deadline:
        token = client.poll_activation(uuid, pending_email)
        if token is not None:
            save_token(token_path, token)
            delete_pending(pending_path)
            logger.info(
                "Zonneplan activation confirmed. Token saved to %s.", token_path
            )
            return token
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))

    logger.warning(
        "Still waiting for Zonneplan activation (polling). "
        "Click the link in your email. Will retry on the next trigger."
    )
    return None
