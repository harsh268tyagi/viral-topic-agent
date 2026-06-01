"""Example/unit tests for ``get_audience_activity`` error branches (task 6.3).

Covers the two documented audience-activity failure branches of
:meth:`~infrastructure.youtube_data_source.YouTubeDataSource.get_audience_activity`
(implemented by task 6.1) against injected fakes with **no real network access**
(Requirements 16.3, 16.4):

- **3.3 — missing OAuth.** When no OAuth credentials authorizing the YouTube
  Analytics API for the owned channel are configured, ``get_audience_activity``
  raises a :class:`~infrastructure.datasource.NonTransientError` whose reason
  indicates audience activity requires YouTube Analytics API authorization for
  the owned channel, and returns no ``AudienceActivity`` value.
- **3.4 — Analytics auth/permission error.** When the Analytics API responds
  with an authorization or permission error, ``get_audience_activity`` raises a
  :class:`NonTransientError` whose reason identifies the owned channel, and
  returns no ``AudienceActivity`` value.

The data source is wired with a :class:`~tests.edge_fakes.FakeHttpTransport`, a
real :class:`~infrastructure.auth_manager.AuthManager` over that transport, and
a :class:`~infrastructure.clock.FakeClock`, mirroring the construction used by
``tests/test_youtube_datasource_conformance.py`` and the authentication
scenarios in ``tests/test_auth_manager.py``.
"""

from __future__ import annotations

import json

import pytest

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings, OAuthCredentials
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.datasource import NonTransientError
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

_OWNED_CHANNEL_ID = "UC_owned_channel_123"
_DAYS = 7

# Secret values seeded into the auth settings; the error-branch assertions also
# confirm these never leak into a raised reason (defence-in-depth for 2.9/13.6).
_API_KEY_VALUE = "data-api-key-AAA"
_CLIENT_SECRET_VALUE = "client-secret-CCC"
_REFRESH_TOKEN_VALUE = "refresh-token-RRR"
_ACCESS_TOKEN_VALUE = "access-token-TTT"


# ---------------------------------------------------------------------------
# Builders (mirroring tests/test_auth_manager.py conventions)
# ---------------------------------------------------------------------------


def _secret(value: str, name: str) -> Secret:
    return Secret(value, CredentialReference(name))


def _oauth() -> OAuthCredentials:
    """OAuth credentials with a held access token (no refresh needed)."""
    return OAuthCredentials(
        client_id="client-id-public",
        client_secret=_secret(_CLIENT_SECRET_VALUE, "oauth_client_secret"),
        refresh_token=_secret(_REFRESH_TOKEN_VALUE, "oauth_refresh_token"),
        access_token=_secret(_ACCESS_TOKEN_VALUE, "oauth_access_token"),
    )


def _auth_settings(*, oauth: OAuthCredentials | None) -> AuthSettings:
    return AuthSettings(
        youtube_api_key=_secret(_API_KEY_VALUE, "youtube_api_key"),
        oauth=oauth,
    )


def _data_source(
    settings: AuthSettings, transport: FakeHttpTransport
) -> YouTubeDataSource:
    clock = FakeClock()
    auth = AuthManager(settings, transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url="https://youtube.example.com/v3",
        request_timeout_seconds=30.0,
    )


def _permission_denied_body() -> bytes:
    """A 403 Analytics body reporting a genuine permission failure.

    The reason markers (``forbidden`` / ``PERMISSION_DENIED``) are not quota or
    expired-token markers, so the response classifies as an authorization /
    permission failure rather than a rate limit or a token-refresh trigger.
    """
    return json.dumps(
        {
            "error": {
                "code": 403,
                "status": "PERMISSION_DENIED",
                "errors": [{"reason": "forbidden", "message": "Access denied."}],
            }
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 3.3 — missing OAuth raises NonTransientError and returns no activity
# ---------------------------------------------------------------------------


def test_missing_oauth_raises_nontransient_and_returns_no_activity():
    """3.3: no OAuth configured -> NonTransientError, no AudienceActivity."""
    transport = FakeHttpTransport()  # must never be called when OAuth is absent
    source = _data_source(_auth_settings(oauth=None), transport)

    with pytest.raises(NonTransientError) as excinfo:
        source.get_audience_activity(_OWNED_CHANNEL_ID, _DAYS)

    reason = excinfo.value.reason
    # The reason indicates audience activity requires Analytics authorization
    # for the owned channel (named).
    assert _OWNED_CHANNEL_ID in reason
    assert "analytics" in reason.lower()
    assert "authoriz" in reason.lower()  # covers "authorization"/"re-authorization"
    # No external request is issued when OAuth is missing (graceful, no activity).
    assert transport.call_count == 0
    # No secret value leaks into the reason (2.9 / 13.6 defence-in-depth).
    for secret_value in (_API_KEY_VALUE, _CLIENT_SECRET_VALUE):
        assert secret_value not in reason


# ---------------------------------------------------------------------------
# 3.4 — Analytics auth/permission error raises NonTransientError naming the
#       channel and returns no activity
# ---------------------------------------------------------------------------


def test_analytics_permission_error_raises_nontransient_naming_channel():
    """3.4: an Analytics auth/permission error -> NonTransientError naming the channel."""
    transport = FakeHttpTransport()
    transport.queue_response(
        HttpResponse(
            status=403,
            headers={"Content-Type": "application/json"},
            body=_permission_denied_body(),
        )
    )
    source = _data_source(_auth_settings(oauth=_oauth()), transport)

    with pytest.raises(NonTransientError) as excinfo:
        source.get_audience_activity(_OWNED_CHANNEL_ID, _DAYS)

    reason = excinfo.value.reason
    # The reason identifies the owned channel (3.4).
    assert _OWNED_CHANNEL_ID in reason
    # No secret value (API key, OAuth client secret, tokens) leaks (2.9 / 13.6).
    for secret_value in (
        _API_KEY_VALUE,
        _CLIENT_SECRET_VALUE,
        _REFRESH_TOKEN_VALUE,
        _ACCESS_TOKEN_VALUE,
    ):
        assert secret_value not in reason
