"""Hypothesis property test for channel-id tagging and the 50-channel cap (task 5.2).

Example-based and branch tests for the Connection_Manager live in
``test_connection_manager.py``. This module validates Property 3 for
:class:`~viral_topic_agent.connection_manager.ConnectionManager`.

Property 3 (design.md -> Correctness Properties): *For any* set of authorized
Owned_Channels -- up to the maximum of 50 -- every retrieved data set is tagged
with its own originating channel id (never another channel's), and an attempt to
authorize a channel beyond the 50-channel cap is refused rather than connected.

The two halves of the property are exercised together against the genuine
:class:`ResilientDataSource` (no mocking of the resilience layer): channels are
authorized one at a time through :meth:`ConnectionManager.complete_authorization`
accumulating into the :class:`Configuration`, and then each successfully connected
channel's data is retrieved and checked for correct, isolated tagging.

# Feature: viral-topic-agent, Property 3: Each owned channel's retrieved data is tagged with its own channel id, capped at 50

Validates: Requirements 1.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

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
# Test double
#
# The stub returns a payload derived from the requested channel id, so a
# mistagged or cross-wired result is detectable: the tagged channel id and the
# payload must agree. Wrapping it in a real ``ResilientDataSource`` keeps the
# retrieval path genuine.
# ---------------------------------------------------------------------------


class _PerChannelSource:
    """A :class:`DataSource` whose ``get_videos`` payload encodes the channel id."""

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):
        # The payload is uniquely tied to the requested channel so the property
        # can detect any cross-channel leakage in tagging.
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


# Distinct channel ids; sizes span below, at, and above the 50-channel cap.
_channel_ids = st.lists(
    st.text(
        alphabet=st.characters(min_codepoint=48, max_codepoint=122),
        min_size=1,
        max_size=12,
    ),
    unique=True,
    min_size=1,
    max_size=70,
)


# Feature: viral-topic-agent, Property 3: Each owned channel's retrieved data is tagged with its own channel id, capped at 50
@settings(max_examples=150)
@given(channel_ids=_channel_ids)
def test_retrieved_data_is_tagged_per_channel_and_capped_at_fifty(channel_ids):
    """Every retrieved data set carries its own channel id, and no more than 50
    channels can be connected (1.6)."""
    store = InMemoryCredentialStore()
    manager = ConnectionManager(store)
    clock = FakeClock()

    # Authorize each channel one at a time, threading the growing Configuration
    # through so the 50-channel cap is evaluated against the channels stored so far.
    authorized: list[AuthorizedChannel] = []
    for channel_id in channel_ids:
        config = Configuration(
            authorized_channels=tuple(authorized),
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

        if len(authorized) < ConnectionManager.MAX_OWNED_CHANNELS:
            # Within the cap: the channel connects and is persisted.
            assert result.status is AuthStatus.CONNECTED
            stored = store.get(channel_id)
            assert stored is not None
            assert stored.connected is True
            authorized.append(stored)
        else:
            # Beyond the cap: refused as a credential-storage failure, not connected.
            assert result.status is AuthStatus.CREDENTIAL_STORAGE_FAILED
            assert str(ConnectionManager.MAX_OWNED_CHANNELS) in (result.error or "")
            assert store.get(channel_id) is None

    # The cap is honored: at most 50 channels are ever connected.
    assert len(authorized) == min(len(channel_ids), ConnectionManager.MAX_OWNED_CHANNELS)

    # Every connected channel's retrieved data is tagged with its OWN id and
    # carries its OWN payload -- no cross-channel contamination (1.6).
    source = ResilientDataSource(_PerChannelSource(), RetryPolicy(), clock)
    for channel in authorized:
        retrieval = manager.retrieve_with_credentials(
            channel, _videos_request(channel.channel_id), source, clock
        )
        assert retrieval.is_ok()
        tagged = retrieval.unwrap()
        assert tagged.channel_id == channel.channel_id
        assert tagged.data == [f"videos::{channel.channel_id}"]
