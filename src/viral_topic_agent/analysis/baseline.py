"""Shared baseline (median) view-count computation.

The :func:`compute_baseline_view_count` function is the single source of truth
for the ``Baseline_View_Count`` used across the pipeline:

- ``Channel_Analyzer`` computes it over the most-recent 30 owned-channel videos
  (Requirements 2.2, 2.4, 2.7).
- ``Competitor_Tracker`` computes it per competitor over trailing-window videos
  (Requirement 6.3).
- ``Outlier_Detector`` computes it over the most-recent 50 published videos
  (Requirement 7.1).

Because every caller wants "the median of the most-recent up-to-N videos", the
recency selection and the confidence marker live here once, parameterised by a
caller-supplied cap ``N``.

Design reference: ``design.md`` -> Components -> ``Channel_Analyzer``::

    def compute_baseline_view_count(...) -> BaselineResult:
        \"\"\"Median of most-recent up-to-N counts; status reflects sample size.\"\"\"

Confidence rules (Requirements 2.4, 2.7):

- ``0`` videos          -> value ``None``, confidence ``UNAVAILABLE``
- ``1``-``4`` videos    -> confidence ``LOW`` (low-confidence baseline)
- ``5`` or more videos  -> confidence ``NORMAL``
"""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import datetime, timezone

from viral_topic_agent.domain.models import BaselineResult, Confidence, VideoStats

__all__ = ["compute_baseline_view_count"]

# Sample-size boundary at/above which the baseline is full-confidence (2.7).
_NORMAL_CONFIDENCE_MIN_SAMPLE = 5


def _recency_key(video: VideoStats) -> datetime:
    """Return a timezone-aware datetime used to order videos by recency.

    ``VideoStats.published_at`` is an ISO-8601 string. We parse it so that
    ordering is chronological rather than lexical, and normalise naive
    timestamps to UTC so aware/naive values never get compared (which would
    raise ``TypeError``).
    """

    raw = video.published_at
    # ``datetime.fromisoformat`` accepts a trailing ``Z`` only from Python 3.11,
    # but normalising explicitly keeps the intent obvious and robust.
    normalized = f"{raw[:-1]}+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def compute_baseline_view_count(
    videos: Sequence[VideoStats], cap: int
) -> BaselineResult:
    """Compute the median view count of the most-recent ``min(cap, len)`` videos.

    Args:
        videos: The candidate videos. Only their ``view_count`` and
            ``published_at`` fields are used. The input order is irrelevant;
            videos are selected by publish date (most recent first).
        cap: The maximum number of most-recent videos to include in the median
            (``N``). Callers pass 30 (channel/competitor) or 50 (outlier).

    Returns:
        A :class:`BaselineResult` whose ``value`` is the median of the selected
        view counts (``None`` when there are no videos), ``sample_size`` is the
        number of videos actually used (``min(cap, len(videos))``), and
        ``confidence`` reflects that sample size.

    Raises:
        ValueError: If ``cap`` is less than 1. "Most-recent N videos" is not
            meaningful for a non-positive cap, so this is treated as a caller
            error rather than silently producing an unavailable baseline.
    """

    if cap < 1:
        raise ValueError(f"cap must be a positive integer, got {cap!r}")

    # Select the most-recent up-to-cap videos by publish date. Python's sort is
    # stable, so videos sharing a publish date keep their relative input order.
    sample_size = min(cap, len(videos))
    if sample_size == 0:
        return BaselineResult(
            value=None, confidence=Confidence.UNAVAILABLE, sample_size=0
        )

    most_recent = sorted(videos, key=_recency_key, reverse=True)[:sample_size]
    view_counts = [video.view_count for video in most_recent]

    # ``statistics.median`` returns the middle element for odd counts and the
    # mean of the two middle elements for even counts; cast to float so the
    # result type is consistent with ``BaselineResult.value``.
    median_value = float(statistics.median(view_counts))

    confidence = (
        Confidence.NORMAL
        if sample_size >= _NORMAL_CONFIDENCE_MIN_SAMPLE
        else Confidence.LOW
    )

    return BaselineResult(
        value=median_value, confidence=confidence, sample_size=sample_size
    )
