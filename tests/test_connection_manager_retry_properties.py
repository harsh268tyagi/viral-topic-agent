"""Hypothesis property test for failed-retrieval retry + credential retention (task 5.3).

Example-based and branch tests live in ``test_connection_manager.py``. This
module validates Property 4 for
:class:`~viral_topic_agent.connection_manager.ConnectionManager`.

Property 4 (design.md -> Correctness Properties): *For any* owned-channel data
retrieval that fails at the Data_Source, the retrieval is retried up to the
bounded number of attempts (3, via the resilience layer); if every attempt fails
the manager returns a ``data-retrieval-failed`` error that identifies the affected
channel; and the stored authorization credentials are retained unchanged.

The property drives the genuine :class:`ResilientDataSource` over a stub that
raises a configurable number of transient failures, so the 3-attempt bound and
the credential-retention guarantee are exercised end to end (no mocking of the
resilience layer).

# Feature: viral-topic-agent, Property 4: Failed owned-channel retrieval retries within bound, fails identifiably, and retains credentials

Validates: Requirements 1.9
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from infrastructure.clock import FakeClock
from connection.connection_manager import (
    ConnectionManager,
    InMemoryCredentialStore,
)
from infrastructure.datasource import (
    DataOperation,
    DataRequest,
    TransientError,
)
from domain.models import (
    AuthorizationGrant,
    AuthStatus,
    Configuration,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test double
#
# ``_FailingSource`` raises a TransientError on every ``get_videos`` call and
# counts how many times it was invoked, so the property can assert the
# resilience layer made exactly the bounded number of attempts before giving up.
# ---------------------------------------------------------------------------


class _FailingSource:
    """A :class:`DataSource` whose ``get_videos`` always fails transiently."""

    def __init__(self) -> None:
        self.attempts = 0

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):
        self.attempts += 1
        raise TransientError("data source temporarily unavailable")

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


def _connect(manager: ConnectionManager, channel_id: str):
    """Authorize ``channel_id`` and return its stored, connected channel."""
    config = Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(),
    )
    grant = AuthorizationGrant(
        granted=True,
        credentials_ref=f"cred::{channel_id}",
        responded_within_seconds=1.0,
    )
    result = manager.complete_authorization(channel_id, grant, config)
    assert result.status is AuthStatus.CONNECTED
    stored = manager.credential_store.get(channel_id)
    assert stored is not None
    return stored


_channel_ids = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122),
    min_size=1,
    max_size=16,
)
# max_attempts is the documented bound (3); the property checks the layer never
# exceeds whatever bound is configured.
_max_attempts = st.integers(min_value=1, max_value=5)


# Feature: viral-topic-agent, Property 4: Failed owned-channel retrieval retries within bound, fails identifiably, and retains credentials
@settings(max_examples=150)
@given(channel_id=_channel_ids, max_attempts=_max_attempts)
def test_failed_retrieval_retries_within_bound_fails_identifiably_and_retains_credentials(
    channel_id, max_attempts
):
    """A persistently failing retrieval is retried up to the bound, then fails with
    a channel-identifying ``data-retrieval-failed`` error while the stored
    credentials are retained unchanged (1.9)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)
    clock = FakeClock()

    channel = _connect(manager, channel_id)
    credentials_before = store.get(channel_id)

    failing = _FailingSource()
    source = ResilientDataSource(
        failing, RetryPolicy(max_attempts=max_attempts), clock
    )

    result = manager.retrieve_with_credentials(
        channel, _videos_request(channel_id), source, clock
    )

    # Retried within the bound: exactly ``max_attempts`` inner calls, no more.
    assert failing.attempts == max_attempts

    # Failed identifiably: data-retrieval-failed naming the affected channel.
    assert result.is_err()
    error = result.unwrap_err()
    assert error.status is AuthStatus.DATA_RETRIEVAL_FAILED
    assert error.channel_id == channel_id
    assert channel_id in error.reason
    assert "data-retrieval-failed" in error.reason

    # Credentials retained unchanged across the failed retrieval (1.9).
    credentials_after = store.get(channel_id)
    assert credentials_after == credentials_before
    assert credentials_after is not None
    assert credentials_after.credentials_ref == f"cred::{channel_id}"
    assert credentials_after.connected is True
