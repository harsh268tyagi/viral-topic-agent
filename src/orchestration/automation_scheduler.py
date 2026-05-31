"""End-to-end automation and scheduling (Requirement 14).

The :class:`AutomationScheduler` is the Orchestration-layer component. It owns
the recurring schedule, prevents overlapping runs, executes the seven pipeline
steps in the mandated order, short-circuits steps whose input depends on a
failed step, and records a :class:`~viral_topic_agent.models.RunSummary` of the
outcome.

Design reference (``.kiro/specs/viral-topic-agent/design.md`` ->
*Automation_Scheduler* and the *End-to-End Scheduled Run Sequence*)::

    class AutomationScheduler:
        STEP_ORDER = [CHANNEL_ANALYSIS, TREND_DISCOVERY, CATEGORY_FILTER,
                      IDEA_SCORING, COMPETITOR_TRACKING, OUTLIER_DETECTION,
                      DIGEST_DELIVERY]
        def set_schedule(self, config, schedule) -> ScheduleResult: ...
        def run(self, config, clock) -> RunSummary: ...

Behavior:

- ``set_schedule`` stores a *valid* schedule -- one that specifies both a
  recurrence interval and a run time -- in the :class:`Configuration` (14.1). A
  schedule that omits either field is rejected without being stored, and the
  result names the missing field(s) (14.2).
- ``run`` executes the steps in the mandated order: channel analysis, trend
  discovery, category filtering, idea scoring, competitor tracking, outlier
  detection, then digest delivery (14.3).
- A trigger that arrives while a previous run is still in progress does not start
  concurrently; it is recorded as skipped in its own run summary (14.4).
- When a step fails, the failure is recorded, each step whose input depends
  (transitively) on the failed step's output is skipped, and every step that
  does *not* depend on the failed step still runs (14.5).
- A completed run emits a :class:`RunSummary` listing every step with a status of
  succeeded / failed / skipped plus the run start and completion times (14.6).
- When no schedule is configured, the workflow runs only on a manual trigger
  (14.7); a scheduled (non-manual) trigger with no configured schedule does not
  execute.

Step dependency map
-------------------
The skip-on-failure rule (14.5) is driven by an explicit directed acyclic graph
mapping each step to the steps it *directly* depends on (i.e. whose output is its
input). It mirrors the data-flow edges in the design's architecture diagram
(``TDE --> CF --> IS``; ``IS, CT, OD --> DS``) plus the two genuine owned-channel
data dependencies (idea scoring needs the channel profile/baseline):

================== ===================================================
Step               Directly depends on
================== ===================================================
channel_analysis   (independent)
trend_discovery    (independent)
category_filter    trend_discovery
idea_scoring       category_filter, channel_analysis
competitor_tracking(independent)
outlier_detection  (independent)
digest_delivery    idea_scoring, competitor_tracking, outlier_detection
================== ===================================================

``STEP_ORDER`` is a topological order of this DAG, so a single forward pass can
both run independent steps and skip the transitive dependents of any failed or
skipped step.

Dependency injection
---------------------
The seven analysis/delivery components are injected at construction so tests can
supply per-step stubs that succeed or fail deterministically. The shared
``ResilientDataSource``, the per-destination deliverers, and an optional
category aggregate (for scoring degradation) are likewise injected.

Requirements traceability: 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Callable, Mapping

from analysis.category_filter import CategoryFilter
from analysis.channel_analyzer import ChannelAnalyzer
from infrastructure.clock import Clock
from analysis.competitor_tracker import CompetitorTracker
from infrastructure.datasource import DataOperation, DataRequest
from delivery.deliverer import Deliverer
from delivery.digest_service import DigestService
from domain.models import (
    ChannelProfile,
    CompetitorReport,
    CompetitorSpike,
    Configuration,
    ContentIdea,
    DeliveryDestination,
    DiscoveryResult,
    FilterResult,
    Outlier,
    OutlierResult,
    RunSummary,
    Schedule,
    ScoredIdea,
    StepResult,
    StepStatus,
    ViralTemplate,
    VideoStats,
)
from analysis.outlier_detector import OutlierDetector
from infrastructure.resilient_data_source import ResilientDataSource
from analysis.scoring import CategoryAggregate, IdeaScorer
from analysis.trend_discovery import TrendDiscoveryEngine

__all__ = [
    "AutomationScheduler",
    "ScheduleResult",
    "CHANNEL_ANALYSIS",
    "TREND_DISCOVERY",
    "CATEGORY_FILTER",
    "IDEA_SCORING",
    "COMPETITOR_TRACKING",
    "OUTLIER_DETECTION",
    "DIGEST_DELIVERY",
    "STEP_ORDER",
    "STEP_DEPENDENCIES",
    "FIELD_RECURRENCE_INTERVAL",
    "FIELD_RUN_TIME",
]


# ---------------------------------------------------------------------------
# Step identifiers, order, and dependency graph
# ---------------------------------------------------------------------------

CHANNEL_ANALYSIS = "channel_analysis"
TREND_DISCOVERY = "trend_discovery"
CATEGORY_FILTER = "category_filter"
IDEA_SCORING = "idea_scoring"
COMPETITOR_TRACKING = "competitor_tracking"
OUTLIER_DETECTION = "outlier_detection"
DIGEST_DELIVERY = "digest_delivery"

#: The mandated execution order (14.3). Also a valid topological order of
#: :data:`STEP_DEPENDENCIES`, so dependencies are always processed before the
#: steps that depend on them.
STEP_ORDER: tuple[str, ...] = (
    CHANNEL_ANALYSIS,
    TREND_DISCOVERY,
    CATEGORY_FILTER,
    IDEA_SCORING,
    COMPETITOR_TRACKING,
    OUTLIER_DETECTION,
    DIGEST_DELIVERY,
)

#: Each step mapped to the steps whose output is its direct input (14.5). A step
#: is skipped when any of its (transitive) dependencies failed or were skipped.
STEP_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    CHANNEL_ANALYSIS: (),
    TREND_DISCOVERY: (),
    CATEGORY_FILTER: (TREND_DISCOVERY,),
    IDEA_SCORING: (CATEGORY_FILTER, CHANNEL_ANALYSIS),
    COMPETITOR_TRACKING: (),
    OUTLIER_DETECTION: (),
    DIGEST_DELIVERY: (IDEA_SCORING, COMPETITOR_TRACKING, OUTLIER_DETECTION),
}

# Schedule field names used when reporting which required field is missing (14.2).
FIELD_RECURRENCE_INTERVAL = "recurrence_interval"
FIELD_RUN_TIME = "run_time"


# ---------------------------------------------------------------------------
# Result of set_schedule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScheduleResult:
    """Outcome of :meth:`AutomationScheduler.set_schedule`.

    ``config`` is the resulting configuration: a new configuration carrying the
    stored schedule when ``stored`` is ``True`` (14.1), or the *unchanged* input
    configuration when the schedule was rejected (14.2). ``missing_fields`` names
    every required schedule field that was omitted, and ``error`` is a
    human-readable indication naming those fields. Both are empty/``None`` on
    success.
    """

    config: Configuration
    stored: bool
    missing_fields: tuple[str, ...] = ()
    error: str | None = None


# ---------------------------------------------------------------------------
# Internal run context
# ---------------------------------------------------------------------------


@dataclass
class _RunContext:
    """Mutable state threaded through a single run's steps.

    Each step reads the outputs it depends on and writes its own output here for
    downstream steps. It is intentionally *not* frozen -- it is private,
    short-lived per-run scratch space, never a persisted domain model.
    """

    config: Configuration
    owned_channel_id: str
    profile: ChannelProfile | None = None
    discovery: DiscoveryResult | None = None
    ideas: list[ContentIdea] = field(default_factory=list)
    templates: list[ViralTemplate] = field(default_factory=list)
    filtered: FilterResult | None = None
    scored: list[ScoredIdea] = field(default_factory=list)
    competitor_reports: list[CompetitorReport] = field(default_factory=list)
    spikes: list[CompetitorSpike] = field(default_factory=list)
    outlier_result: OutlierResult | None = None
    outliers: list[Outlier] = field(default_factory=list)


class AutomationScheduler:
    """Runs the end-to-end workflow on a schedule (Requirement 14)."""

    #: Exposed for callers/tests that want the mandated order without importing
    #: the module-level constant.
    STEP_ORDER: tuple[str, ...] = STEP_ORDER

    def __init__(
        self,
        channel_analyzer: ChannelAnalyzer | None = None,
        trend_engine: TrendDiscoveryEngine | None = None,
        category_filter: CategoryFilter | None = None,
        idea_scorer: IdeaScorer | None = None,
        competitor_tracker: CompetitorTracker | None = None,
        outlier_detector: OutlierDetector | None = None,
        digest_service: DigestService | None = None,
        *,
        source: ResilientDataSource | None = None,
        deliverers: Mapping[DeliveryDestination, Deliverer] | None = None,
        category_aggregate: CategoryAggregate | None = None,
    ) -> None:
        # The seven pipeline components (14.3 wiring). Each defaults to the real
        # implementation, but tests inject stubs that succeed/fail per step.
        self._channel_analyzer = channel_analyzer or ChannelAnalyzer()
        self._trend_engine = trend_engine or TrendDiscoveryEngine()
        self._category_filter = category_filter or CategoryFilter()
        self._idea_scorer = idea_scorer or IdeaScorer()
        self._competitor_tracker = competitor_tracker or CompetitorTracker()
        self._outlier_detector = outlier_detector or OutlierDetector()
        self._digest_service = digest_service or DigestService()

        # Shared infrastructure used by the data-retrieving steps.
        self._source = source
        self._deliverers: dict[DeliveryDestination, Deliverer] = dict(deliverers or {})
        self._category_aggregate = category_aggregate

        # Re-entrancy guard backing the overlap rule (14.4). A run sets this
        # while executing; a trigger that arrives meanwhile is recorded skipped.
        self._in_progress = False

        # Step -> handler dispatch table, resolved once.
        self._handlers: dict[str, Callable[[_RunContext], StepStatus]] = {
            CHANNEL_ANALYSIS: self._step_channel_analysis,
            TREND_DISCOVERY: self._step_trend_discovery,
            CATEGORY_FILTER: self._step_category_filter,
            IDEA_SCORING: self._step_idea_scoring,
            COMPETITOR_TRACKING: self._step_competitor_tracking,
            OUTLIER_DETECTION: self._step_outlier_detection,
            DIGEST_DELIVERY: self._step_digest_delivery,
        }

    # ------------------------------------------------------------------
    # set_schedule (14.1, 14.2)
    # ------------------------------------------------------------------

    def set_schedule(self, config: Configuration, schedule: Schedule) -> ScheduleResult:
        """Validate and (if valid) store ``schedule`` in ``config``.

        A schedule is valid only when it specifies *both* a recurrence interval
        and a run time (14.1). When either is omitted -- ``None`` or blank -- the
        schedule is rejected, ``config`` is returned unchanged, and the result
        names the missing field(s) (14.2). ``config`` is never mutated; a new
        configuration is returned when the schedule is stored.
        """
        missing: list[str] = []
        if self._is_blank(schedule.recurrence_interval):
            missing.append(FIELD_RECURRENCE_INTERVAL)
        if self._is_blank(schedule.run_time):
            missing.append(FIELD_RUN_TIME)

        if missing:
            # 14.2: reject without storing; name the missing field(s).
            return ScheduleResult(
                config=config,
                stored=False,
                missing_fields=tuple(missing),
                error=(
                    "invalid-schedule: missing required schedule "
                    f"field(s): {', '.join(missing)}"
                ),
            )

        # 14.1: both fields present -> store the schedule in the configuration.
        updated = replace(config, schedule=schedule)
        return ScheduleResult(config=updated, stored=True)

    @staticmethod
    def _is_blank(value: str | None) -> bool:
        """A schedule field is "missing" when it is ``None`` or only whitespace."""
        return value is None or not str(value).strip()

    # ------------------------------------------------------------------
    # run (14.3, 14.4, 14.5, 14.6, 14.7)
    # ------------------------------------------------------------------

    def run(
        self, config: Configuration, clock: Clock, *, manual: bool = False
    ) -> RunSummary:
        """Execute one workflow run, returning its :class:`RunSummary`.

        Args:
            config: The configuration to run against (its schedule, selected
                category, authorized channels, competitors, and destinations).
            clock: The injected clock; its ``monotonic`` reading supplies the run
                start and completion timestamps (14.6).
            manual: ``True`` when the Creator manually triggered the run. A run
                executes when it is manual *or* a schedule is configured;
                otherwise (scheduled trigger, no schedule) it does not execute
                (14.7).

        Returns:
            A :class:`RunSummary`. For an executed run it lists all seven steps
            in :data:`STEP_ORDER`, each with its status, plus the start/end
            times (14.3, 14.5, 14.6). An overlapping trigger yields a summary
            with every step skipped and ``overlap_skipped=True`` (14.4). A
            scheduled trigger with no configured schedule yields an empty-step
            summary (nothing executed) (14.7).
        """
        # 14.4: a trigger arriving while a run is in progress does not start a
        # concurrent run; it is recorded as skipped in its own summary.
        if self._in_progress:
            return self._overlap_summary(clock)

        # 14.7: with no schedule configured, only a manual trigger runs.
        if config.schedule is None and not manual:
            now = self._timestamp(clock)
            return RunSummary(steps=(), started_at=now, completed_at=now)

        self._in_progress = True
        try:
            return self._execute_run(config, clock)
        finally:
            # Always clear the guard so a later trigger can run (even if a step
            # raised an unexpected error that escaped the per-step guard).
            self._in_progress = False

    def _execute_run(self, config: Configuration, clock: Clock) -> RunSummary:
        """Run every step in order, applying the failure/skip rule (14.3, 14.5)."""
        started_at = self._timestamp(clock)
        ctx = _RunContext(
            config=config,
            owned_channel_id=self._resolve_owned_channel_id(config),
        )

        statuses: dict[str, StepStatus] = {}
        # Steps that did not succeed (failed or skipped). A step is skipped when
        # any of its dependencies is in this set; because STEP_ORDER is a
        # topological order, dependencies are always resolved first, so this
        # single pass skips transitive dependents correctly.
        not_succeeded: set[str] = set()

        for step in STEP_ORDER:
            deps = STEP_DEPENDENCIES[step]
            if any(dep in not_succeeded for dep in deps):
                statuses[step] = StepStatus.SKIPPED
                not_succeeded.add(step)
                continue

            status = self._execute_step(step, ctx)
            statuses[step] = status
            if status is not StepStatus.SUCCEEDED:
                not_succeeded.add(step)

        completed_at = self._timestamp(clock)

        # 14.6: list each step (in the mandated order) with its status.
        steps = tuple(
            StepResult(step=step, status=statuses[step]) for step in STEP_ORDER
        )
        return RunSummary(
            steps=steps,
            started_at=started_at,
            completed_at=completed_at,
            overlap_skipped=False,
        )

    def _execute_step(self, step: str, ctx: _RunContext) -> StepStatus:
        """Run one step's handler, classifying any exception as a failure (14.5).

        A handler returns :data:`StepStatus.SUCCEEDED` or
        :data:`StepStatus.FAILED`. Any exception escaping the underlying
        component is contained here and recorded as a failed step, so one step's
        failure never aborts the run -- independent steps still execute.
        """
        handler = self._handlers[step]
        try:
            return handler(ctx)
        except Exception:  # noqa: BLE001 - any component error is a step failure
            return StepStatus.FAILED

    # ------------------------------------------------------------------
    # Individual step handlers (14.3 wiring)
    # ------------------------------------------------------------------

    def _step_channel_analysis(self, ctx: _RunContext) -> StepStatus:
        """Analyze the owned channel into a profile; an ``Err`` is a failure."""
        result = self._channel_analyzer.analyze(ctx.owned_channel_id, self._source)
        if result.is_err():
            return StepStatus.FAILED
        ctx.profile = result.unwrap()
        return StepStatus.SUCCEEDED

    def _step_trend_discovery(self, ctx: _RunContext) -> StepStatus:
        """Discover ideas across the time windows and collect them for filtering."""
        discovery = self._trend_engine.discover(self._source)
        ctx.discovery = discovery
        ctx.ideas = self._flatten_ideas(discovery)
        ctx.templates = self._collect_templates(ctx.ideas)
        return StepStatus.SUCCEEDED

    def _step_category_filter(self, ctx: _RunContext) -> StepStatus:
        """Filter the discovered ideas/templates by the applied category."""
        detected = ctx.profile.detected_category if ctx.profile is not None else None
        ctx.filtered = self._category_filter.filter(
            ctx.ideas,
            ctx.templates,
            ctx.config.selected_category,
            detected,
        )
        return StepStatus.SUCCEEDED

    def _step_idea_scoring(self, ctx: _RunContext) -> StepStatus:
        """Score the filtered ideas against the channel profile."""
        # Reachable only when channel_analysis and category_filter both
        # succeeded (per STEP_DEPENDENCIES), so both are available.
        assert ctx.profile is not None
        assert ctx.filtered is not None
        ctx.scored = self._idea_scorer.score(
            list(ctx.filtered.ideas), ctx.profile, self._category_aggregate
        )
        return StepStatus.SUCCEEDED

    def _step_competitor_tracking(self, ctx: _RunContext) -> StepStatus:
        """Monitor competitors and collect any flagged spikes."""
        reports = self._competitor_tracker.monitor(ctx.config, self._source)
        ctx.competitor_reports = list(reports)
        spikes: list[CompetitorSpike] = []
        for report in ctx.competitor_reports:
            spikes.extend(report.spikes)
        ctx.spikes = spikes
        return StepStatus.SUCCEEDED

    def _step_outlier_detection(self, ctx: _RunContext) -> StepStatus:
        """Detect outliers among the owned channel's retrieved videos."""
        videos = self._retrieve_owned_videos(ctx.owned_channel_id)
        ctx.outlier_result = self._outlier_detector.detect(
            ctx.owned_channel_id, videos
        )
        ctx.outliers = list(ctx.outlier_result.outliers)
        return StepStatus.SUCCEEDED

    def _step_digest_delivery(self, ctx: _RunContext) -> StepStatus:
        """Compile the digest and deliver it to the configured destinations."""
        report = self._digest_service.compile(ctx.scored, ctx.spikes, ctx.outliers)
        self._digest_service.deliver(report, ctx.config, self._deliverers)
        return StepStatus.SUCCEEDED

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    def _retrieve_owned_videos(self, channel_id: str) -> list[VideoStats]:
        """Fetch the owned channel's videos for outlier detection.

        A retrieval failure (or absent source) yields an empty list, which the
        :class:`OutlierDetector` treats as insufficient data rather than a step
        failure -- consistent with the system's graceful-degradation posture.
        """
        if self._source is None:
            return []
        result = self._source.call(
            DataRequest(
                operation=DataOperation.VIDEOS,
                target=channel_id,
                params={"channel_id": channel_id},
            )
        )
        if result.is_err():
            return []
        return list(result.unwrap())

    @staticmethod
    def _flatten_ideas(discovery: DiscoveryResult) -> list[ContentIdea]:
        """Flatten the per-window discovered ideas into a single list."""
        ideas: list[ContentIdea] = []
        for window_ideas in discovery.ideas_by_window.values():
            ideas.extend(window_ideas)
        return ideas

    @staticmethod
    def _collect_templates(ideas: list[ContentIdea]) -> list[ViralTemplate]:
        """Collect the distinct templates referenced by ``ideas`` (stable order)."""
        seen: set[str] = set()
        templates: list[ViralTemplate] = []
        for idea in ideas:
            for template in idea.templates:
                if template.template_id not in seen:
                    seen.add(template.template_id)
                    templates.append(template)
        return templates

    @staticmethod
    def _resolve_owned_channel_id(config: Configuration) -> str:
        """Pick the owned channel to analyze: the first connected one, else any.

        Returns an empty string when no channel is authorized; the injected
        components decide how to treat that (real ones return a retrieval error,
        which the run records as a failed channel-analysis step).
        """
        for channel in config.authorized_channels:
            if channel.connected:
                return channel.channel_id
        if config.authorized_channels:
            return config.authorized_channels[0].channel_id
        return ""

    # ------------------------------------------------------------------
    # Summary helpers
    # ------------------------------------------------------------------

    def _overlap_summary(self, clock: Clock) -> RunSummary:
        """Build the summary for an overlapping (not-started) trigger (14.4)."""
        now = self._timestamp(clock)
        steps = tuple(
            StepResult(step=step, status=StepStatus.SKIPPED) for step in STEP_ORDER
        )
        return RunSummary(
            steps=steps,
            started_at=now,
            completed_at=now,
            overlap_skipped=True,
        )

    @staticmethod
    def _timestamp(clock: Clock) -> str:
        """Render the clock's current monotonic reading as a timestamp string.

        The injected :class:`Clock` exposes monotonic time (the only time source
        the system abstracts), so run start/completion are recorded from it
        (14.6). Monotonic is non-decreasing, so a completion read after a start
        read is always >= that start.
        """
        return f"{clock.monotonic():.6f}"
