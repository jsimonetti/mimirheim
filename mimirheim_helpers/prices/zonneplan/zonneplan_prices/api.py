"""HTTP client for the Zonneplan API.

This module wraps the minimal set of Zonneplan API calls needed by the
prices helper. All calls are synchronous (using ``requests``). Authentication
state (the current access token) is held in the client instance and injected
as a Bearer token header on each authenticated request.

This module does not handle token persistence (that is token.py's job) and does
not manage the auth flow state machine (that is auth.py's job).

API base URL: https://app-api.zonneplan.nl/

Required request headers (sent on every call):

    Content-Type: application/json;charset=utf-8
    x-app-version: 5.10.1
    x-app-environment: production
"""
from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)

_BASE_URL = "https://app-api.zonneplan.nl"
_DEFAULT_HEADERS = {
    "Content-Type": "application/json;charset=utf-8",
    "x-app-version": "5.10.1",
    "x-app-environment": "production",
}


class FetchError(Exception):
    """Raised when a Zonneplan API call fails due to a network or HTTP error.

    The caller should log the error, retain the existing MQTT payload, and retry
    on the next trigger cycle. FetchError is recoverable.
    """


class AuthError(Exception):
    """Raised when Zonneplan authentication fails in an unrecoverable way.

    Examples include an expired or revoked refresh token, or an invalid email
    address. The daemon should log the error at ERROR level and wait for
    operator intervention (reconfiguration or re-authentication).
    """


