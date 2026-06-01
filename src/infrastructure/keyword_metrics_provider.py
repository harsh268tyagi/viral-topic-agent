"""The configurable ``KeywordMetricsProvider`` seam (Requirement 4).

The design (``.kiro/specs/real-provider-integration/design.md`` ->
*KeywordMetricsProvider / TemplatePerformanceStrategy*) notes that keyword
demand and competition are **not** exposed by the public YouTube Data API and
must come from a *configurable* source or be omitted. This module defines that
seam: the :class:`KeywordMetricsProvider` ``Protocol`` that a concrete keyword
source implements, and which :class:`~infrastructure.youtube_data_source.YouTubeDataSource`
delegates to from :meth:`~infrastructure.youtube_data_source.YouTubeDataSource.get_keyword_metrics`.

Behavioral contract (consumed by the data source):

- ``fetch`` returns one :class:`~domain.models.KeywordMetric` per candidate
  keyword retrieved for the requested :class:`~domain.models.ChannelCategory`,
  each carrying that keyword's demand and competition values (4.1). The data
  source caps the returned list at the requested maximum (4.2).
- When **no** provider is configured the data source returns an empty list
  without consulting any provider (4.3); that degradation is the data source's
  responsibility, not the provider's.
- A configured provider that is unavailable or errors signals the failure by
  raising an :class:`~infrastructure.http_transport.HttpTransportError` (or a
  member of the existing ``DataSourceError`` hierarchy directly); the data
  source maps a transport-level signal through
  :func:`~infrastructure.youtube_error_mapping.classify_response` so the result
  is exactly one classified ``DataSourceError`` (4.4).

The provider is a structural ``Protocol`` (no base class to inherit), so any
object exposing a matching ``fetch`` satisfies it -- keeping the seam testable
with a simple fake and free of import coupling.

Requirements traceability: 4.1 (one metric per retrieved keyword), 4.2 (cap is
applied by the data source), 4.3 (unconfigured degradation handled by the data
source), 4.4 (failure surfaces as a classified ``DataSourceError``).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.models import ChannelCategory, KeywordMetric

__all__ = ["KeywordMetricsProvider"]


@runtime_checkable
class KeywordMetricsProvider(Protocol):
    """A configurable source of keyword demand and competition values (Req. 4).

    Implementations retrieve candidate keywords for a
    :class:`~domain.models.ChannelCategory` together with their demand and
    competition values and return one :class:`~domain.models.KeywordMetric` per
    keyword (4.1). ``max_keywords`` is the upper bound the caller is interested
    in; an implementation may use it to bound its own retrieval, but the
    authoritative cap (4.2) is applied by
    :class:`~infrastructure.youtube_data_source.YouTubeDataSource`.

    On failure (the source is unavailable or returns an error), an
    implementation raises an
    :class:`~infrastructure.http_transport.HttpTransportError` /
    :class:`~infrastructure.http_transport.HttpTimeoutError` (which the data
    source maps via
    :func:`~infrastructure.youtube_error_mapping.classify_response`) or a member
    of the existing ``DataSourceError`` hierarchy directly (4.4). It performs no
    retry or backoff of its own, leaving that policy to ``ResilientDataSource``.
    """

    def fetch(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]: ...
