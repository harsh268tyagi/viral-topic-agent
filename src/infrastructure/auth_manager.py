"""Authentication and authorization for YouTube data/analytics requests.

This module implements the ``AuthManager`` the design
(``.kiro/specs/real-provider-integration/design.md`` -> *AuthManager*) calls
for: it selects the right credential per request and manages OAuth access-token
refresh, satisfying Requirement 13 (and the dependency-policy clause 15.2).

Responsibilities (Requirement 13):

- Public YouTube Data API requests authenticate with the configured API key
  (13.1): :meth:`AuthManager.data_api_params`.
- Owned-channel YouTube Analytics API requests authenticate with the owned
  channel's OAuth credentials (13.2): :meth:`AuthManager.analytics_auth_header`.
- On an Analytics response indicating the access token is expired *and* a
  refresh token is available, a new access token is obtained and the caller is
  left to reissue the failed request exactly once (13.3):
  :meth:`AuthManager.is_access_token_expired` + :meth:`AuthManager.refresh_access_token`.
- Refresh failure (13.4) or token expiry with no refresh token (13.7) raises a
  :class:`~infrastructure.datasource.NonTransientError` that names the owned
  channel and indicates re-authorization is required.
- An invalid Data API key surfaces as a
  :class:`~infrastructure.datasource.NonTransientError` (13.5):
  :meth:`AuthManager.invalid_api_key_error`.

Secret hygiene and dependency policy:

- Every error reason is built through :func:`~config.secrets.redact` so no
  :class:`~config.secrets.Secret` value can leak (13.6); the module logger only
  ever records the non-secret channel identifier, never a token value.
- The OAuth client library is imported **lazily inside** :meth:`refresh_access_token`
  and nowhere else, so the dependency-free core and its tests import nothing
  third-party when the optional ``youtube`` extra is absent (15.2).

The OAuth refresh is performed *through the injected* :class:`HttpTransport`
(via a small adapter to the OAuth library's request interface) so the refresh
branch is fully exercisable with a scripted fake transport and no real network
access (16.3).
"""

from __future__ import annotations

import logging
from typing import Mapping

from config.secrets import CredentialReference, Secret, redact
from config.settings import AuthSettings, OAuthCredentials
from infrastructure.clock import Clock
from infrastructure.datasource import NonTransientError
from infrastructure.http_transport import HttpResponse, HttpTransport

__all__ = ["AuthManager"]

logger = logging.getLogger(__name__)

# The OAuth 2.0 token endpoint used to refresh an access token. It is a
# constructor default so tests can point the (scripted) transport anywhere; the
# fake transport ignores the URL, and production uses Google's real endpoint.
_GOOGLE_OAUTH2_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Markers (lower-cased) in a 403 response that indicate an expired/invalid
# access token rather than a genuine permission failure.
_EXPIRED_TOKEN_MARKERS = (
    "invalid_token",
    "invalid credentials",
    "invalid authentication credentials",
    "access token",
    "expired",
)


