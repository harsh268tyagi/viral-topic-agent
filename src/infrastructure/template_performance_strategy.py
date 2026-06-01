"""The configurable ``TemplatePerformanceStrategy`` seam (Requirement 5).

The design (``.kiro/specs/real-provider-integration/design.md`` ->
*KeywordMetricsProvider / TemplatePerformanceStrategy*) notes that a ready-made
"viral template performance" feed does **not** exist and must be *derived* from
retrieved video statistics through a *configurable* strategy or be omitted.
This module defines that seam: the :class:`TemplatePerformanceStrategy`
``Protocol`` a concrete derivation strategy implements, and which
:class:`~infrastructure.youtube_data_source.YouTubeDataSource` delegates to from
:meth:`~infrastructure.youtube_data_source.YouTubeDataSource.get_template_performance`.

Behavioral contract (consumed by the data source):

- ``derive`` is applied to the retrieved :class:`~domain.models.VideoStats` for
  a :class:`~domain.models.ChannelCategory` and returns one
  :class:`~domain.models.TemplatePerformance` per derived template (5.1), each
  populating the template identifier, the channel category, the observed
  performance, the sample size, the Short-format average view count, and the
  long-form-format average view count (5.2).
- When **no** strategy is configured the data source returns an empty list
  without consulting any strategy (5.3); that degradation is the data source's
  responsibility, not the strategy's.
- If deriving template performance requires retrieving data from the YouTube
  Data API and that retrieval fails, the failure surfaces as the corresponding
  classified ``DataSourceError`` via the data source's request path (5.4).

The strategy is a structural ``Protocol`` (no base class to inherit), so any
object exposing a matching ``derive`` satisfies it -- keeping the seam testable
with a simple fake and free of import coupling.

Requirements traceability: 5.1 (one performance value per derived template),
5.2 (all fields populated), 5.3 (unconfigured degradation handled by the data
source), 5.4 (retrieval failure surfaces as a classified ``DataSourceError``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.models import ChannelCategory, TemplatePerformance, VideoStats

__all__ = ["TemplatePerformanceStrategy"]


@runtime_checkable
class TemplatePerformanceStrategy(Protocol):
    """A configurable derivation of template performance from video stats (Req. 5).

    Implementations derive viral-template-performance values for a
    :class:`~domain.models.ChannelCategory` by applying their strategy to the
    retrieved :class:`~domain.models.VideoStats` and return one
    :class:`~domain.models.TemplatePerformance` per derived template (5.1). Each
    returned value populates every field: the template identifier, the channel
    category, the observed performance, the sample size, and the per-format
    (Short / long-form) average view counts (5.2).

    The strategy operates only on the video statistics it is handed; it performs
    no network access, retry, or backoff of its own. When deriving requires
    additional YouTube Data API retrieval, that retrieval is performed by the
    data source so any failure is classified there (5.4).
    """

    def derive(
        self, category: ChannelCategory, videos: list[VideoStats]
    ) -> list[TemplatePerformance]: ...
