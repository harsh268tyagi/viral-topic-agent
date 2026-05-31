"""Core enums and immutable domain data models for the Viral Topic Agent.

This module defines the shared, frozen dataclasses and enums that flow between
every component of the pipeline (analysis, generation, delivery, orchestration).

Design decisions (see ``.kiro/specs/viral-topic-agent/design.md`` -> Data Models):

- **Immutability.** Every domain model is a ``frozen`` dataclass. Frozen
  dataclasses are hashable and have value-based (field-by-field) equality, which
  directly supports the configuration round-trip integrity requirement (15.4).
- **Tuple collections.** All collection fields use ``tuple`` rather than ``list``
  so the models remain immutable and so equality compares element-by-element.
  Lists would break both ``frozen`` hashing and value equality.
- **Status as data, not exceptions.** Degraded states (low confidence,
  insufficient data, no matches, etc.) are explicit fields rather than raised
  errors, so downstream components and the digest can render them.

Requirements traceability: 2.3, 5.1, 13.1, 15.1, 15.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = [
    # Enums
    "ChannelCategory",
    "TimeWindow",
    "Confidence",
    "VideoFormat",
    "DeliveryDestination",
    "StepStatus",
    "AuthStatus",
    # Dataclasses
    "ChannelMetadata",
    "HourlyActivity",
    "AudienceActivity",
    "TemplatePerformance",
    "VideoStats",
    "BaselineResult",
    "ChannelProfile",
    "ViralTemplate",
    "ContentIdea",
    "DiscoveryResult",
    "FilterResult",
    "ScoredIdea",
    "CompetitorSpike",
    "CompetitorReport",
    "Outlier",
    "OutlierResult",
    "TitleConcept",
    "ThumbnailConcept",
    "ConceptSet",
    "PublishRecommendation",
    "ScriptBundle",
    "KeywordMetric",
    "KeywordGap",
    "KeywordGapResult",
    "FormatResult",
    "DigestSection",
    "DigestReport",
    "DeliveryOutcome",
    "Schedule",
    "StepResult",
    "RunSummary",
    "AuthorizedChannel",
    "AuthorizationGrant",
    "AuthorizationResult",
    "Configuration",
]


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ChannelCategory(Enum):
    """Supported channel categories for filtering and discovery (4.3)."""

    GAMING = "gaming"
    MUSIC = "music"
    ENTERTAINMENT = "entertainment"
    SPORTS = "sports"


class TimeWindow(Enum):
    """Trend-discovery time windows."""

    WEEKLY = "weekly"  # trailing 7 days
    MONTHLY = "monthly"  # trailing 30 days
    ALL_TIME = "all_time"


class Confidence(Enum):
    """Confidence marker attached to derived results based on sample size."""

    NORMAL = "normal"
    LOW = "low_confidence"
    UNAVAILABLE = "unavailable"


class VideoFormat(Enum):
    """Video format used for format recommendation."""

    SHORT = "short"
    LONG_FORM = "long_form"


class DeliveryDestination(Enum):
    """Supported digest delivery destinations (13.5)."""

    EMAIL = "email"
    SLACK = "slack"
    NOTION = "notion"


class StepStatus(Enum):
    """Per-step status recorded in a run summary (14.6)."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class AuthStatus(Enum):
    """Channel authorization / connection lifecycle status (Requirement 1)."""

    REQUESTED = "requested"
    CONNECTED = "connected"
    AUTHORIZATION_FAILED = "authorization-failed"
    AUTHORIZATION_TIMEOUT = "authorization-timeout"
    AUTHORIZATION_EXPIRED = "authorization-expired"
    CREDENTIAL_STORAGE_FAILED = "credential-storage-failed"
    DATA_RETRIEVAL_FAILED = "data-retrieval-failed"


# ---------------------------------------------------------------------------
# Channel & baseline models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelMetadata:
    """Metadata for a channel as returned by the ``DataSource`` (2.1, 2.3).

    The raw metadata the analyzer turns into a :class:`ChannelProfile`. Kept
    separate from ``ChannelProfile`` because this is *retrieved* data, whereas
    the profile is *derived* (it also carries the computed baseline).
    """

    channel_id: str
    title: str
    subscriber_count: int
    video_count: int
    detected_category: ChannelCategory | None = None


@dataclass(frozen=True)
class HourlyActivity:
    """Audience activity for a single (day-of-week, hour) bucket (Requirement 9)."""

    day_of_week: int  # 0..6 (Monday=0)
    hour: int  # 0..23
    activity: float  # relative audience activity for this bucket