class AuthManager:
    """Selects and applies the right credential and refreshes OAuth tokens.

    Constructor-injected with the :class:`AuthSettings`, an
    :class:`HttpTransport` (used for the OAuth refresh round-trip so the branch
    stays testable, 16.3), and a :class:`Clock`. The current access token is
    held as mutable state so that a successful refresh updates the bearer used
    by subsequent :meth:`analytics_auth_header` calls; the immutable
    :class:`AuthSettings`/:class:`OAuthCredentials` are never mutated.
    """

    def __init__(
        self,
        settings: AuthSettings,
        transport: HttpTransport,
        clock: Clock,
        *,
        token_uri: str = _GOOGLE_OAUTH2_TOKEN_URI,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._clock = clock
        self._token_uri = token_uri
        # The access token may be refreshed at runtime; track it separately from
        # the frozen OAuthCredentials it was seeded from.
        oauth = settings.oauth
        self._access_token: Secret | None = oauth.access_token if oauth else None

    # ------------------------------------------------------------------
    # Data API authentication (13.1, 13.5)
    # ------------------------------------------------------------------

    @property
    def analytics_authorized(self) -> bool:
        """Whether OAuth credentials for the Analytics API are configured (3.3).

        Lets a caller (the data source's ``get_audience_activity``) distinguish
        the "no OAuth configured at all" case — which Requirement 3.3 maps to a
        ``NonTransientError`` indicating that audience activity *requires*
        Analytics authorization — from the re-authorization cases (13.4, 13.7)
        that arise once OAuth is present but unusable.
        """
        return self._settings.oauth is not None

    def data_api_params(self) -> Mapping[str, str]:
        """Return the query parameters that authenticate a Data API request.

        Public YouTube Data API requests authenticate with the configured API
        key (13.1). This is one of the two boundary points where a secret value
        is deliberately revealed, because the value must actually be sent.
        """
        return {"key": self._settings.youtube_api_key.reveal()}

    def invalid_api_key_error(self) -> NonTransientError:
        """Build the error for a Data API key rejected as invalid (13.5).

        Returned (not raised) so the caller decides where to raise it; the
        reason names the problem without exposing the key value, and is built
        through :func:`redact` for defence in depth (13.6).
        """
        return NonTransientError(
            redact("YouTube Data API key is invalid", self._all_secrets())
        )

    # ------------------------------------------------------------------
    # Analytics API authentication (13.2, 13.3, 13.7)
    # ------------------------------------------------------------------

    def analytics_auth_header(self, channel_id: str) -> Mapping[str, str]:
        """Return the OAuth bearer header authenticating an Analytics request.

        Owned-channel Analytics requests authenticate with the owned channel's
        OAuth credentials (13.2). When no usable access token is held yet, a
        refresh is attempted if a refresh token is available; otherwise (or on
        refresh failure) a :class:`NonTransientError` naming the channel and
        indicating re-authorization is raised (13.4, 13.7).
        """
        self._require_oauth(channel_id)
        if self._access_token is None:
            # No usable access token; obtain one if we can, else require
            # re-authorization. refresh_access_token raises when no refresh
            # token is available (13.7) or the refresh fails (13.4).
            self.refresh_access_token(channel_id)
        assert self._access_token is not None  # refresh_access_token guarantees it
        return {"Authorization": f"Bearer {self._access_token.reveal()}"}

    def is_access_token_expired(self, response: HttpResponse) -> bool:
        """Return whether an Analytics response indicates an expired token.

        A ``401`` response always indicates an expired/invalid access token. A
        ``403`` is treated as an expired token only when the response carries a
        marker distinguishing it from a genuine permission failure (e.g. an
        ``invalid_token`` ``WWW-Authenticate`` challenge); otherwise the caller
        classifies it as an authorization failure per Requirement 2.
        """
        if response.status == 401:
            return True
        if response.status != 403:
            return False
        challenge = response.headers.get("WWW-Authenticate", "") or ""
        body_text = (
            response.body.decode("utf-8", "replace") if response.body else ""
        )
        haystack = f"{challenge} {body_text}".lower()
        return any(marker in haystack for marker in _EXPIRED_TOKEN_MARKERS)

    def refresh_access_token(self, channel_id: str) -> None:
        """Obtain a new access token from the refresh token (13.3).

        On success the held access token is replaced so the next
        :meth:`analytics_auth_header` issues the new bearer and the caller can
        reissue the failed request exactly once. When no refresh token is
        available (13.7) or the refresh fails (13.4), a
        :class:`NonTransientError` naming the owned channel and indicating
        re-authorization is raised. No secret value is logged (13.6).
        """
        oauth = self._require_oauth(channel_id)
        if oauth.refresh_token is None:
            # Expired/absent access token with no way to refresh (13.7).
            raise self._reauthorization_error(channel_id)

        # Lazy, edge-confined import of the OAuth client library (15.2). Imported
        # here and nowhere else so the core imports nothing third-party.
        try:
            from google.auth.exceptions import RefreshError
            from google.oauth2.credentials import Credentials
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise NonTransientError(
                redact(
                    f"refreshing OAuth credentials for owned channel {channel_id} "
                    "requires the optional 'youtube' dependency extra to be installed",
                    self._all_secrets(),
                )
            ) from exc

        credentials = Credentials(
            token=self._access_token.reveal() if self._access_token else None,
            refresh_token=oauth.refresh_token.reveal(),
            token_uri=self._token_uri,
            client_id=oauth.client_id,
            client_secret=oauth.client_secret.reveal(),
        )

        try:
            credentials.refresh(_OAuthTransportRequest(self._transport))
        except RefreshError as exc:
            # Refresh was attempted but rejected by the token endpoint (13.4).
            logger.warning(
                "OAuth access-token refresh failed for owned channel %s", channel_id
            )
            raise self._reauthorization_error(channel_id) from exc

        new_token = credentials.token
        if not new_token:
            # A "successful" refresh that yielded no usable token is still a
            # re-authorization condition (13.4).
            raise self._reauthorization_error(channel_id)

        self._access_token = Secret(
            new_token, CredentialReference("oauth_access_token")
        )
        # Log the event without any secret value (13.6); the channel id is a
        # non-secret identifier.
        logger.info("Refreshed OAuth access token for owned channel %s", channel_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _require_oauth(self, channel_id: str) -> OAuthCredentials:
        """Return the configured OAuth credentials or raise re-auth (13.2)."""
        oauth = self._settings.oauth
        if oauth is None:
            raise self._reauthorization_error(channel_id)
        return oauth

    def _reauthorization_error(self, channel_id: str) -> NonTransientError:
        """Build the channel-naming re-authorization error (13.4, 13.7)."""
        return NonTransientError(
            redact(
                f"OAuth credentials for owned channel {channel_id} require "
                "re-authorization for YouTube Analytics API access",
                self._all_secrets(),
            )
        )

    def _all_secrets(self) -> list[Secret]:
        """Collect every known secret so reasons can be scrubbed (13.6)."""
        secrets: list[Secret] = [self._settings.youtube_api_key]
        oauth = self._settings.oauth
        if oauth is not None:
            secrets.append(oauth.client_secret)
            if oauth.refresh_token is not None:
                secrets.append(oauth.refresh_token)
            if oauth.access_token is not None:
                secrets.append(oauth.access_token)
        if self._access_token is not None:
            secrets.append(self._access_token)
        return secrets


# ---------------------------------------------------------------------------
# Adapter: drive the OAuth library's refresh over the injected HttpTransport
# ---------------------------------------------------------------------------


class _OAuthTransportRequest:
    """Adapts an :class:`HttpTransport` to the OAuth library request callable.

    The OAuth client library performs the refresh round-trip through a callable
    matching ``google.auth.transport.Request`` (invoked as
    ``request(method=..., url=..., headers=..., body=...)``) and reads
    ``status``/``headers``/``data`` off the returned response. Routing that
    through the injected transport keeps the refresh branch testable with a
    scripted fake and no real network access (16.3).
    """

    __slots__ = ("_transport",)

    def __init__(self, transport: HttpTransport) -> None:
        self._transport = transport

    def __call__(
        self,
        url: str,
        method: str = "GET",
        body: bytes | str | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        **_kwargs: object,
    ) -> "_OAuthTransportResponse":
        payload: bytes | None
        if isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
        response = self._transport.request(
            method,
            url,
            headers=dict(headers or {}),
            body=payload,
            timeout_seconds=timeout,
        )
        return _OAuthTransportResponse(response)


class _OAuthTransportResponse:
    """Presents an :class:`HttpResponse` via the OAuth library response shape."""

    __slots__ = ("_response",)

    def __init__(self, response: HttpResponse) -> None:
        self._response = response

    @property
    def status(self) -> int:
        return self._response.status

    @property
    def headers(self) -> Mapping[str, str]:
        return self._response.headers

    @property
    def data(self) -> bytes:
        return self._response.body
