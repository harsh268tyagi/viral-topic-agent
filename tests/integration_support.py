"""Shared test doubles and builders for the integration tests (tasks 20.1, 20.2).

This module is *not* a test module (its name does not match ``test_*``), so it
is never collected by pytest. It provides a single, fast, deterministic
:class:`StubDataSource` that satisfies the
:class:`~viral_topic_agent.datasource.DataSource` protocol plus the extra
keyword arguments the pipeline passes through ``DataRequest.invoke`` (e.g. the
trend engine's ``window``/``published_within_days`` and the competitor
tracker's ``published_within_days``).

The stub does no I/O and returns immediately, so when it is wrapped in a real
:class:`~viral_topic_agent.resilient_data_source.ResilientDataSource` over a
:class:`~viral_topic_agent.clock.RealClock`, the only wall-clock time consumed
is the component's own work. That is exactly what the latency-budget tests
(20.1) measure, and the same stub backs the end-to-end happy-path run (20.2).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from domain.models import (
    AudienceActivity,
    ChannelCategory,
    ChannelMetadata,
    HourlyActivity,
    KeywordMetric,
    VideoStats,
    ViralTemplate,
)

__all__ = [
    "make_videos",
    "default_templates",
    "StubDataSource",
]


def make_videos(view_counts: list[int]) -> list[VideoStats]:
    """Build :class:`VideoStats` with distinct, increasing publish dates.

    The view counts drive the median baseline; a list like
    ``[100, 100, 100, 100, 100, 1000]`` yields a baseline of ``100`` with a
    single ``10x`` video that is both an outlier (>= 5x, 7.2) and a competitor
    spike (>= 3x, 6.4).
    """
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        VideoStats(
            video_id=f"v{i + 1}",
            view_count=vc,
            published_at=(base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            format=None,
        )
        for i, vc in enumerate(view_counts)
    ]


def default_templates(category: ChannelCategory) -> tuple[ViralTemplate, ...]:
    """A handful of templates in ``category`` with descending performance.

    Returned from ``get_template_performance`` so the trend engine derives
    1..20 ideas per window, each anchored on a real observed metric value.
    """
    return tuple(
        ViralTemplate(
            template_id=f"tpl-{i}",
            name=f"Template {i}",
            category=category,
            observed_performance=float(1000 - i * 100),
        )
        for i in range(6)
    )


class StubDataSource:
    """A fast, deterministic in-memory ``DataSource`` for integration tests.

    Every method returns immediately with canned, internally-consistent data,
    so the only time a wrapping ``ResilientDataSource`` spends is the caller's
    own work (no network, no sleeps). The signatures accept the keyword
    arguments the pipeline forwards through ``DataRequest.invoke`` -- notably
    the trend engine calls ``get_template_performance(window=..., published_within_days=...)``
    and the channel/competitor/outlier steps call ``get_videos(channel_id=..., published_within_days=...)``.
    """

    def __init__(
        self,
        *,
        category: ChannelCategory = ChannelCategory.GAMING,
        videos: list[VideoStats] | None = None,
        templates: tuple[ViralTemplate, ...] | None = None,
        keyword_count: int = 1000,
    ) -> None:
        self.category = category
        # A baseline of 100 with one 10x video -> drives both outlier (7.2) and
        # competitor-spike (6.4) detection in the end-to-end run.
        self._videos = (
            videos if videos is not None else make_videos([100, 100, 100, 100, 100, 1000])
        )
        self._templates = (
            templates if templates is not None else default_templates(category)
        )
        self._keyword_count = keyword_count

    # -- DataSource protocol (+ pass-through kwargs) -----------------------

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=channel_id,
            title=f"Channel {channel_id}",
            subscriber_count=10_000,
            video_count=len(self._videos),
            detected_category=self.category,
        )

    def get_videos(
        self, channel_id: str | None = None, published_within_days: int | None = None
    ) -> list[VideoStats]:
        return list(self._videos)

    def get_audience_activity(
        self, channel_id: str | None = None, days: int = 7
    ) -> AudienceActivity:
        buckets = tuple(
            HourlyActivity(day_of_week=d, hour=h, activity=float((d * 24 + h) % 50))
            for d in range(7)
            for h in range(24)
        )
        return AudienceActivity(
            channel_id=channel_id, days_covered=max(days, 7), buckets=buckets
        )

    def get_keyword_metrics(
        self, category: ChannelCategory | None = None, max_keywords: int = 1000
    ) -> list[KeywordMetric]:
        n = min(self._keyword_count, max_keywords)
        return [
            KeywordMetric(
                keyword=f"kw-{i}",
                demand=float(i % 100),
                competition=float((i * 7) % 100),
            )
            for i in range(n)
        ]

    def get_template_performance(
        self, category: ChannelCategory | None = None, **_kwargs: object
    ) -> list[ViralTemplate]:
        # The trend engine forwards ``window``/``published_within_days`` here;
        # they are irrelevant to the stub, which always returns its templates.
        return list(self._templates)