@dataclass(frozen=True)
class AudienceActivity:
    """Audience activity series used for publish-time prediction (9.1).

    ``days_covered`` lets the predictor enforce the "at least 7 days" rule (9.1)
    independently of how many hourly buckets are populated.
    """

    channel_id: str | None  # None for a category aggregate
    days_covered: int
    buckets: tuple[HourlyActivity, ...]


@dataclass(frozen=True)
class TemplatePerformance:
    """Observed performance of a viral template within a category.

    Returned by ``DataSource.get_template_performance`` and consumed by
    discovery, scoring, and format recommendation. ``sample_videos`` and the
    per-format averages support the Format_Recommender's 5-video threshold and
    higher-average selection (Requirement 12).
    """

    template_id: str
    category: ChannelCategory
    observed_performance: float
    sample_size: int = 0
    short_form_avg_views: float | None = None
    long_form_avg_views: float | None = None


@dataclass(frozen=True)
class VideoStats:
    """View statistics for a single video."""

    video_id: str
    view_count: int
    published_at: str  # ISO-8601
    format: VideoFormat | None = None


@dataclass(frozen=True)
class BaselineResult:
    """Baseline (median) view count for a channel with a confidence marker."""

    value: float | None  # median; None when unavailable
    confidence: Confidence  # NORMAL, LOW (1-4 videos), UNAVAILABLE (0 videos)
    sample_size: int


@dataclass(frozen=True)
class ChannelProfile:
    """Profile of an analyzed channel (2.3)."""

    channel_id: str
    detected_category: ChannelCategory | None
    subscriber_count: int
    video_count: int
    baseline: BaselineResult
    partial_failure_reason: str | None = None


# ---------------------------------------------------------------------------
# Discovery & filtering models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViralTemplate:
    """A reusable viral content template with observed performance."""

    template_id: str
    name: str  # e.g. "tier-list ranking", "reaction"
    category: ChannelCategory
    observed_performance: float  # aggregate observed view performance


@dataclass(frozen=True)
class ContentIdea:
    """A discovered content idea associated with 1-5 viral templates."""

    idea_id: str
    title_concept: str
    rationale: str
    time_window: TimeWindow
    category: ChannelCategory | None
    templates: tuple[ViralTemplate, ...]  # length 1..5
    observed_metric_value: float  # referenced metric within the window


@dataclass(frozen=True)
class DiscoveryResult:
    """Trend discovery output keyed by time window, with per-window errors."""

    ideas_by_window: dict[TimeWindow, tuple[ContentIdea, ...]]
    window_errors: dict[TimeWindow, str]  # window -> error indication


@dataclass(frozen=True)
class FilterResult:
    """Result of category filtering over ideas and templates."""

    ideas: tuple[ContentIdea, ...]
    templates: tuple[ViralTemplate, ...]
    no_matches: bool = False
    category_unavailable: bool = False
    applied_category: ChannelCategory | None = None
    error: str | None = None  # unsupported-category


# ---------------------------------------------------------------------------
# Scoring models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredIdea:
    """A content idea with an assigned (or withheld) score (5.1)."""

    idea: ContentIdea
    score: int | None  # 0..100, or None when withheld
    confidence: Confidence
    insufficient_data: bool = False


# ---------------------------------------------------------------------------
# Competitor & outlier models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompetitorSpike:
    """A competitor video whose views spiked >= 3x the competitor baseline."""

    channel_id: str
    video_id: str
    view_count: int
    spike_factor: float  # view_count / baseline, >= 3.0


@dataclass(frozen=True)
class CompetitorReport:
    """Monitoring report for a single competitor channel."""

    channel_id: str
    status: str  # ok | insufficient-data | unavailable
    baseline: BaselineResult | None
    spikes: tuple[CompetitorSpike, ...]


@dataclass(frozen=True)
class Outlier:
    """An owned-channel video whose views are >= 5x the channel baseline."""

    video_id: str
    view_count: int
    outlier_factor: float  # view_count / baseline, >= 5.0


@dataclass(frozen=True)
class OutlierResult:
    """Result of outlier detection for a channel."""

    channel_id: str
    insufficient_data: bool
    baseline: BaselineResult | None
    outliers: tuple[Outlier, ...]


# ---------------------------------------------------------------------------
# Concept generation models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TitleConcept:
    """A generated title concept (1-100 chars)."""

    text: str  # 1..100 chars


