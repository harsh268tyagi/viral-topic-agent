"""Competitor channel tracking (Requirement 6).

The :class:`CompetitorTracker` lets a Creator register rival channels and, on a
monitoring run, surfaces those channels' recent performance spikes so the
Creator can react to competitor successes.

Design reference (``.kiro/specs/viral-topic-agent/design.md`` -> Competitor_Tracker):

- :meth:`CompetitorTracker.add_competitor` stores a not-yet-present competitor
  id in the :class:`~viral_topic_agent.models.Configuration` (6.1). Adding an id
  that is already monitored is idempotent (the set is left unchanged), and once
  ``MAX_COMPETITORS`` (50) distinct competitors are monitored, a further *new*
  id is rejected with a ``limit-reached`` indication naming the maximum (6.8).
- :meth:`CompetitorTracker.monitor` retrieves, for each monitored competitor,
  the list of videos published within the trailing 30 days together with their
  per-video view counts via :class:`ResilientDataSource` (6.2), computes that
  competitor's ``Baseline_View_Count`` from the retrieved view counts via the
  shared :func:`~viral_topic_agent.baseline.compute_baseline_view_count` (6.3),
  and flags any video whose view count is greater than zero and whose ratio to
  the baseline is ``3.0`` or greater as a competitor spike (6.4), recording the
  channel id, video id, view count, and spike factor (6.5).
- A competitor with fewer than 5 retrieved videos is reported as
  ``insufficient-data`` with spike detection skipped (6.6); a competitor the
  Data_Source cannot serve is reported as ``unavailable`` (6.7). In both cases
  the remaining competitors are still monitored - one competitor's degraded
  state never blocks the others (this is also why every retrieval flows through
  ``ResilientDataSource``, which surfaces failures as ``Result`` values).

The component is pure with respect to its inputs apart from the explicit,
returned configuration update: :meth:`add_competitor` returns a *new*
``Configuration`` (the model is frozen/immutable) rather than mutating in place.

Requirements traceability: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from viral_topic_agent.analysis.baseline import compute_baseline_view_count
from viral_topic_agent.infrastructure.datasource import DataOperation, DataRequest
from viral_topic_agent.domain.models import (
    CompetitorReport,
    CompetitorSpike,
    Configuration,
    VideoStats,
)
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource

__all__ = ["CompetitorTracker", "AddResult"]

# Competitor videos are retrieved over the trailing 30 days (6.2); the per
# competitor baseline is the median of the most-recent up-to-30 of those (6.3).
_TRAILING_WINDOW_DAYS = 30
_BASELINE_CAP = 30

# Minimum retrieved videos required to attempt spike detection (6.6).
_MIN_RETRIEVED_VIDEOS = 5

# A video is a spike when its views are at least this multiple of the baseline
# (6.4). The boundary is inclusive: a ratio of exactly 3.0 qualifies.
_SPIKE_RATIO_THRESHOLD = 3.0

# Per-competitor report status markers (design.md -> CompetitorReport.status).
_STATUS_OK = "ok"
_STATUS_INSUFFICIENT_DATA = "insufficient-data"
_STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class AddResult:
    """Outcome of :meth:`CompetitorTracker.add_competitor`.

    ``config`` is the resulting configuration - a new object carrying the added
    competitor when one was stored, or the unchanged input configuration when
    the id was already present (idempotent) or the addition was rejected at the
    limit. ``added`` is ``True`` only when a new id was stored. ``limit_reached``
    is ``True`` when the addition was rejected because the configuration already
    monitors ``MAX_COMPETITORS`` distinct competitors, in which case ``error``
    carries a ``limit-reached`` indication naming the maximum (6.8).
    """

    config: Configuration
    added: bool
    limit_reached: bool = False
    error: str | None = None


class CompetitorTracker:
    """Registers and monitors competitor channels (Requirement 6)."""

    MAX_COMPETITORS = 50

    def add_competitor(self, config: Configuration, channel_id: str) -> AddResult:
        """Register ``channel_id`` as a monitored competitor in ``config`` (6.1, 6.8).

        Args:
            config: The current configuration. It is never mutated; a new
                configuration is returned when the monitored set changes.
            channel_id: The competitor channel id to add.

        Returns:
            An :class:`AddResult`. When ``channel_id`` is not already monitored
            and the configuration holds fewer than ``MAX_COMPETITORS`` (50)
            competitors, the id is appended and ``added`` is ``True`` (6.1).
            When ``channel_id`` is already monitored the call is idempotent: the
            configuration is returned unchanged with ``added=False`` (6.1). When
            the configuration already monitors 50 competitors and ``channel_id``
            is new, the addition is rejected with ``limit_reached=True`` and an
            ``error`` naming the maximum (6.8).
        """
        existing = config.monitored_competitors

        # Idempotent: an already-monitored id leaves the set unchanged (6.1).
        # This takes precedence over the limit so re-adding an existing id never
        # spuriously trips the limit when the configuration is already full.
        if channel_id in existing:
            return AddResult(config=config, added=False)

        # 6.8: at the cap, reject a *new* id with a limit-reached indication
        # that names the maximum.
        if len(existing) >= self.MAX_COMPETITORS:
            return AddResult(
                config=config,
                added=False,
                limit_reached=True,
                error=(
                    "limit-reached: cannot add competitor; the maximum of "
                    f"{self.MAX_COMPETITORS} monitored competitors has been reached"
                ),
            )

        # 6.1: store exactly the new id, leaving all other monitored competitors
        # unchanged (append preserves insertion order without disturbing them).
        updated = replace(config, monitored_competitors=existing + (channel_id,))
        return AddResult(config=updated, added=True)

    def monitor(
        self, config: Configuration, source: ResilientDataSource
    ) -> list[CompetitorReport]:
        """Monitor every competitor in ``config`` for performance spikes.

        For each monitored competitor, retrieves the trailing-30-day videos and
        their view counts through ``source`` (6.2), computes the competitor's
        baseline (6.3), and flags spikes (6.4, 6.5). Degraded competitors are
        isolated: a competitor with fewer than 5 retrieved videos is reported as
        ``insufficient-data`` with detection skipped (6.6) and an unavailable
        competitor is reported as ``unavailable`` (6.7), while every other
        competitor is still monitored.

        Args:
            config: The configuration whose ``monitored_competitors`` are
                monitored, in their stored order.
            source: The resilient data-source handle. All retrieval flows
                through it, so a failed retrieval surfaces as an ``Err`` rather
                than an exception and cannot abort the remaining competitors.

        Returns:
            One :class:`CompetitorReport` per monitored competitor, in the same
            order as ``config.monitored_competitors``.
        """
        reports: list[CompetitorReport] = []
        for channel_id in config.monitored_competitors:
            reports.append(self._monitor_one(channel_id, source))
        return reports

    def _monitor_one(
        self, channel_id: str, source: ResilientDataSource
    ) -> CompetitorReport:
        """Build the monitoring report for a single competitor channel."""
        result = source.call(
            DataRequest(
                operation=DataOperation.VIDEOS,
                target=channel_id,
                params={
                    "channel_id": channel_id,
                    "published_within_days": _TRAILING_WINDOW_DAYS,
                },
            )
        )

        # 6.7: the Data_Source could not serve this competitor -> unavailable,
        # while the caller continues with the rest.
        if result.is_err():
            return CompetitorReport(
                channel_id=channel_id,
                status=_STATUS_UNAVAILABLE,
                baseline=None,
                spikes=(),
            )

        videos: list[VideoStats] = list(result.unwrap())

        # 6.3: baseline over the retrieved view counts (most-recent up-to-30).
        baseline = compute_baseline_view_count(videos, cap=_BASELINE_CAP)

        # 6.6: fewer than 5 retrieved videos -> insufficient data, no detection.
        if len(videos) < _MIN_RETRIEVED_VIDEOS:
            return CompetitorReport(
                channel_id=channel_id,
                status=_STATUS_INSUFFICIENT_DATA,
                baseline=baseline,
                spikes=(),
            )

        spikes = self._detect_spikes(channel_id, videos, baseline.value)
        return CompetitorReport(
            channel_id=channel_id,
            status=_STATUS_OK,
            baseline=baseline,
            spikes=spikes,
        )

    @staticmethod
    def _detect_spikes(
        channel_id: str, videos: list[VideoStats], baseline_value: float | None
    ) -> tuple[CompetitorSpike, ...]:
        """Flag videos whose views are > 0 and >= 3x the baseline (6.4, 6.5)."""
        # A non-positive or unavailable baseline yields no meaningful ratio, so
        # no spikes can be flagged. (With >= 5 videos the baseline median can
        # still be zero if most videos have zero views.)
        if baseline_value is None or baseline_value <= 0:
            return ()

        spikes: list[CompetitorSpike] = []
        for video in videos:
            if video.view_count <= 0:
                continue
            factor = video.view_count / baseline_value
            if factor >= _SPIKE_RATIO_THRESHOLD:
                spikes.append(
                    CompetitorSpike(
                        channel_id=channel_id,
                        video_id=video.video_id,
                        view_count=video.view_count,
                        spike_factor=factor,
                    )
                )
        return tuple(spikes)
