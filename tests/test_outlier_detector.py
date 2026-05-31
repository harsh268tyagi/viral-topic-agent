"""Example / edge-case tests for the Outlier_Detector (task 10.1).

These cover the concrete branches and boundaries of
``OutlierDetector.detect`` for Requirement 7:

- baseline computed from up to the 50 most-recent published videos (7.1),
- outlier classification at and around the 5.0 ratio boundary, with the factor
  recorded as ``view_count / baseline`` (7.2, 7.3),
- videos with zero views are never outliers (7.2),
- fewer than 5 published videos -> insufficient-data, no outliers (7.4),
- baseline zero or unavailable -> insufficient-data, no outliers (7.5).

The universal Hypothesis property for outlier classification is task 10.2
(Property 14); this module is the example/edge layer that complements it.

Requirements exercised: 7.1, 7.2, 7.3, 7.4, 7.5.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.models import Confidence, VideoFormat, VideoStats
from analysis.outlier_detector import OutlierDetector


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
    *last* element of ``view_counts`` is the most recent. View counts and ids
    are assigned in the same order as given (``v1`` is oldest).
    """
    videos: list[VideoStats] = []
    for i, vc in enumerate(view_counts):
        published_at = (_BASE_DATE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        videos.append(_video(vc, published_at, video_id=f"v{i + 1}"))
    return videos


# ---------------------------------------------------------------------------
# Insufficient data: fewer than 5 published videos (7.4)
# ---------------------------------------------------------------------------


def test_zero_videos_is_insufficient_data():
    result = OutlierDetector().detect("ch-1", [])
    assert result.channel_id == "ch-1"
    assert result.insufficient_data is True
    assert result.outliers == ()
    # Baseline is still surfaced; zero videos -> unavailable.
    assert result.baseline is not None
    assert result.baseline.confidence is Confidence.UNAVAILABLE


def test_four_videos_is_insufficient_data_even_with_huge_outlier():
    # Four videos, one wildly above the others; still insufficient (< 5).
    videos = _videos_on_days([100, 100, 100, 100_000])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.insufficient_data is True
    assert result.outliers == ()


def test_five_videos_is_enough_to_run_detection():
    # Boundary: exactly 5 videos -> detection runs (not insufficient on count).
    videos = _videos_on_days([100, 100, 100, 100, 100])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.insufficient_data is False


# ---------------------------------------------------------------------------
# Insufficient data: baseline zero or unavailable (7.5)
# ---------------------------------------------------------------------------


def test_zero_baseline_is_insufficient_data():
    # >= 5 videos but the median is zero -> insufficient data, no outliers.
    # Majority zeros force the median to 0 even with a couple of large videos.
    videos = _videos_on_days([0, 0, 0, 0, 0, 0, 999_999])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.baseline is not None
    assert result.baseline.value == 0.0
    assert result.insufficient_data is True
    assert result.outliers == ()


# ---------------------------------------------------------------------------
# Outlier classification and recording (7.2, 7.3)
# ---------------------------------------------------------------------------


def test_classifies_video_at_exactly_5x_baseline():
    # Baseline (median) is 100; a single video at exactly 500 -> ratio 5.0.
    # 5 videos at 100 plus one at 500 -> median across 6 is still 100.
    videos = _videos_on_days([100, 100, 100, 100, 100, 500])
    # Give the 500-view video a recognizable id (it is v6, the most recent).
    result = OutlierDetector().detect("ch-1", videos)

    assert result.insufficient_data is False
    assert result.baseline is not None
    assert result.baseline.value == 100.0

    assert len(result.outliers) == 1
    outlier = result.outliers[0]
    assert outlier.video_id == "v6"
    assert outlier.view_count == 500
    assert outlier.outlier_factor == 5.0


def test_does_not_classify_video_just_below_5x():
    # 499 / 100 = 4.99 -> not an outlier (boundary is inclusive at 5.0).
    videos = _videos_on_days([100, 100, 100, 100, 100, 499])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.baseline.value == 100.0
    assert result.outliers == ()


def test_outlier_factor_is_ratio_to_baseline():
    # Baseline 100, video at 1000 -> factor 10.0.
    videos = _videos_on_days([100, 100, 100, 100, 100, 1000])
    result = OutlierDetector().detect("ch-1", videos)
    assert len(result.outliers) == 1
    assert result.outliers[0].outlier_factor == 10.0
    assert result.outliers[0].view_count == 1000


def test_multiple_outliers_are_all_recorded():
    videos = _videos_on_days([100, 100, 100, 100, 100, 600, 800])
    result = OutlierDetector().detect("ch-1", videos)
    ids = {o.video_id for o in result.outliers}
    # v6 (600 -> 6.0) and v7 (800 -> 8.0) both qualify.
    assert ids == {"v6", "v7"}
    factors = {o.video_id: o.outlier_factor for o in result.outliers}
    assert factors["v6"] == 6.0
    assert factors["v7"] == 8.0


def test_no_outliers_when_all_within_baseline():
    videos = _videos_on_days([100, 110, 90, 120, 80, 130])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.insufficient_data is False
    assert result.outliers == ()


def test_zero_view_video_is_never_an_outlier():
    # A zero-view video must not be classified even though baseline > 0 (7.2).
    videos = _videos_on_days([100, 100, 100, 100, 100, 0])
    result = OutlierDetector().detect("ch-1", videos)
    assert result.baseline.value == 100.0
    assert result.outliers == ()


# ---------------------------------------------------------------------------
# Baseline uses up to the 50 most-recent videos (7.1)
# ---------------------------------------------------------------------------


def test_baseline_uses_only_most_recent_50_videos():
    # 60 videos: the 10 oldest are tiny (1 view) and would lower the median if
    # included, but the cap of 50 excludes them. Most-recent 50 are 100s, so the
    # baseline is 100 and a recent 500-view spike is a clean 5.0 outlier.
    counts = [1] * 10 + [100] * 49 + [500]
    videos = _videos_on_days(counts)
    result = OutlierDetector().detect("ch-1", videos)

    assert result.baseline is not None
    assert result.baseline.sample_size == 50
    assert result.baseline.value == 100.0
    assert len(result.outliers) == 1
    assert result.outliers[0].view_count == 500
    assert result.outliers[0].outlier_factor == 5.0


def test_detection_is_independent_of_input_order():
    ordered = _videos_on_days([100, 100, 100, 100, 100, 700])
    shuffled = [ordered[5], ordered[0], ordered[3], ordered[1], ordered[4], ordered[2]]
    result_ordered = OutlierDetector().detect("ch-1", ordered)
    result_shuffled = OutlierDetector().detect("ch-1", shuffled)
    assert result_ordered.outliers == result_shuffled.outliers
    assert result_ordered.baseline == result_shuffled.baseline


def test_channel_id_is_echoed_on_result():
    videos = _videos_on_days([100, 100, 100, 100, 100, 600])
    result = OutlierDetector().detect("my-channel", videos)
    assert result.channel_id == "my-channel"