@dataclass(frozen=True)
class ThumbnailConcept:
    """A generated thumbnail concept with a visual description and overlay."""

    visual_description: str
    text_overlay: str  # <= 30 chars


@dataclass(frozen=True)
class ConceptSet:
    """A set of generated concepts for an idea (>= 3 titles, >= 1 thumbnail)."""

    idea_id: str
    titles: tuple[TitleConcept, ...]  # >= 3 distinct
    thumbnails: tuple[ThumbnailConcept, ...]  # >= 1
    category: ChannelCategory | None


# ---------------------------------------------------------------------------
# Publish-time & script models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishRecommendation:
    """Recommended publish day and contiguous time window."""

    day_of_week: int  # 0..6
    window_start_hour: int  # 0..23
    window_duration_hours: int  # 1..3
    timezone: str  # IANA tz or "UTC"
    confidence: Confidence


@dataclass(frozen=True)
class ScriptBundle:
    """Generated script artifacts for an idea."""

    idea_id: str
    outline: str
    script: str
    seo_tags: tuple[str, ...]  # 5..30, superset of analyzer keywords
    description: str  # 100..5000 chars
    seo_tags_unavailable: bool = False


# ---------------------------------------------------------------------------
# SEO keyword-gap models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordMetric:
    """Demand/competition metrics for a candidate keyword."""

    keyword: str
    demand: float
    competition: float


@dataclass(frozen=True)
class KeywordGap:
    """A keyword classified as a gap (high demand, low competition)."""

    keyword: str
    demand: float
    competition: float


@dataclass(frozen=True)
class KeywordGapResult:
    """Result of SEO keyword-gap analysis."""

    gaps: tuple[KeywordGap, ...]
    no_gap: bool = False
    insufficient_data: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Format recommendation models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatResult:
    """Recommended video format with supporting averages and rationale."""

    idea_id: str
    recommended: VideoFormat | None  # None when withheld
    short_avg: float | None
    long_avg: float | None
    rationale: str | None
    insufficient_performance_data: bool = False


# ---------------------------------------------------------------------------
# Digest & delivery models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DigestSection:
    """One typed section of the digest report (13.1, 13.2)."""

    item_type: str  # scored_ideas | competitor_spikes | outliers
    items: tuple
    no_items: bool


@dataclass(frozen=True)
class DigestReport:
    """A compiled digest report with exactly three distinct typed sections."""

    sections: tuple[DigestSection, DigestSection, DigestSection]  # exactly 3 distinct


@dataclass(frozen=True)
class DeliveryOutcome:
    """Outcome of delivering the digest to a single destination."""

    destination: DeliveryDestination
    status: str  # delivered | delivery-failed
    attempts: int


# ---------------------------------------------------------------------------
# Scheduling & run models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Schedule:
    """A recurrence schedule for automated runs (14.1)."""

    recurrence_interval: str | None  # e.g. "daily", "weekly"
    run_time: str | None  # e.g. "08:00"


@dataclass(frozen=True)
class StepResult:
    """Status of a single step within a run."""

    step: str
    status: StepStatus


@dataclass(frozen=True)
class RunSummary:
    """Summary of a completed (or skipped) scheduled run (14.6)."""

    steps: tuple[StepResult, ...]
    started_at: str
    completed_at: str
    overlap_skipped: bool = False


# ---------------------------------------------------------------------------
# Authorization & configuration models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizedChannel:
    """An owned channel with stored credentials and connection state."""

    channel_id: str
    credentials_ref: str  # reference/handle, not raw secret in logs
    connected: bool
    credentials_expired: bool = False


@dataclass(frozen=True)
class AuthorizationGrant:
    """A Creator's authorization decision for a channel."""

    granted: bool
    credentials_ref: str | None  # present only when granted
    responded_within_seconds: float  # used to evaluate the 300s decision window


@dataclass(frozen=True)
class AuthorizationResult:
    """Result of an authorization attempt for a channel."""

    channel_id: str
    status: AuthStatus
    error: str | None = None


@dataclass(frozen=True)
class Configuration:
    """Persisted configuration for the agent (Requirement 15)."""

    authorized_channels: tuple[AuthorizedChannel, ...]  # up to 50
    selected_category: ChannelCategory | None
    monitored_competitors: tuple[str, ...]  # up to 50
    schedule: Schedule | None
    delivery_destinations: tuple[DeliveryDestination, ...]
