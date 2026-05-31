"""Example / branch tests for the Channel_Analyzer (task 4.5).

The universal Property 2 lives in ``test_channel_analyzer_properties.py``. This
module covers the concrete retrieval branches of
:class:`~viral_topic_agent.channel_analyzer.ChannelAnalyzer`:

- successful retrieval of channel metadata, the video list, and per-video view
  counts, surfaced into the profile (2.1, 2.3);
- a partial failure (one of the two retrievals fails) still produces a profile
  built from the retrieved data and records the failure reason (2.5);
- a total failure (no data retrievable at all) returns a ``DataRetrievalError``
  identifying the channel and the reason (2.6).

Tests drive the genuine :class:`ResilientDataSource` over a scripted stub with a
:class:`FakeClock`, so retries/timeouts are exercised instantly and without
mocking the resilience layer.

Requirements exercised: 2.1, 2.5, 2.6.
"""

from __future__ import annotations

from analysis.channel_analyzer import ChannelAnalyzer, DataRetrievalError
from infrastructure.clock import FakeClock
from infrastructure.datasource import (
    DataSource,
    NonTransientError,
)
from domain.models import (
    ChannelCategory,
    ChannelMetadata,
    Confidence,
    VideoStats,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ConfigurableSource:
    """A :class:`DataSource` stub whose two used calls succeed or raise.

    ``metadata`` / ``videos`` supply the success payloads; ``metadata_error`` /
    ``videos_error`` (when set) make the corresponding call raise instead. This
    lets a single stub express full success, either partial failure, or total
    failure. Every call is recorded in ``calls`` for retrieval assertions.
    """

    def __init__(
        self,
        *,
        metadata: ChannelMetadata | None = None,
        videos: list[VideoStats] | None = None,
        metadata_error: Exception | None = None,
        videos_error: Exception | None = None,
    ) -> None:
        self._metadata = metadata
        self._videos = videos
        self._metadata_error = metadata_error
        self._videos_error = videos_error
        self.calls: list[tuple[str, dict]] = []

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        self.calls.append(("get_channel_metadata", {"channel_id": channel_id}))
        if self._metadata_error is not None:
            raise self._metadata_error
        assert self._metadata is not None
        return self._metadata

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        self.calls.append(
            (
                "get_videos",
                {"channel_id": channel_id, "published_within_days": published_within_days},
            )
        )
        if self._videos_error is not None:
            raise self._videos_error
        assert self._videos is not None
        return list(self._videos)

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(source: DataSource) -> ResilientDataSource:
    # FakeClock makes any retry backoff instant; a single-attempt policy keeps
    # non-transient failures (used below) to one call each.
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


def _videos_on_days(view_counts: list[int]) -> list[VideoStats]:
    from datetime import datetime, timedelta, timezone

    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        VideoStats(
            video_id=f"v{i + 1}",
            view_count=vc,
            published_at=(base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            format=None,
        )
        for i, vc in enumerate(view_counts)
    ]


# ---------------------------------------------------------------------------
# 2.1 / 2.3: successful retrieval of metadata, video list, and view counts
# ---------------------------------------------------------------------------


def test_successful_analysis_retrieves_metadata_videos_and_view_counts():
    """Both retrievals succeed -> a profile carrying metadata + baseline (2.1, 2.3)."""
    metadata = ChannelMetadata(
        channel_id="chan-1",
        title="My Channel",
        subscriber_count=12_345,
        video_count=5,
        detected_category=ChannelCategory.GAMING,
    )
    videos = _videos_on_days([10, 20, 30, 40, 50])  # median 30, 5 videos -> NORMAL
    source = _ConfigurableSource(metadata=metadata, videos=videos)

    result = ChannelAnalyzer().analyze("chan-1", _resilient(source))

    assert result.is_ok()
    profile = result.unwrap()
    assert profile.channel_id == "chan-1"
    assert profile.detected_category == ChannelCategory.GAMING
    assert profile.subscriber_count == 12_345
    assert profile.video_count == 5
    assert profile.partial_failure_reason is None
    # Per-video view counts drove the baseline (median of 10..50 = 30).
    assert profile.baseline.value == 30.0
    assert profile.baseline.confidence == Confidence.NORMAL
    assert profile.baseline.sample_size == 5

    # Both the metadata and video-list calls were actually made (2.1).
    call_names = [name for name, _ in source.calls]
    assert "get_channel_metadata" in call_names
    assert "get_videos" in call_names


def test_zero_videos_yields_unavailable_baseline_but_still_a_profile():
    """A channel with no videos still produces a profile (2.3) with UNAVAILABLE baseline."""
    metadata = ChannelMetadata(
        channel_id="chan-empty",
        title="Empty",
        subscriber_count=7,
        video_count=0,
        detected_category=None,
    )
    source = _ConfigurableSource(metadata=metadata, videos=[])

    result = ChannelAnalyzer().analyze("chan-empty", _resilient(source))

    assert result.is_ok()
    profile = result.unwrap()
    assert profile.video_count == 0
    assert profile.baseline.value is None
    assert profile.baseline.confidence == Confidence.UNAVAILABLE


# ---------------------------------------------------------------------------
# 2.5: partial failure -> profile from retrieved data + recorded reason
# ---------------------------------------------------------------------------


def test_partial_failure_metadata_missing_records_reason_and_builds_from_videos():
    """Metadata fails but videos succeed: profile built from videos, reason recorded (2.5)."""
    videos = _videos_on_days([100, 200, 300])  # median 200
    source = _ConfigurableSource(
        metadata_error=NonTransientError("metadata not found"),
        videos=videos,
    )

    result = ChannelAnalyzer().analyze("chan-2", _resilient(source))

    assert result.is_ok()
    profile = result.unwrap()
    # Built from the retrieved videos.
    assert profile.video_count == 3
    assert profile.baseline.value == 200.0
    # Metadata was unavailable -> fall back to neutral values.
    assert profile.detected_category is None
    assert profile.subscriber_count == 0
    # The partial-failure reason is recorded and names the failure.
    assert profile.partial_failure_reason is not None
    assert "metadata not found" in profile.partial_failure_reason


def test_partial_failure_videos_missing_records_reason_and_builds_from_metadata():
    """Videos fail but metadata succeeds: profile from metadata, baseline UNAVAILABLE (2.5)."""
    metadata = ChannelMetadata(
        channel_id="chan-3",
        title="Has Metadata",
        subscriber_count=999,
        video_count=42,
        detected_category=ChannelCategory.MUSIC,
    )
    source = _ConfigurableSource(
        metadata=metadata,
        videos_error=NonTransientError("video list unavailable"),
    )

    result = ChannelAnalyzer().analyze("chan-3", _resilient(source))

    assert result.is_ok()
    profile = result.unwrap()
    # Metadata fields preserved.
    assert profile.subscriber_count == 999
    assert profile.detected_category == ChannelCategory.MUSIC
    assert profile.video_count == 42
    # No videos retrieved -> baseline UNAVAILABLE.
    assert profile.baseline.value is None
    assert profile.baseline.confidence == Confidence.UNAVAILABLE
    # Reason recorded.
    assert profile.partial_failure_reason is not None
    assert "video list unavailable" in profile.partial_failure_reason


# ---------------------------------------------------------------------------
# 2.6: total failure -> DataRetrievalError identifying channel + reason
# ---------------------------------------------------------------------------


def test_total_failure_returns_data_retrieval_error_with_channel_and_reason():
    """Both retrievals fail -> Err(DataRetrievalError) naming the channel + reason (2.6)."""
    source = _ConfigurableSource(
        metadata_error=NonTransientError("auth rejected"),
        videos_error=NonTransientError("auth rejected"),
    )

    result = ChannelAnalyzer().analyze("chan-dead", _resilient(source))

    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, DataRetrievalError)
    assert error.channel_id == "chan-dead"
    # The reason references both failed retrievals.
    assert "metadata retrieval failed" in error.reason
    assert "video retrieval failed" in error.reason
    assert "auth rejected" in error.reason
