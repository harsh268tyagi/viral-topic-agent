"""Example / edge-case tests for ``compute_baseline_view_count`` (task 4.1).

These cover concrete, hand-checked behaviour:

- median for odd and even sample counts,
- capping to the most-recent ``N`` videos,
- recency selection (input order irrelevant; selection by publish date),
- confidence boundaries at 0, 1, 4, and 5 videos,
- the unavailable (zero-video) case,
- defensive handling of an invalid cap.

The universal Hypothesis property for this function is task 4.2 (Property 1);
this module is the example/edge layer that complements it.

Requirements exercised: 2.2, 2.4, 2.7, 6.3, 7.1.
"""

from datetime import datetime, timedelta, timezone

import pytest

from analysis.baseline import compute_baseline_view_count
from domain.models import Confidence, VideoFormat, VideoStats


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _video(view_count: int, published_at: str, video_id: str = "v") -> VideoStats:
    return VideoStats(
        video_id=video_id,
        view_count=view_count,
        published_at=published_at,
        format=VideoFormat.LONG_FORM,
    )


def _videos_on_days(view_counts: list[int]) -> list[VideoStats]:
    """Build videos on ascending, distinct publish dates one day apart.

    The video at index ``i`` is published on ``2024-01-01 + i days`` so the
    *last* element of ``view_counts`` is the most recent. Real date arithmetic
    is used so the helper produces valid calendar dates for any sample size.
    View counts are assigned in the same order as given.
    """
    videos: list[VideoStats] = []
    for i, vc in enumerate(view_counts):
        published_at = (_BASE_DATE + timedelta(days=i)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        videos.append(_video(vc, published_at, video_id=f"v{i + 1}"))
    return videos


# ---------------------------------------------------------------------------
# Median: odd / even counts
# ---------------------------------------------------------------------------


def test_median_odd_count_is_middle_value():
    """An odd number of videos yields the middle view count."""
    videos = _videos_on_days([10, 50, 30])  # sorted: 10, 30, 50 -> median 30
    result = compute_baseline_view_count(videos, cap=30)
    assert result.value == 30.0
    assert result.sample_size == 3
    assert result.confidence == Confidence.LOW


def test_median_even_count_is_mean_of_two_middle_values():
    """An even number of videos yields the mean of the two middle counts."""
    videos = _videos_on_days([10, 20, 30, 40])  # median of 20,30 -> 25.0
    result = compute_baseline_view_count(videos, cap=30)
    assert result.value == 25.0
    assert result.sample_size == 4
    assert result.confidence == Confidence.LOW


def test_median_even_count_can_be_fractional():
    """Even-count median averages, so a .5 result is possible and expected."""
    videos = _videos_on_days([10, 20, 30, 50, 50, 50])  # middles 30,50 -> 40.0
    result = compute_baseline_view_count(videos, cap=30)
    assert result.value == 40.0


def test_single_video_median_is_that_videos_views():
    videos = _videos_on_days([777])
    result = compute_baseline_view_count(videos, cap=30)
    assert result.value == 777.0
    assert result.sample_size == 1


# ---------------------------------------------------------------------------
# Capping behaviour
# ---------------------------------------------------------------------------


def test_cap_limits_sample_to_most_recent_n():
    """With more videos than the cap, only the most-recent N are used."""
    # Days 1..10 with view counts equal to the day number; most recent day = 10.
    videos = _videos_on_days([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    # cap=5 -> most-recent 5 are days 6..10 (views 6,7,8,9,10) -> median 8.
    result = compute_baseline_view_count(videos, cap=5)
    assert result.sample_size == 5
    assert result.value == 8.0
    assert result.confidence == Confidence.NORMAL


def test_cap_larger_than_population_uses_all_videos():
    """When cap exceeds the number of videos, every video is used."""
    videos = _videos_on_days([10, 20, 30])
    result = compute_baseline_view_count(videos, cap=30)
    assert result.sample_size == 3
    assert result.value == 20.0


def test_cap_30_matches_channel_analyzer_rule():
    """31 videos with cap=30 drops the single oldest video (2.2)."""
    # Oldest (day 1) has an extreme low value that would drag the median down
    # if (incorrectly) included.
    counts = [0] + [100] * 30  # day1=0, days 2..31 = 100
    videos = _videos_on_days(counts)
    result = compute_baseline_view_count(videos, cap=30)
    assert result.sample_size == 30
    assert result.value == 100.0  # the lone 0 was excluded by the cap


def test_cap_50_matches_outlier_detector_rule():
    """Up to the 50 most-recent videos are used for outlier baselines (7.1)."""
    videos = _videos_on_days(list(range(1, 61)))  # 60 videos, views 1..60
    result = compute_baseline_view_count(videos, cap=50)
    # Most-recent 50 are views 11..60 -> median of 35 and 36 -> 35.5
    assert result.sample_size == 50
    assert result.value == 35.5


# ---------------------------------------------------------------------------
# Recency selection (input order irrelevant)
# ---------------------------------------------------------------------------


def test_selection_is_by_publish_date_not_input_order():
    """Shuffled input still selects the most-recent videos by publish date."""
    videos = _videos_on_days([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    shuffled = [videos[3], videos[9], videos[0], videos[6], videos[1],
                videos[8], videos[2], videos[5], videos[4], videos[7]]
    capped_sorted = compute_baseline_view_count(videos, cap=4)
    capped_shuffled = compute_baseline_view_count(shuffled, cap=4)
    # Most recent 4 days are 7,8,9,10 -> median of 8,9 -> 8.5 regardless of order.
    assert capped_sorted.value == 8.5
    assert capped_shuffled.value == 8.5
    assert capped_sorted == capped_shuffled


def test_recency_handles_non_zero_padded_and_z_suffix_dates():
    """ISO timestamps with a trailing Z are parsed chronologically."""
    videos = [
        _video(100, "2024-02-01T00:00:00Z", "feb"),
        _video(200, "2024-01-15T00:00:00Z", "jan"),
        _video(300, "2024-03-10T00:00:00Z", "mar"),
    ]
    # Most recent single video is March (300).
    result = compute_baseline_view_count(videos, cap=1)
    assert result.value == 300.0
    assert result.sample_size == 1


def test_recency_handles_naive_and_aware_timestamps_together():
    """Mixing naive and ``Z``/offset-aware timestamps does not raise."""
    videos = [
        _video(10, "2024-01-01T00:00:00", "naive"),
        _video(20, "2024-01-02T00:00:00Z", "z"),
        _video(30, "2024-01-03T00:00:00+00:00", "offset"),
    ]
    result = compute_baseline_view_count(videos, cap=1)
    # Most recent is Jan 3 (30).
    assert result.value == 30.0


def test_videos_sharing_publish_date_are_all_eligible():
    """Ties on publish date are handled without error and counted in sample."""
    videos = [
        _video(10, "2024-01-01T00:00:00Z", "a"),
        _video(20, "2024-01-01T00:00:00Z", "b"),
        _video(30, "2024-01-01T00:00:00Z", "c"),
    ]
    result = compute_baseline_view_count(videos, cap=30)
    assert result.sample_size == 3
    assert result.value == 20.0


# ---------------------------------------------------------------------------
# Confidence boundaries: 0, 1, 4, 5 videos
# ---------------------------------------------------------------------------


def test_zero_videos_is_unavailable_with_none_value():
    """Zero videos -> value None, UNAVAILABLE confidence, sample_size 0 (2.4)."""
    result = compute_baseline_view_count([], cap=30)
    assert result.value is None
    assert result.confidence == Confidence.UNAVAILABLE
    assert result.sample_size == 0


def test_one_video_is_low_confidence():
    """A single video -> LOW confidence (2.7)."""
    result = compute_baseline_view_count(_videos_on_days([42]), cap=30)
    assert result.confidence == Confidence.LOW
    assert result.sample_size == 1


def test_four_videos_is_low_confidence_boundary():
    """Four videos is the upper boundary of the low-confidence band (2.7)."""
    result = compute_baseline_view_count(_videos_on_days([1, 2, 3, 4]), cap=30)
    assert result.confidence == Confidence.LOW
    assert result.sample_size == 4


def test_five_videos_is_normal_confidence_boundary():
    """Five videos is the lower boundary of full confidence (2.7)."""
    result = compute_baseline_view_count(_videos_on_days([1, 2, 3, 4, 5]), cap=30)
    assert result.confidence == Confidence.NORMAL
    assert result.sample_size == 5


def test_confidence_reflects_capped_sample_not_total_population():
    """Confidence is based on the sample actually used, not the input length."""
    # 100 videos but cap=4 -> sample_size 4 -> LOW confidence.
    videos = _videos_on_days(list(range(1, 101)))
    result = compute_baseline_view_count(videos, cap=4)
    assert result.sample_size == 4
    assert result.confidence == Confidence.LOW


# ---------------------------------------------------------------------------
# Defensive input handling
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_cap", [0, -1, -30])
def test_non_positive_cap_raises_value_error(bad_cap):
    """A non-positive cap is a caller error and raises ValueError."""
    with pytest.raises(ValueError):
        compute_baseline_view_count(_videos_on_days([1, 2, 3]), cap=bad_cap)


def test_zero_view_counts_are_valid_data():
    """View counts of zero are real data and included in the median."""
    videos = _videos_on_days([0, 0, 0])
    result = compute_baseline_view_count(videos, cap=30)
    assert result.value == 0.0
    assert result.sample_size == 3
    assert result.confidence == Confidence.LOW
