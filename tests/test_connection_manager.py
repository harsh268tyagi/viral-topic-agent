"""Example / branch tests for the Connection_Manager (task 5.4).

The universal Properties 3 and 4 live in
``test_connection_manager_tagging_properties.py`` and
``test_connection_manager_retry_properties.py``. This module covers the concrete
authorization-decision branches of
:class:`~viral_topic_agent.connection_manager.ConnectionManager`:

- a timely grant stores the credentials and marks the channel ``connected`` (1.2);
- a denial records ``authorization-failed`` and never retrieves (1.3);
- expired stored credentials yield ``authorization-expired`` identifying the
  channel, with no retrieval attempt (1.5);
- a storage failure records ``credential-storage-failed`` and leaves the channel
  not connected (1.8);
- a decision arriving after 300 s records ``authorization-timeout`` (1.7).

Tests drive the genuine :class:`ResilientDataSource` over simple stubs with a
:class:`FakeClock`, so any retrieval path is exercised without mocking the
resilience layer.

Requirements exercised: 1.2, 1.3, 1.5, 1.7, 1.8.
"""

from __future__ import annotations

from viral_topic_agent.infrastructure.clock import FakeClock
from viral_topic_agent.connection.connection_manager import (
    ConnectionManager,
    InMemoryCredentialStore,
)
from viral_topic_agent.infrastructure.datasource import DataOperation, DataRequest
from viral_topic_agent.domain.models import (
    AuthorizationGrant,
    AuthorizedChannel,
    AuthStatus,
    Configuration,
)
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_config() -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(),
    )


class _NeverCalledSource:
    """A :class:`DataSource` that fails the test if any method is called.

    Used to prove the "SHALL NOT retrieve" guarantees: any retrieval attempt
    would raise here, so a passing test confirms no retrieval happened.
    """

    def _boom(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("data source must not be called for this branch")

    get_channel_metadata = _boom
    get_videos = _boom
    get_audience_activity = _boom
    get_keyword_metrics = _boom
    get_template_performance = _boom


class _OkVideosSource:
    """A :class:`DataSource` whose ``get_videos`` returns a fixed payload."""

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):
        return [f"videos::{channel_id}"]

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover - unused
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _videos_request(channel_id: str) -> DataRequest:
    return DataRequest(
        operation=DataOperation.VIDEOS,
        target=channel_id,
        params={"channel_id": channel_id},
    )


# ---------------------------------------------------------------------------
# 1.2: grant stores credentials and connects
# ---------------------------------------------------------------------------


def test_grant_stores_credentials_and_marks_connected():
    """A timely grant connects the channel and persists its credentials (1.2)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)

    grant = AuthorizationGrant(
        granted=True, credentials_ref="cred-abc", responded_within_seconds=12.0
    )
    result = manager.complete_authorization("chan-1", grant, _empty_config())

    assert result.status is AuthStatus.CONNECTED
    assert result.channel_id == "chan-1"
    assert result.error is None

    stored = store.get("chan-1")
    assert stored is not None
    assert stored.connected is True
    assert stored.credentials_ref == "cred-abc"
    assert stored.credentials_expired is False


def test_connected_channel_retrieval_is_tagged_with_channel_id():
    """A connected channel retrieves data tagged with its own id (1.2 -> 1.6 path)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)
    clock = FakeClock()

    grant = AuthorizationGrant(
        granted=True, credentials_ref="cred-xyz", responded_within_seconds=2.0
    )
    manager.complete_authorization("chan-2", grant, _empty_config())
    channel = store.get("chan-2")
    assert channel is not None

    source = ResilientDataSource(_OkVideosSource(), RetryPolicy(), clock)
    retrieval = manager.retrieve_with_credentials(
        channel, _videos_request("chan-2"), source, clock
    )

    assert retrieval.is_ok()
    tagged = retrieval.unwrap()
    assert tagged.channel_id == "chan-2"
    assert tagged.data == ["videos::chan-2"]


# ---------------------------------------------------------------------------
# 1.3: denial -> authorization-failed, no retrieval
# ---------------------------------------------------------------------------


