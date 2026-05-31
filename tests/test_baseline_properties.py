"""Hypothesis property test for ``compute_baseline_view_count`` (task 4.2).

This module hosts Property 1 for the shared baseline computation. Concrete,
hand-checked examples and the confidence-boundary edge cases live in
``tests/test_baseline.py``; this module is the universal layer that asserts the
same contract across arbitrary inputs.

Property 1 (design.md -> ``Channel_Analyzer``): *for any* list of videos and any
positive cap ``N``, ``compute_baseline_view_count`` SHALL return a result whose

- ``value`` is ``statistics.median`` of the view counts of the most-recent
  ``min(N, len(videos))`` videos by publish date (``None`` when there are none),
- ``sample_size`` equals ``min(N, len(videos))``, and
- ``confidence`` follows the sample-size rule: ``UNAVAILABLE`` for 0 videos,
  ``LOW`` for 1-4 videos, and ``NORMAL`` for 5 or more.

This is the single source of truth for the channel baseline (cap 30, 2.2/2.4/2.7),
the competitor baseline (6.3), and the outlier baseline (cap 50, 7.1), so the
property is parameterised by an arbitrary positive cap.

# Feature: viral-topic-agent, Property 1: Baseline view count is the median of the most-recent capped sample with correct confidence

Validates: Requirements 2.2, 2.4, 2.7, 6.3, 7.1
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.analysis.baseline import compute_baseline_view_count
from viral_topic_agent.domain.models import Confidence, VideoStats

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategy constrained to the property's input space: lists of videos with
# *distinct*, zero-padded ISO-8601 publish dates. Distinct dates make "the
# most-recent min(N, len) videos" unambiguous (no tie-break), so the expected
# median can be derived independently of the implementation. Because every date
# uses the same fixed-width ``...Z`` format, lexical ordering of the timestamp
# string equals chronological ordering -- an independent recency check that does
# not reuse the implementation's datetime parsing.
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)

# View counts are non-negative integers (a count of views); the upper bound is
# large but keeps medians exactly representable.
_view_counts = st.integers(min_value=0, max_value=10**9)

# Distinct minute offsets from the base date -> distinct, parseable publish
# dates. ``max_size`` exercises samples that straddle common caps (well below,
# at, and above 5) without making each example slow.
_offsets = st.lists(
    st.integers(min_value=0, max_value=5_000_000),
    unique=True,
    min_size=0,
    max_size=15,
)


@st.composite
def _videos(draw: st.DrawFn) -> list[VideoStats]:
    """A list of videos with distinct publish dates, in arbitrary input order."""
    offsets = draw(_offsets)
    counts = draw(
        st.lists(_view_counts, min_size=len(offsets), max_size=len(offsets))
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

    # Decouple input order from recency: the function must select by publish
    # date regardless of how the caller orders the list.
    return draw(st.permutations(videos))


# Arbitrary positive cap. The range spans caps below, equal to, and above the
# generated population size, plus the production caps 30 (channel/competitor)
# and 50 (outlier).
_caps = st.integers(min_value=1, max_value=60)


def _expected_confidence(sample_size: int) -> Confidence:
    """The confidence the result must carry for a given sample size (2.4, 2.7)."""
    if sample_size == 0:
        return Confidence.UNAVAILABLE
    if sample_size < 5:
        return Confidence.LOW
    return Confidence.NORMAL


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 1: Baseline view count is the median of the most-recent capped sample with correct confidence
@settings(max_examples=200)
@given(videos=_videos(), cap=_caps)
def test_baseline_is_capped_recent_median_with_correct_confidence(
    videos: list[VideoStats], cap: int
) -> None:
    """value == median(most-recent min(cap, len)), sample_size, and confidence.

    Validates: Requirements 2.2, 2.4, 2.7, 6.3, 7.1
    """
    result = compute_baseline_view_count(videos, cap)

    expected_sample_size = min(cap, len(videos))

    # Independently recompute the most-recent sample. Distinct, fixed-width ISO
    # timestamps mean lexical descending order == chronological most-recent.
    most_recent = sorted(videos, key=lambda v: v.published_at, reverse=True)[
        :expected_sample_size
    ]

    # (b) sample_size is the number of videos actually used.
    assert result.sample_size == expected_sample_size

    # (a) value is the median of the selected view counts, or None when empty.
    if expected_sample_size == 0:
        assert result.value is None
    else:
        expected_value = float(
            statistics.median(video.view_count for video in most_recent)
        )
        assert result.value == expected_value

    # (c) confidence follows the 0 / 1-4 / 5+ sample-size rule.
    assert result.confidence == _expected_confidence(expected_sample_size)
