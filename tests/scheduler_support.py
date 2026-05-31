"""Shared test doubles and builders for the Automation_Scheduler tests.

This module is *not* a test module (its name does not match ``test_*``), so it
is never collected by pytest. It provides:

- per-step component stubs that record their invocation order and can be told to
  fail (by raising) for a given step, and a channel-analysis stub that can fail
  by returning an ``Err`` (the analyzer's documented failure mode);
- a :func:`make_scheduler` factory that wires the seven stubs into a real
  :class:`~viral_topic_agent.automation_scheduler.AutomationScheduler`;
- minimal valid-model builders and a :class:`TickingClock` whose ``monotonic``
  advances on every read (so run start/completion timestamps are strictly
  ordered).

These doubles let the scheduler property/unit tests exercise step ordering
(14.3), failure/skip propagation (14.5), and run-summary completeness (14.6)
without touching real data sources, generation, or delivery.
"""

from __future__ import annotations

from typing import Iterable

from orchestration.automation_scheduler import (
    CATEGORY_FILTER,
    CHANNEL_ANALYSIS,
    COMPETITOR_TRACKING,
    DIGEST_DELIVERY,
    IDEA_SCORING,
    OUTLIER_DETECTION,
    TREND_DISCOVERY,
    AutomationScheduler,
)
from analysis.channel_analyzer import DataRetrievalError
from domain.models import (
    BaselineResult,
    ChannelCategory,
    ChannelProfile,
    Configuration,
    Confidence,
    DigestReport,
    DigestSection,
    DiscoveryResult,
    FilterResult,
    OutlierResult,
    Schedule,
)
from infrastructure.result import Err, Ok


# ---------------------------------------------------------------------------
# A clock whose monotonic reading advances on every read
# ---------------------------------------------------------------------------


class TickingClock:
    """A :class:`~viral_topic_agent.clock.Clock` whose time strictly increases.

    Each :meth:`monotonic` call returns a value ``step`` greater than the last,
    so a completion timestamp read after a start timestamp is always strictly
    greater. :meth:`sleep` advances time by the requested amount. This lets the
    run-summary property assert ``started_at <= completed_at`` non-trivially.
    """

    def __init__(self, start: float = 0.0, step: float = 1.0) -> None:
        self._now = float(start)
        self._step = float(step)

    def monotonic(self) -> float:
        value = self._now
        self._now += self._step
        return value

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep() seconds must be non-negative")
        self._now += float(seconds)


# ---------------------------------------------------------------------------
# Minimal valid-model builders
# ---------------------------------------------------------------------------


def make_profile(channel_id: str = "chan-1") -> ChannelProfile:
    return ChannelProfile(
        channel_id=channel_id,
        detected_category=ChannelCategory.GAMING,
        subscriber_count=1000,
        video_count=10,
        baseline=BaselineResult(value=500.0, confidence=Confidence.NORMAL, sample_size=10),
    )


def make_discovery() -> DiscoveryResult:
    return DiscoveryResult(ideas_by_window={}, window_errors={})


def make_filter_result() -> FilterResult:
    return FilterResult(ideas=(), templates=())


def make_outlier_result(channel_id: str = "chan-1") -> OutlierResult:
    return OutlierResult(
        channel_id=channel_id,
        insufficient_data=True,
        baseline=None,
        outliers=(),
    )


def make_report() -> DigestReport:
    empty = lambda item_type: DigestSection(item_type=item_type, items=(), no_items=True)
    return DigestReport(
        sections=(
            empty("scored_ideas"),
            empty("competitor_spikes"),
            empty("outliers"),
        )
    )


def make_config(*, with_schedule: bool = True) -> Configuration:
    """A configuration with (by default) a valid schedule so ``run`` executes."""
    schedule = Schedule(recurrence_interval="daily", run_time="08:00") if with_schedule else None
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=schedule,
        delivery_destinations=(),
    )


# ---------------------------------------------------------------------------
# Per-step component stubs
# ---------------------------------------------------------------------------


class _StepStub:
    """Base for the recording stubs: tracks invocation order and failure mode."""

    def __init__(self, recorder: list[str], *, fail: bool = False) -> None:
        self._recorder = recorder
        self._fail = fail

    def _enter(self, step: str) -> None:
        self._recorder.append(step)


class StubChannelAnalyzer(_StepStub):
    """Stub analyzer. Fails by returning ``Err`` (its documented failure mode)."""

    def analyze(self, channel_id, source):
        self._enter(CHANNEL_ANALYSIS)
        if self._fail:
            return Err(DataRetrievalError(channel_id=channel_id, reason="stub failure"))
        return Ok(make_profile(channel_id or "chan-1"))


class StubTrendEngine(_StepStub):
    def discover(self, source, windows=None):
        self._enter(TREND_DISCOVERY)
        if self._fail:
            raise RuntimeError("trend discovery failed")
        return make_discovery()


class StubCategoryFilter(_StepStub):
    def filter(self, ideas, templates, selected, detected):
        self._enter(CATEGORY_FILTER)
        if self._fail:
            raise RuntimeError("category filter failed")
        return make_filter_result()


class StubIdeaScorer(_StepStub):
    def score(self, ideas, profile, category_aggregate):
        self._enter(IDEA_SCORING)
        if self._fail:
            raise RuntimeError("idea scoring failed")
        return []


class StubCompetitorTracker(_StepStub):
    def monitor(self, config, source):
        self._enter(COMPETITOR_TRACKING)
        if self._fail:
            raise RuntimeError("competitor tracking failed")
        return []


class StubOutlierDetector(_StepStub):
    def detect(self, channel_id, videos):
        self._enter(OUTLIER_DETECTION)
        if self._fail:
            raise RuntimeError("outlier detection failed")
        return make_outlier_result(channel_id or "chan-1")


class StubDigestService(_StepStub):
    """Stub digest service. The digest-delivery step is recorded on ``compile``."""

    def __init__(self, recorder: list[str], *, fail: bool = False) -> None:
        super().__init__(recorder, fail=fail)
        self.delivered = False

    def compile(self, scored, spikes, outliers):
        self._enter(DIGEST_DELIVERY)
        if self._fail:
            raise RuntimeError("digest compile failed")
        return make_report()

    def deliver(self, report, config, deliverers):
        self.delivered = True
        return None


# ---------------------------------------------------------------------------
# Scheduler factory
# ---------------------------------------------------------------------------


def make_scheduler(
    recorder: list[str],
    fail_steps: Iterable[str] = (),
) -> AutomationScheduler:
    """Build an :class:`AutomationScheduler` wired with recording stubs.

    Every stub appends its step id to ``recorder`` when its handler runs. A stub
    whose step is in ``fail_steps`` fails (channel analysis via ``Err``, the rest
    by raising), so the scheduler records that step as failed and applies the
    dependency-skip rule (14.5).
    """
    failing = set(fail_steps)
    return AutomationScheduler(
        channel_analyzer=StubChannelAnalyzer(recorder, fail=CHANNEL_ANALYSIS in failing),
        trend_engine=StubTrendEngine(recorder, fail=TREND_DISCOVERY in failing),
        category_filter=StubCategoryFilter(recorder, fail=CATEGORY_FILTER in failing),
        idea_scorer=StubIdeaScorer(recorder, fail=IDEA_SCORING in failing),
        competitor_tracker=StubCompetitorTracker(recorder, fail=COMPETITOR_TRACKING in failing),
        outlier_detector=StubOutlierDetector(recorder, fail=OUTLIER_DETECTION in failing),
        digest_service=StubDigestService(recorder, fail=DIGEST_DELIVERY in failing),
    )
