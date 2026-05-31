"""End-to-end happy-path integration test (task 20.2).

Exercises a single scheduled run through the full mandated step order
(channel analysis -> trend discovery -> category filtering -> idea scoring ->
competitor tracking -> outlier detection -> digest delivery), wiring the *real*
pipeline components into the :class:`AutomationScheduler` rather than per-step
stubs.

Wiring:

- The seven pipeline components are the genuine implementations (the scheduler's
  defaults: :class:`ChannelAnalyzer`, :class:`TrendDiscoveryEngine`,
  :class:`CategoryFilter`, :class:`IdeaScorer`, :class:`CompetitorTracker`,
  :class:`OutlierDetector`, :class:`DigestService`).
- All ``Data_Source`` access goes through a real
  :class:`~viral_topic_agent.resilient_data_source.ResilientDataSource` over a
  :class:`~viral_topic_agent.clock.FakeClock`, backed by the fast in-memory
  :class:`~tests.integration_support.StubDataSource`.
- Delivery uses real :class:`~viral_topic_agent.delivery.InMemoryDeliverer`
  stubs (email + Slack), so "the digest was delivered" is observable via the
  deliverers' recorded reports.

Assertions: the :class:`RunSummary` lists all seven steps in the mandated order,
each ``SUCCEEDED`` and not overlap-skipped, and the digest report reached every
configured destination.

Requirement: 14.3.
"""

from __future__ import annotations

from viral_topic_agent.orchestration.automation_scheduler import STEP_ORDER, AutomationScheduler
from viral_topic_agent.infrastructure.clock import FakeClock
from viral_topic_agent.delivery import EmailDeliverer, SlackDeliverer
from viral_topic_agent.domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    Configuration,
    DeliveryDestination,
    Schedule,
    StepStatus,
)
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy

from .integration_support import StubDataSource
from .scheduler_support import TickingClock


def _connected_config() -> Configuration:
    """A configuration that drives a full, successful run.

    One connected owned channel (so channel analysis has a target), two
    monitored competitors (so competitor tracking produces spikes from the
    stub's 10x video), a valid schedule, and two delivery destinations.
    """
    return Configuration(
        authorized_channels=(
            AuthorizedChannel(
                channel_id="chan-1",
                credentials_ref="cred-ref",
                connected=True,
                credentials_expired=False,
            ),
        ),
        selected_category=None,  # fall back to the profile's detected category
        monitored_competitors=("comp-1", "comp-2"),
        schedule=Schedule(recurrence_interval="daily", run_time="08:00"),
        delivery_destinations=(DeliveryDestination.EMAIL, DeliveryDestination.SLACK),
    )


def test_scheduled_run_completes_full_pipeline_and_delivers_digest():
    """A single scheduled run executes all seven steps and delivers the digest (14.3)."""
    # Stub data source wrapped in the real resilience layer over a FakeClock.
    source = ResilientDataSource(
        StubDataSource(category=ChannelCategory.GAMING), RetryPolicy(), FakeClock()
    )

    # Real per-destination deliverers so delivery is observable end-to-end.
    email = EmailDeliverer()
    slack = SlackDeliverer()
    deliverers = {
        DeliveryDestination.EMAIL: email,
        DeliveryDestination.SLACK: slack,
    }

    # Real pipeline components (the scheduler's defaults), wired to the shared
    # source and the deliverers.
    scheduler = AutomationScheduler(source=source, deliverers=deliverers)
    config = _connected_config()

    summary = scheduler.run(config, TickingClock(), manual=False)

    # 14.3: every step ran, in the mandated order, and succeeded.
    assert [s.step for s in summary.steps] == list(STEP_ORDER)
    assert all(s.status == StepStatus.SUCCEEDED for s in summary.steps), summary.steps
    assert summary.overlap_skipped is False
    # The run is time-consistent (TickingClock advances on each read).
    assert float(summary.started_at) <= float(summary.completed_at)

    # The digest was delivered to every configured destination.
    assert email.delivered is True
    assert slack.delivered is True
    assert len(email.delivered_reports) == 1
    assert len(slack.delivered_reports) == 1

    # The delivered report carries the three distinct typed sections, and the
    # stub's 10x video surfaced as both a competitor spike and an outlier.
    report = email.delivered_reports[0]
    sections = {section.item_type: section for section in report.sections}
    assert set(sections) == {"scored_ideas", "competitor_spikes", "outliers"}
    assert sections["competitor_spikes"].no_items is False
    assert sections["outliers"].no_items is False