def test_denial_records_authorization_failed_and_does_not_connect():
    """A denial records authorization-failed and stores nothing (1.3)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)

    grant = AuthorizationGrant(
        granted=False, credentials_ref=None, responded_within_seconds=30.0
    )
    result = manager.complete_authorization("chan-deny", grant, _empty_config())

    assert result.status is AuthStatus.AUTHORIZATION_FAILED
    assert result.channel_id == "chan-deny"
    assert "authorization-failed" in (result.error or "")
    # Nothing was connected/stored, so no retrieval is possible (1.3).
    assert store.get("chan-deny") is None


def test_denied_channel_is_never_retrieved():
    """A denied (never-connected) channel does not hit the data source (1.3)."""
    manager = ConnectionManager(InMemoryCredentialStore())
    clock = FakeClock()
    # A channel object that was never connected (mirrors a denied authorization).
    channel = AuthorizedChannel(
        channel_id="chan-deny",
        credentials_ref="",
        connected=False,
        credentials_expired=False,
    )
    source = ResilientDataSource(_NeverCalledSource(), RetryPolicy(), clock)

    result = manager.retrieve_with_credentials(
        channel, _videos_request("chan-deny"), source, clock
    )

    # Refused without ever calling the (exploding) data source.
    assert result.is_err()
    assert result.unwrap_err().channel_id == "chan-deny"


# ---------------------------------------------------------------------------
# 1.5: expired credentials -> authorization-expired identifying the channel
# ---------------------------------------------------------------------------


def test_expired_credentials_return_authorization_expired_without_retrieval():
    """Expired credentials yield authorization-expired and skip retrieval (1.5)."""
    manager = ConnectionManager(InMemoryCredentialStore())
    clock = FakeClock()
    channel = AuthorizedChannel(
        channel_id="chan-exp",
        credentials_ref="cred-old",
        connected=True,
        credentials_expired=True,
    )
    source = ResilientDataSource(_NeverCalledSource(), RetryPolicy(), clock)

    result = manager.retrieve_with_credentials(
        channel, _videos_request("chan-exp"), source, clock
    )

    assert result.is_err()
    error = result.unwrap_err()
    assert error.status is AuthStatus.AUTHORIZATION_EXPIRED
    assert error.channel_id == "chan-exp"
    assert "authorization-expired" in error.reason
    assert "chan-exp" in error.reason


# ---------------------------------------------------------------------------
# 1.8: storage failure -> credential-storage-failed, not connected
# ---------------------------------------------------------------------------


def test_storage_failure_records_credential_storage_failed_and_not_connected():
    """A storage failure records credential-storage-failed and connects nothing (1.8)."""
    store = InMemoryCredentialStore(fail_all=True)
    manager = ConnectionManager(store)

    grant = AuthorizationGrant(
        granted=True, credentials_ref="cred-fail", responded_within_seconds=5.0
    )
    result = manager.complete_authorization("chan-store-fail", grant, _empty_config())

    assert result.status is AuthStatus.CREDENTIAL_STORAGE_FAILED
    assert result.channel_id == "chan-store-fail"
    assert "credential-storage-failed" in (result.error or "")
    assert "not saved" in (result.error or "")
    # The channel was not persisted and is therefore not connected (1.8).
    assert store.get("chan-store-fail") is None


def test_storage_failure_for_specific_channel_only():
    """Only the channel whose store fails is left unconnected (1.8)."""
    store = InMemoryCredentialStore(fail_channel_ids={"chan-bad"})
    manager = ConnectionManager(store)

    ok = manager.complete_authorization(
        "chan-good",
        AuthorizationGrant(granted=True, credentials_ref="c1", responded_within_seconds=1.0),
        _empty_config(),
    )
    bad = manager.complete_authorization(
        "chan-bad",
        AuthorizationGrant(granted=True, credentials_ref="c2", responded_within_seconds=1.0),
        _empty_config(),
    )

    assert ok.status is AuthStatus.CONNECTED
    assert store.get("chan-good") is not None
    assert bad.status is AuthStatus.CREDENTIAL_STORAGE_FAILED
    assert store.get("chan-bad") is None


# ---------------------------------------------------------------------------
# 1.7: decision > 300s -> authorization-timeout, no retrieval
# ---------------------------------------------------------------------------


def test_decision_after_300s_records_authorization_timeout():
    """A grant arriving after 300 s is recorded as authorization-timeout (1.7)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)

    # Even though granted, the decision came too late (> 300 s).
    grant = AuthorizationGrant(
        granted=True, credentials_ref="cred-late", responded_within_seconds=300.001
    )
    result = manager.complete_authorization("chan-timeout", grant, _empty_config())

    assert result.status is AuthStatus.AUTHORIZATION_TIMEOUT
    assert result.channel_id == "chan-timeout"
    assert "authorization-timeout" in (result.error or "")
    # No connected channel is produced, so no retrieval is attempted (1.7).
    assert store.get("chan-timeout") is None


def test_decision_exactly_at_300s_is_not_a_timeout():
    """A decision at exactly the 300 s boundary is still timely (1.7 boundary)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)

    grant = AuthorizationGrant(
        granted=True,
        credentials_ref="cred-edge",
        responded_within_seconds=ConnectionManager.AUTH_DECISION_TIMEOUT_SECONDS,
    )
    result = manager.complete_authorization("chan-edge", grant, _empty_config())

    # Boundary is inclusive: exactly 300 s connects rather than timing out.
    assert result.status is AuthStatus.CONNECTED
    assert store.get("chan-edge") is not None


def test_denial_after_300s_is_recorded_as_timeout_not_failure():
    """A late decision is a timeout regardless of grant/deny (1.7 precedence)."""
    manager = ConnectionManager(InMemoryCredentialStore())

    grant = AuthorizationGrant(
        granted=False, credentials_ref=None, responded_within_seconds=600.0
    )
    result = manager.complete_authorization("chan-late-deny", grant, _empty_config())

    assert result.status is AuthStatus.AUTHORIZATION_TIMEOUT


# ---------------------------------------------------------------------------
# 1.1: initiation issues the request and reports REQUESTED
# ---------------------------------------------------------------------------


def test_initiate_connection_issues_request_and_reports_requested():
    """Initiation issues the authorization request and reports REQUESTED (1.1)."""
    issued: list[str] = []
    manager = ConnectionManager(
        InMemoryCredentialStore(), request_issuer=issued.append
    )
    clock = FakeClock()

    result = manager.initiate_connection("chan-init", clock)

    assert result.status is AuthStatus.REQUESTED
    assert result.channel_id == "chan-init"
    assert issued == ["chan-init"]
