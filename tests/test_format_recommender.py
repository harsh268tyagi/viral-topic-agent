"""Example/edge-case tests for format recommendation (task 15.1).

Covers the concrete branches and tie-break behaviour of
``FormatRecommender.recommend`` for Requirement 12:

- with >= 5 short and >= 5 long videos, recommend exactly one format (12.1),
- choose the format with the higher observed average view count (12.2),
- default to Short on an exact tie of the averages (12.3),
- include a rationale referencing both averages (12.4),
- fewer than 5 in either format -> withhold + insufficient-performance-data
  identifying the idea (12.5).

The Hypothesis property test for Property 22 lives in a separate task (15.2);
this module focuses on examples and boundary cases.
"""

from __future__ import annotations

import pytest

from analysis.format_recommender import (
    MIN_VIDEOS_PER_FORMAT,
    FormatRecommender,
)
from domain.models import (
    ChannelCategory,
    ContentIdea,
    FormatResult,
    TimeWindow,
    VideoFormat,
    ViralTemplate,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _idea(idea_id: str = "idea-1") -> ContentIdea:
    template = ViralTemplate(
        template_id=f"{idea_id}-t0",
        name="tier-list ranking",
        category=ChannelCategory.GAMING,
        observed_performance=1000.0,
    )
    return ContentIdea(
        idea_id=idea_id,
        title_concept=f"Idea {idea_id}",
        rationale="metric value 1234 observed within the window",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(template,),
        observed_metric_value=1234.0,
    )


def _views(value: int, count: int = MIN_VIDEOS_PER_FORMAT) -> list[int]:
    """A list of ``count`` identical view counts (handy for fixed averages)."""
    return [value] * count


# ---------------------------------------------------------------------------
# Higher-average selection (12.1, 12.2)
# ---------------------------------------------------------------------------


def test_recommends_long_form_when_long_average_higher():
    result = FormatRecommender().recommend(
        _idea(), short_views=_views(1000), long_views=_views(5000)
    )

    assert isinstance(result, FormatResult)
    assert result.recommended is VideoFormat.LONG_FORM
    assert result.insufficient_performance_data is False
    assert result.short_avg == 1000.0
    assert result.long_avg == 5000.0


def test_recommends_short_when_short_average_higher():
    result = FormatRecommender().recommend(
        _idea(), short_views=_views(5000), long_views=_views(1000)
    )

    assert result.recommended is VideoFormat.SHORT
    assert result.insufficient_performance_data is False
    assert result.short_avg == 5000.0
    assert result.long_avg == 1000.0


def test_exactly_one_format_recommended():
    result = FormatRecommender().recommend(
        _idea(), short_views=_views(3000), long_views=_views(2000)
    )
    assert result.recommended in (VideoFormat.SHORT, VideoFormat.LONG_FORM)


def test_average_uses_mean_not_sum():
    # Long has more videos but a lower mean; short's higher mean must win even
    # though long's total views are larger.
    short = [1000, 1000, 1000, 1000, 1000]  # mean 1000
    long = [100, 100, 100, 100, 100, 100, 100, 100]  # mean 100, larger count
    result = FormatRecommender().recommend(_idea(), short, long)

    assert result.short_avg == 1000.0
    assert result.long_avg == 100.0
    assert result.recommended is VideoFormat.SHORT


# ---------------------------------------------------------------------------
# Tie-break -> Short (12.3)
# ---------------------------------------------------------------------------


def test_equal_averages_default_to_short():
    result = FormatRecommender().recommend(
        _idea(), short_views=_views(2000), long_views=_views(2000)
    )

    assert result.short_avg == result.long_avg
    assert result.recommended is VideoFormat.SHORT


def test_tie_with_different_distributions_same_mean_defaults_to_short():
    # Different per-video counts, identical means (both 3000).
    short = [1000, 2000, 3000, 4000, 5000]  # mean 3000
    long = [3000, 3000, 3000, 3000, 3000]  # mean 3000
    result = FormatRecommender().recommend(_idea(), short, long)

    assert result.short_avg == result.long_avg == 3000.0
    assert result.recommended is VideoFormat.SHORT


# ---------------------------------------------------------------------------
# Rationale references both averages (12.4)
# ---------------------------------------------------------------------------


def test_rationale_references_both_averages():
    result = FormatRecommender().recommend(
        _idea(), short_views=_views(1500), long_views=_views(4500)
    )

    assert result.rationale is not None
    # Both formatted averages appear in the rationale text.
    assert f"{result.short_avg:.2f}" in result.rationale
    assert f"{result.long_avg:.2f}" in result.rationale
    # The rationale mentions both formats explicitly.
    assert "short" in result.rationale.lower()
    assert "long" in result.rationale.lower()


# ---------------------------------------------------------------------------
# Insufficient data -> withhold (12.5)
# ---------------------------------------------------------------------------


def test_too_few_short_videos_withholds():
    result = FormatRecommender().recommend(
        _idea("idea-x"), short_views=_views(1000, count=4), long_views=_views(1000)
    )

    assert result.recommended is None
    assert result.insufficient_performance_data is True
    assert result.short_avg is None
    assert result.long_avg is None
    assert result.rationale is None
    assert result.idea_id == "idea-x"  # identifies the idea


def test_too_few_long_videos_withholds():
    result = FormatRecommender().recommend(
        _idea("idea-y"), short_views=_views(1000), long_views=_views(1000, count=4)
    )

    assert result.recommended is None
    assert result.insufficient_performance_data is True
    assert result.idea_id == "idea-y"


def test_both_formats_empty_withholds():
    result = FormatRecommender().recommend(_idea("idea-z"), short_views=[], long_views=[])

    assert result.recommended is None
    assert result.insufficient_performance_data is True
    assert result.idea_id == "idea-z"


def test_exactly_five_each_is_sufficient_boundary():
    # Exactly the minimum in each format -> a recommendation is produced.
    result = FormatRecommender().recommend(
        _idea(),
        short_views=_views(2000, count=MIN_VIDEOS_PER_FORMAT),
        long_views=_views(1000, count=MIN_VIDEOS_PER_FORMAT),
    )

    assert result.insufficient_performance_data is False
    assert result.recommended is VideoFormat.SHORT


@pytest.mark.parametrize("short_count", [0, 1, 4])
def test_short_below_threshold_withholds(short_count):
    result = FormatRecommender().recommend(
        _idea(),
        short_views=_views(1000, count=short_count) if short_count else [],
        long_views=_views(1000),
    )
    assert result.insufficient_performance_data is True
    assert result.recommended is None