class ZonneplanClient:
    """Synchronous HTTP client for the Zonneplan API.

    Args:
        access_token: Current OAuth access token, or None when not yet
            authenticated. Injected as ``Authorization: Bearer <token>`` on
            authenticated requests.
    """

    def __init__(self, access_token: str | None) -> None:
        self._access_token = access_token

    def set_access_token(self, token: str) -> None:
        """Update the access token used for authenticated requests.

        Args:
            token: The new access token string.
        """
        self._access_token = token

    def _auth_headers(self) -> dict[str, str]:
        """Return request headers including the current Bearer token.

        Returns:
            Headers dict with Authorization added to the default headers.
        """
        return {**_DEFAULT_HEADERS, "Authorization": f"Bearer {self._access_token}"}

    def request_login_email(self, email: str) -> str:
        """Send a Zonneplan login email to the given address.

        This is step 1 of the OTP auth flow. It causes Zonneplan to send the
        user an email containing an activation link. The user must click the
        link before the OTP can be exchanged for tokens.

        Args:
            email: The email address registered with the Zonneplan account.

        Returns:
            The auth-request UUID string from the response. Pass this UUID to
            :meth:`poll_activation` to check whether the user has clicked the
            link.

        Raises:
            FetchError: On any HTTP or network failure.
        """
        try:
            resp = requests.post(
                f"{_BASE_URL}/auth/request",
                json={"email": email},
                headers=_DEFAULT_HEADERS,
                timeout=15,
            )
            if resp.status_code >= 400:
                raise FetchError(
                    f"POST /auth/request returned HTTP {resp.status_code}"
                )
            return resp.json()["data"]["uuid"]
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"POST /auth/request failed: {exc}") from exc

    def poll_activation(self, uuid: str, email: str = "") -> dict | None:
        """Check whether the user has activated the login email.

        This is step 3 of the OTP auth flow. Call this repeatedly (with a
        short sleep between calls) after sending the login email. When the
        user clicks the activation link, Zonneplan marks the request as
        activated and provides a one-time password.

        Args:
            uuid: The auth-request UUID returned by :meth:`request_login_email`.
            email: The email address used in the original auth request. Some
                Zonneplan OTP exchanges require the same email to be supplied
                alongside the one-time password.

        Returns:
            The OAuth token dict (with ``access_token``, ``refresh_token``,
            ``token_type``, and ``expires_in``) if the user has activated, or
            None if the request is still pending.

        Raises:
            FetchError: On any HTTP or network failure.
        """
        try:
            resp = requests.get(
                f"{_BASE_URL}/auth/request/{uuid}",
                headers=_DEFAULT_HEADERS,
                timeout=15,
            )
            if resp.status_code >= 400:
                raise FetchError(
                    f"GET /auth/request/{uuid} returned HTTP {resp.status_code}"
                )
            data = resp.json()["data"]
            if not data.get("is_activated"):
                return None
            # Activated — exchange the one-time password for OAuth tokens.
            otp = data["password"]
            return self._exchange_otp(otp, email)
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"GET /auth/request/{uuid} failed: {exc}") from exc

    def _exchange_otp(self, otp: str, email: str = "") -> dict:
        """Exchange a one-time password for OAuth tokens.

        This is step 4 of the OTP auth flow, called automatically by
        :meth:`poll_activation` when the user has clicked the activation link.

        Args:
            otp: The one-time password from the activation response.
            email: The email address used in the original auth request. May be
                empty when the client has already sent the email and the OTP
                was obtained from a poll response.

        Returns:
            OAuth token dict with ``access_token``, ``refresh_token``,
            ``token_type``, and ``expires_in``.

        Raises:
            AuthError: If the OTP is invalid or has expired.
        """
        try:
            resp = requests.post(
                f"{_BASE_URL}/oauth/token",
                json={
                    "grant_type": "one_time_password",
                    "email": email,
                    "password": otp,
                },
                headers=_DEFAULT_HEADERS,
                timeout=15,
            )
            if resp.status_code >= 400:
                raise AuthError(
                    f"OTP exchange returned HTTP {resp.status_code}"
                )
            return resp.json()
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"OTP exchange failed: {exc}") from exc

    def refresh_token(self, refresh_token: str) -> dict:
        """Obtain a new access token using a refresh token.

        Called when the current access token has expired or is about to expire.
        The refresh token itself has a longer lifetime and typically remains
        valid across many token refreshes.

        Args:
            refresh_token: The refresh token stored in the token file.

        Returns:
            OAuth token dict with ``access_token``, ``refresh_token``,
            ``token_type``, and ``expires_in``.

        Raises:
            AuthError: If the refresh token is expired, revoked, or invalid.
                The caller must re-authenticate via the OTP flow.
        """
        try:
            resp = requests.post(
                f"{_BASE_URL}/oauth/token",
                json={"grant_type": "refresh_token", "refresh_token": refresh_token},
                headers=_DEFAULT_HEADERS,
                timeout=15,
            )
            if resp.status_code >= 400:
                raise AuthError(
                    f"Token refresh returned HTTP {resp.status_code} — "
                    "refresh token may be expired"
                )
            return resp.json()
        except AuthError:
            raise
        except Exception as exc:
            raise AuthError(f"Token refresh failed: {exc}") from exc

    def get_connection_uuid(self) -> str:
        """Discover the electricity connection UUID from the account.

        Called once when ``connection_uuid`` is not configured. The first
        electricity connection found across all address groups is used.

        Returns:
            The UUID string of the first electricity connection.

        Raises:
            FetchError: On HTTP or network failure, or if no electricity
                connection is found in the account.
        """
        try:
            resp = requests.get(
                f"{_BASE_URL}/user-accounts/me",
                headers=self._auth_headers(),
                timeout=15,
            )
            if resp.status_code >= 400:
                raise FetchError(
                    f"GET /user-accounts/me returned HTTP {resp.status_code}"
                )
            address_groups = resp.json()["data"]["address_groups"]
            for group in address_groups:
                for conn in group.get("connections", []):
                    if conn.get("market_segment") == "electricity":
                        return conn["uuid"]
            raise FetchError(
                "No electricity connection found in Zonneplan account. "
                "Check that the account has an active electricity contract."
            )
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(f"GET /user-accounts/me failed: {exc}") from exc

    def get_summary(self, connection_uuid: str) -> dict:
        """Fetch the price summary for a connection.

        Returns the ``data`` object from the summary response, which contains
        the ``price_per_hour`` list. The raw integer price fields in the list
        must be multiplied by 0.0000001 to convert to EUR/kWh.

        Args:
            connection_uuid: The electricity connection UUID to fetch prices for.

        Returns:
            The ``data`` dict from the summary response.

        Raises:
            FetchError: On HTTP or network failure.
        """
        try:
            resp = requests.get(
                f"{_BASE_URL}/connections/{connection_uuid}/summary",
                headers=self._auth_headers(),
                timeout=15,
            )
            if resp.status_code >= 400:
                raise FetchError(
                    f"GET /connections/{connection_uuid}/summary "
                    f"returned HTTP {resp.status_code}"
                )
            return resp.json()["data"]
        except FetchError:
            raise
        except Exception as exc:
            raise FetchError(
                f"GET /connections/{connection_uuid}/summary failed: {exc}"
            ) from exc
