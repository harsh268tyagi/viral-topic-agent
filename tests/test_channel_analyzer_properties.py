"""Hypothesis property test for the Channel_Analyzer (task 4.4).

Example-based and branch tests live in ``test_channel_analyzer.py``. This module
validates Property 2 for
:class:`~viral_topic_agent.channel_analyzer.ChannelAnalyzer`.

Property 2 (design.md -> Correctness Properties): *For any* successfully
retrieved owned-channel data set, the produced channel profile SHALL carry the
subscriber count, a video count equal to the number of retrieved videos, the
detected category (or none), and a baseline consistent with Property 1.

A "successfully retrieved owned-channel data set" is one where both the metadata
call and the (un-windowed, i.e. complete) video-list call succeed. Because the
analyzer retrieves the channel's full published video list, the channel's
reported total video count equals the number of retrieved videos; the generator
encodes that consistency by setting ``metadata.video_count == len(videos)``.

# Feature: viral-topic-agent, Property 2: Channel profile is complete and consistent with retrieved data

Validates: Requirements 2.3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from analysis.baseline import compute_baseline_view_count
from analysis.channel_analyzer import ChannelAnalyzer
from infrastructure.clock import FakeClock
from infrastructure.datasource import DataSource
from domain.models import (
    ChannelCategory,
    ChannelMetadata,
    VideoStats,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy

# The analyzer computes the owned-channel baseline over the most-recent 30
# videos; mirror that cap here when independently recomputing the expected
# baseline (kept in sync with channel_analyzer._BASELINE_CAP).
_BASELINE_CAP = 30


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class _StubDataSource:
    """A :class:`DataSource` returning pre-canned metadata and videos.

    Only the two methods the analyzer exercises are meaningful; the rest exist
    so the stub structurally satisfies the protocol. Wrapping this in a real
    ``ResilientDataSource`` (rather than faking the resilience layer) keeps the
    property test free of mocks: it exercises the genuine retrieval path.
    """

    def __init__(self, metadata: ChannelMetadata, videos: list[VideoStats]) -> None:
        self._metadata = metadata
        self._videos = videos

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        return self._metadata

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        return list(self._videos)

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(metadata: ChannelMetadata, videos: list[VideoStats]) -> ResilientDataSource:
    source: DataSource = _StubDataSource(metadata, videos)
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


# ---------------------------------------------------------------------------
# Generators
#
# Videos carry distinct, fixed-width ISO publish dates (so recency ordering is
# unambiguous, matching the Property 1 generator) and arbitrary view counts.
# Metadata's reported ``video_count`` is pinned to the number of retrieved
# videos to model a consistent, complete retrieval.
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2021, 1, 1, tzinfo=timezone.utc)
_view_counts = st.integers(min_value=0, max_value=10**9)
_optional_category = st.one_of(st.none(), st.sampled_from(list(ChannelCategory)))


@st.composite
def _videos(draw: st.DrawFn) -> list[VideoStats]:
    """A list of 0..40 videos with distinct publish dates, arbitrary order."""
    offsets = draw(
        st.lists(
            st.integers(min_value=0, max_value=5_000_000),
            unique=True,
            min_size=0,
            max_size=40,
        )
    )
    counts = draw(st.lists(_view_counts, min_size=len(offsets), max_size=len(offsets)))
    videos = [
        VideoStats(
            video_id=f"v{i}",
            view_count=count,
            published_at=(_BASE_DATE + timedelta(minutes=offset)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            format=None,
        )
        for i, (offset, count) in enumerate(zip(offsets, counts))
    ]
    return draw(st.permutations(videos))


@st.composite
def _metadata_and_videos(draw: st.DrawFn) -> tuple[ChannelMetadata, list[VideoStats]]:
    videos = draw(_videos())
    metadata = ChannelMetadata(
        channel_id="chan-prop",
        title=draw(st.text(min_size=0, max_size=20)),
        subscriber_count=draw(st.integers(min_value=0, max_value=10**9)),
        # Complete retrieval: the channel's total equals the retrieved count.
        video_count=len(videos),
        detected_category=draw(_optional_category),
    )
    return metadata, videos


# ---------------------------------------------------------------------------
# Property 2
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 2: Channel profile is complete and consistent with retrieved data
@settings(max_examples=200)
@given(data=_metadata_and_videos())
def test_profile_is_complete_and_consistent_with_retrieved_data(
    data: tuple[ChannelMetadata, list[VideoStats]],
) -> None:
    """A successful retrieval yields a profile faithfully carrying every field.

    Validates: Requirements 2.3
    """
    metadata, videos = data
    channel_id = "chan-prop"

    result = ChannelAnalyzer().analyze(channel_id, _resilient(metadata, videos))

    # Both retrievals succeeded -> a profile (never a retrieval error).
    assert result.is_ok()
    profile = result.unwrap()

    # The profile identifies the analyzed channel.
    assert profile.channel_id == channel_id

    # Subscriber count and detected category are carried verbatim from metadata.
    assert profile.subscriber_count == metadata.subscriber_count
    assert profile.detected_category == metadata.detected_category

    # Video count equals the number of retrieved videos.
    assert profile.video_count == len(videos)

    # Baseline is consistent with Property 1 (median of most-recent-30).
    assert profile.baseline == compute_baseline_view_count(videos, cap=_BASELINE_CAP)

    # A fully successful retrieval records no partial-failure reason.
    assert profile.partial_failure_reason is None
