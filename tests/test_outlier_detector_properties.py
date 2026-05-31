"""Hypothesis property test for the Outlier_Detector (task 10.2).

This module hosts Property 14 for :class:`OutlierDetector.detect`. Concrete,
hand-checked examples and the insufficient-data boundaries live in
``tests/test_outlier_detector.py``; this module is the universal layer that
asserts the classification-and-recording contract across arbitrary inputs.

Property 14 (design.md -> Outlier_Detector): *for any* channel whose baseline is
available and strictly positive, a video SHALL be classified as an outlier *iff*
its view count is greater than zero AND the ratio ``view_count / baseline`` is
``5.0`` or greater; and every classified outlier SHALL record the video id, the
video view count, and the ``outlier_factor`` equal to ``view_count / baseline``.

Validates: Requirements 7.2, 7.3
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from domain.models import VideoStats
from analysis.outlier_detector import OutlierDetector

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategy constrained to the property's scope: at least 5 videos (so the
# fewer-than-5 branch (7.4) is not taken) with *distinct*, zero-padded ISO-8601
# publish dates and non-negative view counts. Distinct dates make "the
# most-recent up-to-50 videos" unambiguous, and distinct ids let us compare the
# recorded outlier set independently of ordering. The zero/unavailable-baseline
# branch (7.5) is excluded with ``assume`` so each example exercises the
# classification path the property is about.
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2020, 1, 1, tzinfo=timezone.utc)

# View counts are non-negative integers; the upper bound keeps ratios exactly
# representable while still allowing comfortably-above-5x spikes.
_view_counts = st.integers(min_value=0, max_value=10**9)

# Distinct minute offsets from the base date -> distinct, parseable publish
# dates. At least 5 videos so detection runs; the upper bound straddles the
# 50-video baseline cap so the "classify over all videos" path is exercised.
_offsets = st.lists(
    st.integers(min_value=0, max_value=5_000_000),
    unique=True,
    min_size=5,
    max_size=60,
)

# A video is an outlier when its views are at least this multiple of the
# baseline (kept in lock-step with the implementation's inclusive 5.0 boundary).
_OUTLIER_RATIO_THRESHOLD = 5.0


@st.composite
def _videos(draw: st.DrawFn) -> list[VideoStats]:
    """At least 5 videos with distinct ids and publish dates, arbitrary order."""
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
    # Decouple input order from recency: classification must not depend on it.
    return draw(st.permutations(videos))


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 14: Outlier classification and recording
@settings(max_examples=200)
@given(videos=_videos())
def test_outlier_classification_and_recording(videos: list[VideoStats]) -> None:
    """Classified iff views > 0 and ratio >= 5.0, recording id, views, factor.

    Validates: Requirements 7.2, 7.3
    """
    result = OutlierDetector().detect("ch-prop", videos)

    # Scope the property to an available, strictly-positive baseline; the
    # zero/unavailable-baseline case is the separate 7.5 insufficient-data path.
    assume(not result.insufficient_data)
    assert result.baseline is not None
    baseline_value = result.baseline.value
    assert baseline_value is not None and baseline_value > 0

    # Independently classify every video using the same inclusive 5.0 rule and
    # the baseline the detector reported, then compare the recorded set.
    expected = {
        (
            video.video_id,
            video.view_count,
            video.view_count / baseline_value,
        )
        for video in videos
        if video.view_count > 0
        and video.view_count / baseline_value >= _OUTLIER_RATIO_THRESHOLD
    }
    actual = {
        (outlier.video_id, outlier.view_count, outlier.outlier_factor)
        for outlier in result.outliers
    }

    # (7.2) classification membership matches the rule exactly, and
    # (7.3) each record carries the id, view count, and factor = views/baseline.
    assert actual == expected

    # Every recorded outlier independently satisfies the boundary and records a
    # factor equal to the exact ratio of its view count to the baseline.
    for outlier in result.outliers:
        assert outlier.view_count > 0
        assert outlier.outlier_factor >= _OUTLIER_RATIO_THRESHOLD
        assert outlier.outlier_factor == outlier.view_count / baseline_value
