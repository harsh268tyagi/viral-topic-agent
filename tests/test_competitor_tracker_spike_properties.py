"""Hypothesis property test for competitor spike classification (task 9.3).

Validates Property 12 for
:meth:`~viral_topic_agent.competitor_tracker.CompetitorTracker.monitor`.

Property 12 (design.md -> Correctness Properties): *For any* monitored
competitor with at least 5 retrieved trailing-30-day videos, a video SHALL be
flagged as a spike if and only if its view count is greater than zero and its
ratio to the competitor's baseline is 3.0 or greater, and every flagged spike
SHALL record the channel id, video id, view count, and a spike factor equal to
that ratio.

The test drives the genuine :class:`ResilientDataSource` over a stub that serves
a generated video set for one competitor, so the real retrieval + baseline +
classification path is exercised without mocking. The expected spike set is
derived independently from the baseline computed by the shared helper.

# Feature: viral-topic-agent, Property 12: Competitor spike classification and recording

Validates: Requirements 6.4, 6.5
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from analysis.baseline import compute_baseline_view_count
from infrastructure.clock import FakeClock
from analysis.competitor_tracker import CompetitorTracker
from infrastructure.datasource import DataSource
from domain.models import Configuration, VideoStats
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy

# Mirror the tracker's spike threshold and baseline cap so the expected set can
# be recomputed independently (kept in sync with competitor_tracker constants).
_SPIKE_RATIO_THRESHOLD = 3.0
_BASELINE_CAP = 30
_MIN_RETRIEVED_VIDEOS = 5

_BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
_CHANNEL_ID = "comp-prop"


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class _SingleChannelSource:
    """A :class:`DataSource` serving one competitor's video list."""

    def __init__(self, videos: list[VideoStats]) -> None:
        self._videos = videos

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        return list(self._videos)

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(videos: list[VideoStats]) -> ResilientDataSource:
    source: DataSource = _SingleChannelSource(videos)
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


def _config_with_one_competitor() -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(_CHANNEL_ID,),
        schedule=None,
        delivery_destinations=(),
    )


# ---------------------------------------------------------------------------
# Generator: >= 5 videos with distinct publish dates and arbitrary view counts
# (including zeros, to exercise the "view count > 0" half of the iff).
# ---------------------------------------------------------------------------


@st.composite
def _videos_at_least_five(draw: st.DrawFn) -> list[VideoStats]:
    offsets = draw(
        st.lists(
            st.integers(min_value=0, max_value=2_000_000),
            unique=True,
            min_size=_MIN_RETRIEVED_VIDEOS,
            max_size=40,
        )
    )
    counts = draw(
        st.lists(
            st.integers(min_value=0, max_value=10**7),
            min_size=len(offsets),
            max_size=len(offsets),
        )
    )
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


# Feature: viral-topic-agent, Property 12: Competitor spike classification and recording
@settings(max_examples=200)
@given(videos=_videos_at_least_five())
def test_spike_classification_is_iff_and_records_full_detail(
    videos: list[VideoStats],
) -> None:
    """A video spikes iff views > 0 and ratio >= 3.0; spikes record full detail.

    Validates: Requirements 6.4, 6.5
    """
    reports = CompetitorTracker().monitor(
        _config_with_one_competitor(), _resilient(videos)
    )

    assert len(reports) == 1
    report = reports[0]
    assert report.channel_id == _CHANNEL_ID

    # >= 5 videos were served, so monitoring runs (status ok) and the baseline
    # matches the shared helper over the most-recent-30 view counts.
    expected_baseline = compute_baseline_view_count(videos, cap=_BASELINE_CAP)
    assert report.baseline == expected_baseline
    baseline_value = expected_baseline.value

    if baseline_value is None or baseline_value <= 0:
        # A zero/unavailable baseline yields no classifiable ratio -> no spikes.
        assert report.spikes == ()
        return

    # Independently derive the expected spikes: views > 0 and ratio >= 3.0.
    expected_spike_ids = {
        v.video_id
        for v in videos
        if v.view_count > 0 and v.view_count / baseline_value >= _SPIKE_RATIO_THRESHOLD
    }
    actual_spike_ids = {s.video_id for s in report.spikes}
    assert actual_spike_ids == expected_spike_ids

    # No spurious spikes (iff direction: nothing flagged that fails the rule).
    by_id = {v.video_id: v for v in videos}
    for spike in report.spikes:
        source_video = by_id[spike.video_id]
        # 6.5: each spike records channel id, video id, view count, factor.
        assert spike.channel_id == _CHANNEL_ID
        assert spike.view_count == source_video.view_count
        assert spike.view_count > 0
        assert spike.spike_factor == source_video.view_count / baseline_value
        assert spike.spike_factor >= _SPIKE_RATIO_THRESHOLD
