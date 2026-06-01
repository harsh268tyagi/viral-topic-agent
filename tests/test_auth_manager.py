"""Example/unit tests for ``AuthManager`` authentication scenarios (task 4.5).

Covers Requirement 13's authentication and authorization behavior:

- API-key authentication for public YouTube Data API requests (13.1).
- OAuth bearer authentication for owned-channel Analytics requests (13.2).
- The expired-access-token -> refresh -> reissue-once flow (13.3): a refresh is
  performed through the injected transport and the new bearer is applied so the
  caller can reissue exactly once.
- Refresh failure surfacing a re-authorization ``NonTransientError`` (13.4).
- An invalid Data API key surfacing a ``NonTransientError`` (13.5).
- Expiry with no refresh token surfacing a re-authorization
  ``NonTransientError`` (13.7).

A spy :class:`FakeHttpTransport` confirms credential *selection*: header/param
selection performs no network request, while the OAuth refresh round-trip is
driven by scripted transport responses with no real network access (16.3).

The OAuth refresh round-trip is performed through the optional ``youtube``
extra's client library, imported lazily inside ``AuthManager``. When that extra
is not installed the refresh-success/refresh-failure tests skip with a clear
reason; every non-OAuth-library scenario (selection, invalid key,
expired-without-refresh, missing OAuth) always runs.
"""

from __future__ import annotations

import importlib.util
import json

import pytest

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings, OAuthCredentials
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.datasource import NonTransientError
from infrastructure.http_transport import HttpResponse
from tests.edge_fakes import FakeHttpTransport

# The OAuth client library is an optional extra (Requirement 15.2). Tests that
# exercise the genuine refresh round-trip require it; skip them clearly when it
# is absent rather than failing.
_OAUTH_LIB_AVAILABLE = (
    importlib.util.find_spec("google.oauth2.credentials") is not None
    and importlib.util.find_spec("google.auth.exceptions") is not None
)
_requires_oauth_lib = pytest.mark.skipif(
    not _OAUTH_LIB_AVAILABLE,
    reason="optional 'youtube' extra (google-auth) not installed; "
    "OAuth refresh round-trip cannot be exercised",
)

_CHANNEL_ID = "UC_owned_channel_123"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _secret(value: str, name: str) -> Secret:
    return Secret(value, CredentialReference(name))


def _api_key(value: str = "data-api-key-AAA") -> Secret:
    return _secret(value, "youtube_api_key")


def _oauth(
    *,
    refresh_token: str | None = "refresh-token-RRR",
    access_token: str | None = "access-token-TTT",
) -> OAuthCredentials:
    return OAuthCredentials(
        client_id="client-id-public",
        client_secret=_secret("client-secret-CCC", "oauth_client_secret"),
        refresh_token=(
            _secret(refresh_token, "oauth_refresh_token")
            if refresh_token is not None
            else None
        ),
        access_token=(
            _secret(access_token, "oauth_access_token")
            if access_token is not None
            else None
        ),
    )


def _auth_settings(
    *, api_key: Secret | None = None, oauth: OAuthCredentials | None = None
) -> AuthSettings:
    return AuthSettings(youtube_api_key=api_key or _api_key(), oauth=oauth)


def _manager(
    settings: AuthSettings, transport: FakeHttpTransport | None = None
) -> AuthManager:
    return AuthManager(settings, transport or FakeHttpTransport(), FakeClock())


def _token_success_body(access_token: str) -> bytes:
    return json.dumps(
        {
            "access_token": access_token,
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/yt-analytics.readonly",
        }
    ).encode("utf-8")


def _token_error_body() -> bytes:
    return json.dumps(
        {
            "error": "invalid_grant",
            "error_description": "Token has been expired or revoked.",
        }
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# 13.1 / 13.2 — credential selection (spy transport confirms no network)
# ---------------------------------------------------------------------------


def test_data_api_request_authenticates_with_api_key():
    """13.1: Data API requests authenticate with the configured API key."""
    transport = FakeHttpTransport()  # no scripted outcomes -> any request fails
    manager = _manager(_auth_settings(api_key=_api_key("key-XYZ")), transport)

    params = manager.data_api_params()

    assert params == {"key": "key-XYZ"}
    # Selecting the API key must not perform any network request.
    assert transport.call_count == 0


def test_analytics_request_authenticates_with_oauth_bearer():
    """13.2: Analytics requests authenticate with the OAuth bearer token."""
    transport = FakeHttpTransport()
    manager = _manager(
        _auth_settings(oauth=_oauth(access_token="bearer-123")), transport
    )

    header = manager.analytics_auth_header(_CHANNEL_ID)

    assert header == {"Authorization": "Bearer bearer-123"}
    # A held access token needs no refresh, so no network request is made.
    assert transport.call_count == 0


def test_api_key_and_oauth_selection_differ():
    """13.1 vs 13.2: the two request types select different credentials."""
    manager = _manager(
        _auth_settings(api_key=_api_key("data-key"), oauth=_oauth(access_token="tok"))
    )

    params = manager.data_api_params()
    header = manager.analytics_auth_header(_CHANNEL_ID)

    assert "key" in params and "Authorization" not in params
    assert "Authorization" in header and "key" not in header
    assert params["key"] != header["Authorization"]


# ---------------------------------------------------------------------------
# Expiry detection feeding the 13.3 refresh trigger
# ---------------------------------------------------------------------------


def test_401_response_detected_as_expired_token():
    manager = _manager(_auth_settings(oauth=_oauth()))
    assert manager.is_access_token_expired(HttpResponse(status=401)) is True


def test_403_with_invalid_token_marker_detected_as_expired():
    manager = _manager(_auth_settings(oauth=_oauth()))
    response = HttpResponse(
        status=403,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )
    assert manager.is_access_token_expired(response) is True


def test_403_plain_permission_failure_not_treated_as_expired():
    manager = _manager(_auth_settings(oauth=_oauth()))
    response = HttpResponse(
        status=403,
        body=b'{"error": {"errors": [{"reason": "forbidden"}]}}',
    )
    assert manager.is_access_token_expired(response) is False


def test_successful_response_not_treated_as_expired():
    manager = _manager(_auth_settings(oauth=_oauth()))
    assert manager.is_access_token_expired(HttpResponse(status=200)) is False


# ---------------------------------------------------------------------------
# 13.3 — expired -> refresh -> reissue exactly once
# ---------------------------------------------------------------------------


@_requires_oauth_lib
def test_refresh_obtains_new_token_with_exactly_one_request():
    """13.3: a refresh obtains a new access token via exactly one round-trip."""
    transport = FakeHttpTransport()
    transport.queue_response(
        HttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=_token_success_body("new-access-token-789"),
        )
    )
    manager = _manager(
        _auth_settings(oauth=_oauth(access_token="stale-token")), transport
    )

    manager.refresh_access_token(_CHANNEL_ID)

    # Exactly one refresh round-trip occurred (the caller reissues once).
    assert transport.call_count == 1
    assert transport.last_request.method.upper() == "POST"
    # The new bearer is now applied to subsequent Analytics requests; obtaining
    # the header performs no further network request (transport is exhausted).
    assert manager.analytics_auth_header(_CHANNEL_ID) == {
        "Authorization": "Bearer new-access-token-789"
    }
    assert transport.call_count == 1


