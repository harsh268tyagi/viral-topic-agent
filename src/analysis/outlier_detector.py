"""Outlier and viral detection (Requirement 7).

The :class:`OutlierDetector` identifies Owned_Channel videos whose view count
substantially exceeds the channel's recent baseline, surfacing "proven viral"
content for the Creator.

Design reference (``.kiro/specs/viral-topic-agent/design.md`` -> Outlier_Detector):

- Compute the ``Baseline_View_Count`` from up to the 50 most-recent published
  videos, reusing the shared
  :func:`~viral_topic_agent.baseline.compute_baseline_view_count` with
  ``cap=50`` (7.1).
- When the baseline is greater than zero and a video view count is greater than
  zero and the ratio ``view_count / baseline`` is ``5.0`` or greater, classify
  the video as an outlier and set ``outlier_factor`` to that ratio (7.2),
  recording the video id, view count, and factor (7.3).
- A channel with fewer than 5 published videos returns an ``insufficient-data``
  indicator and no outliers (7.4).
- A baseline that is zero or unavailable returns an ``insufficient-data``
  indicator and no outliers (7.5).

The component is pure with respect to its inputs: it consumes already-retrieved
:class:`~viral_topic_agent.models.VideoStats` and returns an
:class:`~viral_topic_agent.models.OutlierResult` carrying status markers rather
than raising for degraded states.

Requirements traceability: 7.1, 7.2, 7.3, 7.4, 7.5.
"""

from __future__ import annotations

from collections.abc import Sequence

from analysis.baseline import compute_baseline_view_count
from domain.models import Outlier, OutlierResult, VideoStats

__all__ = ["OutlierDetector"]

# Up to the 50 most-recent published videos seed the baseline (7.1).
_BASELINE_CAP = 50

# Minimum number of published videos required to attempt outlier detection (7.4).
_MIN_PUBLISHED_VIDEOS = 5

# A video is an outlier when its views are at least this multiple of the
# baseline (7.2). The boundary is inclusive: a ratio of exactly 5.0 qualifies.
_OUTLIER_RATIO_THRESHOLD = 5.0


class OutlierDetector:
    """Detects videos whose views far exceed a channel's baseline (Requirement 7)."""

    def detect(
        self, channel_id: str, videos: Sequence[VideoStats]
    ) -> OutlierResult:
        """Classify outliers for ``channel_id`` from its published ``videos``.

        Args:
            channel_id: Identifier of the channel under analysis. Always echoed
                back on the result so an ``insufficient-data`` outcome still
                identifies the affected channel (7.4, 7.5).
            videos: The channel's published videos. Only ``view_count`` and
                ``published_at`` are used; input order is irrelevant because the
                baseline selects the most-recent videos by publish date.

        Returns:
            An :class:`OutlierResult`. When detection cannot run (fewer than 5
            published videos, or a zero/unavailable baseline) the result has
            ``insufficient_data=True`` and no outliers. Otherwise it carries the
            classified outliers, each recording the video id, view count, and
            ``outlier_factor = view_count / baseline`` (>= 5.0).
        """
        # Baseline over up to the 50 most-recent published videos (7.1). Computed
        # once and surfaced on the result for transparency, even in the
        # insufficient-data branches.
        baseline = compute_baseline_view_count(videos, cap=_BASELINE_CAP)

        # 7.4: fewer than 5 published videos -> insufficient data, no outliers.
        if len(videos) < _MIN_PUBLISHED_VIDEOS:
            return OutlierResult(
                channel_id=channel_id,
                insufficient_data=True,
                baseline=baseline,
                outliers=(),
            )

        # 7.5: baseline zero or unavailable -> insufficient data, no outliers.
        baseline_value = baseline.value
        if baseline_value is None or baseline_value <= 0:
            return OutlierResult(
                channel_id=channel_id,
                insufficient_data=True,
                baseline=baseline,
                outliers=(),
            )

        # 7.2, 7.3: classify each video with views > 0 whose ratio to the
        # baseline is 5.0 or greater, recording id, views, and the factor.
        outliers: list[Outlier] = []
        for video in videos:
            if video.view_count <= 0:
                continue
            factor = video.view_count / baseline_value
            if factor >= _OUTLIER_RATIO_THRESHOLD:
                outliers.append(
                    Outlier(
                        video_id=video.video_id,
                        view_count=video.view_count,
                        outlier_factor=factor,
                    )
                )

        return OutlierResult(
            channel_id=channel_id,
            insufficient_data=False,
            baseline=baseline,
            outliers=tuple(outliers),
        )
