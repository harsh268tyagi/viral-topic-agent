"""Hypothesis property test for the Format_Recommender (task 15.2).

This module hosts Property 22 for :class:`FormatRecommender.recommend`.
Concrete, hand-checked examples and the insufficient-data boundary live in
``tests/test_format_recommender.py``; this module is the universal layer that
asserts the selection-and-tie-break contract across arbitrary inputs.

Property 22 (design.md -> Format_Recommender): *for any* idea with observed
view-count data for at least 5 short-format and at least 5 long-form template
videos, the recommender SHALL recommend exactly one format (12.1), choosing the
format with the higher observed *average* view count (12.2), defaulting to Short
on an exact tie of the two averages (12.3), and SHALL produce a rationale that
references the observed average view count for *both* formats (12.4).

Validates: Requirements 12.1, 12.2, 12.3, 12.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from analysis.format_recommender import (
    MIN_VIDEOS_PER_FORMAT,
    FormatRecommender,
)
from domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    VideoFormat,
    ViralTemplate,
)

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategy constrained to the property's scope (12.1): at least
# MIN_VIDEOS_PER_FORMAT view counts in *each* format so a recommendation is
# always produced (the fewer-than-5 branch (12.5) is its own task, 15.3). View
# counts are non-negative integers bounded so that the two averages, and the
# equality test for the tie-break, are exactly representable as floats.
# ---------------------------------------------------------------------------

# Non-negative view counts; bound keeps the mean of up to ~30 values exact.
_view_counts = st.integers(min_value=0, max_value=10**9)

# At least the minimum per format; the upper bound exercises uneven counts
# between the two formats (so the mean, not the sum, must drive the choice).
_format_views = st.lists(
    _view_counts, min_size=MIN_VIDEOS_PER_FORMAT, max_size=30
)


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """A valid ContentIdea with one associated template."""
    category = draw(st.sampled_from(list(ChannelCategory)))
    template = ViralTemplate(
        template_id="t0",
        name="tier-list ranking",
        category=category,
        observed_performance=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=20)),
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=category,
        templates=(template,),
        observed_metric_value=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )


# ---------------------------------------------------------------------------
# Property 22
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 22: Format recommendation selects the higher average with the Short tie-break
@settings(max_examples=200)
@given(idea=_ideas(), short_views=_format_views, long_views=_format_views)
def test_format_selection_and_tie_break(
    idea: ContentIdea,
    short_views: list[int],
    long_views: list[int],
) -> None:
    """Exactly one format, higher-average wins, Short on a tie, rationale both.

    Validates: Requirements 12.1, 12.2, 12.3, 12.4
    """
    result = FormatRecommender().recommend(idea, short_views, long_views)

    # With >= 5 in each format the recommendation is always produced (12.1).
    assert result.insufficient_performance_data is False
    assert result.recommended in (VideoFormat.SHORT, VideoFormat.LONG_FORM)

    # Independently recompute the two averages and the expected choice.
    short_avg = sum(short_views) / len(short_views)
    long_avg = sum(long_views) / len(long_views)
    expected = (
        VideoFormat.LONG_FORM if long_avg > short_avg else VideoFormat.SHORT
    )

    # (12.2) higher observed average wins; (12.3) exact tie -> Short.
    assert result.recommended is expected
    assert result.short_avg == short_avg
    assert result.long_avg == long_avg

    # (12.4) the rationale references both observed averages and both formats.
    assert result.rationale is not None
    assert f"{short_avg:.2f}" in result.rationale
    assert f"{long_avg:.2f}" in result.rationale
    assert "short" in result.rationale.lower()
    assert "long" in result.rationale.lower()
