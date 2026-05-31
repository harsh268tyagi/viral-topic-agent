"""Format recommendation (Requirement 12).

The :class:`FormatRecommender` decides whether a
:class:`~viral_topic_agent.models.ContentIdea` is better produced as a Short or
a long-form video, based on the observed average view counts of the idea's
Viral_Template videos in each format.

Design references (``.kiro/specs/viral-topic-agent/design.md`` -> Format_Recommender):

- With observed view-count data for **at least 5** short-format and **at least
  5** long-form template videos, recommend exactly one format (12.1).
- Choose the format with the higher observed *average* view count (12.2).
- On an exact tie of the two averages, recommend Short (12.3).
- The recommendation carries a rationale that references the observed average
  view count for *both* formats (12.4).
- If fewer than 5 videos are available in *either* format, withhold the
  recommendation and return an ``insufficient-performance-data`` indicator
  identifying the idea (12.5).

This component is a pure transformation over its inputs: it raises no
exceptions for the degraded (insufficient-data) state, instead returning a
:class:`~viral_topic_agent.models.FormatResult` whose
``insufficient_performance_data`` flag is set.

Requirements traceability: 12.1, 12.2, 12.3, 12.4, 12.5.
"""

from __future__ import annotations

from viral_topic_agent.domain.models import ContentIdea, FormatResult, VideoFormat

__all__ = [
    "MIN_VIDEOS_PER_FORMAT",
    "FormatRecommender",
]


# Minimum observed videos required *in each format* before a recommendation can
# be made (12.1, 12.5).
MIN_VIDEOS_PER_FORMAT = 5


def _average(view_counts: list[int]) -> float:
    """Arithmetic mean of a non-empty list of view counts.

    Callers only invoke this once the 5-video threshold has been met, so the
    list is guaranteed non-empty; the guard keeps the function total.
    """
    if not view_counts:
        return 0.0
    return sum(view_counts) / len(view_counts)


class FormatRecommender:
    """Recommends Short vs long-form for a Content_Idea (Requirement 12)."""

    def recommend(
        self,
        idea: ContentIdea,
        short_views: list[int],
        long_views: list[int],
    ) -> FormatResult:
        """Recommend exactly one video format, or withhold on insufficient data.

        - When fewer than :data:`MIN_VIDEOS_PER_FORMAT` videos are available in
          *either* format, the recommendation is withheld and
          ``insufficient_performance_data`` is set, identifying the idea (12.5).
        - Otherwise the format with the higher observed average view count is
          recommended (12.1, 12.2); on an exact tie Short is chosen (12.3). The
          rationale references both averages (12.4).
        """
        # Insufficient data in either format -> withhold (12.5).
        if (
            len(short_views) < MIN_VIDEOS_PER_FORMAT
            or len(long_views) < MIN_VIDEOS_PER_FORMAT
        ):
            return FormatResult(
                idea_id=idea.idea_id,
                recommended=None,
                short_avg=None,
                long_avg=None,
                rationale=None,
                insufficient_performance_data=True,
            )

        short_avg = _average(short_views)
        long_avg = _average(long_views)

        # Higher average wins; exact tie -> Short (12.2, 12.3).
        if long_avg > short_avg:
            recommended = VideoFormat.LONG_FORM
        else:
            recommended = VideoFormat.SHORT

        rationale = (
            f"Recommended {recommended.value}: short-format average view count "
            f"is {short_avg:.2f} across {len(short_views)} videos and long-form "
            f"average view count is {long_avg:.2f} across {len(long_views)} "
            f"videos."
        )

        return FormatResult(
            idea_id=idea.idea_id,
            recommended=recommended,
            short_avg=short_avg,
            long_avg=long_avg,
            rationale=rationale,
            insufficient_performance_data=False,
        )