@_requires_oauth_lib
def test_analytics_header_refreshes_when_no_access_token_held():
    """13.3: with no held token but a refresh token, the header triggers refresh."""
    transport = FakeHttpTransport()
    transport.queue_response(
        HttpResponse(
            status=200,
            headers={"Content-Type": "application/json"},
            body=_token_success_body("fresh-token-001"),
        )
    )
    manager = _manager(
        _auth_settings(oauth=_oauth(access_token=None)), transport
    )

    header = manager.analytics_auth_header(_CHANNEL_ID)

    assert header == {"Authorization": "Bearer fresh-token-001"}
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# 13.4 — refresh failure raises a re-authorization NonTransientError
# ---------------------------------------------------------------------------


@_requires_oauth_lib
def test_refresh_failure_raises_reauthorization_error():
    """13.4: a rejected refresh raises NonTransientError naming the channel."""
    transport = FakeHttpTransport()
    transport.queue_response(
        HttpResponse(
            status=400,
            headers={"Content-Type": "application/json"},
            body=_token_error_body(),
        )
    )
    manager = _manager(
        _auth_settings(oauth=_oauth(access_token="stale-token")), transport
    )

    with pytest.raises(NonTransientError) as excinfo:
        manager.refresh_access_token(_CHANNEL_ID)

    reason = excinfo.value.reason
    assert _CHANNEL_ID in reason
    assert "re-authorization" in reason.lower()
    # 13.6: the failure reason exposes no secret value.
    for secret_value in (
        "refresh-token-RRR",
        "client-secret-CCC",
        "stale-token",
        "data-api-key-AAA",
    ):
        assert secret_value not in reason


# ---------------------------------------------------------------------------
# 13.5 — invalid Data API key
# ---------------------------------------------------------------------------


def test_invalid_api_key_error_is_nontransient_and_redacted():
    """13.5: an invalid Data API key surfaces a NonTransientError."""
    manager = _manager(_auth_settings(api_key=_api_key("secret-key-value")))

    error = manager.invalid_api_key_error()

    assert isinstance(error, NonTransientError)
    assert "invalid" in error.reason.lower()
    assert "secret-key-value" not in error.reason


# ---------------------------------------------------------------------------
# 13.7 — expired/absent token with no refresh token
# ---------------------------------------------------------------------------


def test_refresh_without_refresh_token_raises_reauthorization_error():
    """13.7: expiry with no refresh token raises a re-authorization error."""
    transport = FakeHttpTransport()  # must never be called
    manager = _manager(
        _auth_settings(oauth=_oauth(refresh_token=None, access_token="stale")),
        transport,
    )

    with pytest.raises(NonTransientError) as excinfo:
        manager.refresh_access_token(_CHANNEL_ID)

    reason = excinfo.value.reason
    assert _CHANNEL_ID in reason
    assert "re-authorization" in reason.lower()
    # No refresh round-trip is attempted when there is no refresh token.
    assert transport.call_count == 0


def test_analytics_header_without_token_or_refresh_token_raises_reauthorization():
    """13.7: requesting a bearer with neither a held nor refreshable token."""
    transport = FakeHttpTransport()
    manager = _manager(
        _auth_settings(oauth=_oauth(refresh_token=None, access_token=None)),
        transport,
    )

    with pytest.raises(NonTransientError) as excinfo:
        manager.analytics_auth_header(_CHANNEL_ID)

    assert _CHANNEL_ID in excinfo.value.reason
    assert transport.call_count == 0


def test_analytics_request_without_oauth_configured_raises_reauthorization():
    """13.2/13.7: Analytics access with no OAuth configured requires re-auth."""
    transport = FakeHttpTransport()
    manager = _manager(_auth_settings(oauth=None), transport)

    with pytest.raises(NonTransientError) as excinfo:
        manager.analytics_auth_header(_CHANNEL_ID)

    assert _CHANNEL_ID in excinfo.value.reason
    assert transport.call_count == 0
