"""The external ``DataSource`` abstraction and its failure model.

This module defines the single external dependency boundary for YouTube data
(``.kiro/specs/viral-topic-agent/design.md`` -> *Data_Source (abstraction)*)
and the error/classification model that ``ResilientDataSource`` (task 2.2)
will use to satisfy Requirement 16.

Contents:

- :class:`DataSource` - a ``Protocol`` describing the five retrieval calls every
  component depends on. Components never call a ``DataSource`` directly; they go
  through ``ResilientDataSource``.
- :class:`DataSourceError` and its subtypes - the exceptions a concrete
  ``DataSource`` may raise. ``ResilientDataSource`` catches these and classifies
  them (rate-limited / transient / non-transient).
- :class:`FailureClassification` - the classification an error maps to.
- :class:`DataRequest` - a reified, replayable description of a single call,
  so the resilience layer can retry it and name its ``target`` in failures.
- :class:`DataSourceFailure` - the recorded failure carrying ``target``,
  ``reason``, and ``classification`` (16.6), returned inside an ``Err``.

Requirements traceability: 16.6 (failures carry request target + reason);
supports 16.1-16.7 once the resilience layer consumes these types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from viral_topic_agent.domain.models import (
    AudienceActivity,
    ChannelCategory,
    ChannelMetadata,
    KeywordMetric,
    TemplatePerformance,
    VideoStats,
)

__all__ = [
    # Protocol
    "DataSource",
    # Errors
    "DataSourceError",
    "RateLimitError",
    "TransientError",
    "NonTransientError",
    "TimeoutError",
    # Classification + request/failure model
    "FailureClassification",
    "DataOperation",
    "DataRequest",
    "DataSourceFailure",
]


# ---------------------------------------------------------------------------
# DataSource protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class DataSource(Protocol):
    """The single external dependency for YouTube data.

    The concrete provider (e.g. the YouTube Data API) is selected at
    deployment time. Any call may raise a :class:`DataSourceError` subtype;
    callers reach this protocol only through ``ResilientDataSource``.
    """

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata: ...

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]: ...

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity: ...

    def get_keyword_metrics(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]: ...

    def get_template_performance(
        self, category: ChannelCategory
    ) -> list[TemplatePerformance]: ...


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class DataSourceError(Exception):
    """Base class for any error raised by a concrete :class:`DataSource`.

    ``reason`` is a human-readable description recorded in a
    :class:`DataSourceFailure`. Subtypes determine how
    ``ResilientDataSource`` classifies and reacts to the error.
    """

    def __init__(self, reason: str = "") -> None:
        super().__init__(reason)
        self.reason = reason or self.__class__.__name__


class RateLimitError(DataSourceError):
    """The provider reported a rate limit (16.1).

    ``retry_after_seconds`` is the provider-reported pause interval, or
    ``None`` when the provider reports no interval (the resilience layer then
    falls back to its default pause).
    """

    def __init__(
        self, reason: str = "rate limit exceeded", retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(reason)
        self.retry_after_seconds = retry_after_seconds


class TransientError(DataSourceError):
    """A retryable failure: network timeout, connection reset, or temporary
    unavailability (16.2, 16.4)."""


class NonTransientError(DataSourceError):
    """A non-retryable failure: auth rejection, invalid request, or
    target-not-found (16.3)."""


class TimeoutError(TransientError):  # noqa: A001 - intentionally shadows builtin within package
    """No complete response within the request timeout (16.4).

    Treated as a transient failure. Named ``TimeoutError`` per the design;
    referenced within the package as ``datasource.TimeoutError`` to avoid
    confusion with the builtin.
    """


# ---------------------------------------------------------------------------
# Classification + request/failure model
# ---------------------------------------------------------------------------


class FailureClassification(Enum):
    """How ``ResilientDataSource`` classifies a recorded failure."""

    RATE_LIMITED = "rate-limited"
    TRANSIENT = "transient"
    NON_TRANSIENT = "non-transient"
    RATE_LIMIT_TIMEOUT = "rate-limit-timeout"


class DataOperation(Enum):
    """Which :class:`DataSource` method a :class:`DataRequest` invokes."""

    CHANNEL_METADATA = "get_channel_metadata"
    VIDEOS = "get_videos"
    AUDIENCE_ACTIVITY = "get_audience_activity"
    KEYWORD_METRICS = "get_keyword_metrics"
    TEMPLATE_PERFORMANCE = "get_template_performance"


@dataclass(frozen=True)
class DataRequest:
    """A reified, replayable description of a single ``DataSource`` call.

    Reifying the call lets ``ResilientDataSource`` (a) retry it without the
    caller re-issuing it and (b) name a stable ``target`` when recording a
    failure (16.6). ``operation`` selects the method; ``params`` carries its
    arguments; ``target`` is a human-readable identifier of what was being
    requested (e.g. the channel id or category) for failure reporting.
    """

    operation: DataOperation
    target: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def invoke(self, source: DataSource) -> Any:
        """Dispatch this request against ``source`` and return its result.

        Raises whatever :class:`DataSourceError` the underlying source raises;
        ``ResilientDataSource`` is responsible for catching and classifying.
        """
        method = getattr(source, self.operation.value)
        return method(**dict(self.params))


@dataclass(frozen=True)
class DataSourceFailure:
    """A recorded ``DataSource`` failure (16.6).

    Carries ``target``, ``reason``, and ``classification`` so a caller can
    render the failure in a digest or run summary without re-deriving it. When
    the failure follows retries, ``attempts`` records how many were made.
    """

    target: str
    reason: str
    classification: FailureClassification
    attempts: int = 1
