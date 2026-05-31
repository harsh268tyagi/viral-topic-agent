"""Example/edge-case tests for the ``DigestService`` (task 18.2).

Covers report compilation (exactly three typed sections + ``no-items``
indicators, 13.1/13.2) and delivery (per-destination, independent, bounded
retry, plus the no-destination-configured status, 13.3/13.4/13.6/13.7).

The Hypothesis property tests for sections (Property 23) and delivery
(Property 24) are separate tasks (18.3/18.4); this module exercises concrete
examples and edge cases.
"""

import pytest

from delivery import InMemoryDeliverer
from delivery.digest_service import (
    MAX_DELIVERY_ATTEMPTS,
    SECTION_COMPETITOR_SPIKES,
    SECTION_OUTLIERS,
    SECTION_SCORED_IDEAS,
    STATUS_DELIVERED,
    STATUS_DELIVERY_FAILED,
    DeliveryResult,
    DigestService,
)
from domain.models import (
    ChannelCategory,
    CompetitorSpike,
    Confidence,
    Configuration,
    ContentIdea,
    DeliveryDestination,
    DigestReport,
    Outlier,
    ScoredIdea,
    TimeWindow,
    ViralTemplate,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _template() -> ViralTemplate:
    return ViralTemplate(
        template_id="t1",
        name="tier-list",
        category=ChannelCategory.GAMING,
        observed_performance=1000.0,
    )


def _scored_idea(idea_id: str = "i1", score: int | None = 80) -> ScoredIdea:
    idea = ContentIdea(
        idea_id=idea_id,
        title_concept="Best moments",
        rationale="trending this week",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(_template(),),
        observed_metric_value=5000.0,
    )
    return ScoredIdea(idea=idea, score=score, confidence=Confidence.NORMAL)


def _spike(video_id: str = "v1") -> CompetitorSpike:
    return CompetitorSpike(
        channel_id="comp1", video_id=video_id, view_count=9000, spike_factor=3.5
    )


def _outlier(video_id: str = "o1") -> Outlier:
    return Outlier(video_id=video_id, view_count=50000, outlier_factor=6.0)


def _config(*destinations: DeliveryDestination) -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=tuple(destinations),
    )


def _report() -> DigestReport:
    return DigestService().compile([_scored_idea()], [_spike()], [_outlier()])


# ---------------------------------------------------------------------------
# compile: three distinct typed sections (13.1)
# ---------------------------------------------------------------------------


def test_compile_produces_exactly_three_sections():
    report = DigestService().compile([_scored_idea()], [_spike()], [_outlier()])
    assert len(report.sections) == 3


def test_compile_sections_are_distinct_and_ordered_by_item_type():
    report = DigestService().compile([_scored_idea()], [_spike()], [_outlier()])
    item_types = [section.item_type for section in report.sections]

    assert item_types == [
        SECTION_SCORED_IDEAS,
        SECTION_COMPETITOR_SPIKES,
        SECTION_OUTLIERS,
    ]
    # Distinct (13.1): no duplicated section type.
    assert len(set(item_types)) == 3


def test_compile_places_items_in_their_typed_section():
    scored = [_scored_idea("a"), _scored_idea("b")]
    spikes = [_spike("v1")]
    outliers = [_outlier("o1"), _outlier("o2"), _outlier("o3")]

    report = DigestService().compile(scored, spikes, outliers)
    by_type = {section.item_type: section for section in report.sections}

    assert by_type[SECTION_SCORED_IDEAS].items == tuple(scored)
    assert by_type[SECTION_COMPETITOR_SPIKES].items == tuple(spikes)
    assert by_type[SECTION_OUTLIERS].items == tuple(outliers)


# ---------------------------------------------------------------------------
# compile: no-items indicators (13.2)
# ---------------------------------------------------------------------------


def test_compile_flags_no_items_only_for_empty_sections():
    report = DigestService().compile([_scored_idea()], [], [_outlier()])
    by_type = {section.item_type: section for section in report.sections}

    assert by_type[SECTION_SCORED_IDEAS].no_items is False
    assert by_type[SECTION_COMPETITOR_SPIKES].no_items is True
    assert by_type[SECTION_OUTLIERS].no_items is False


def test_compile_all_empty_flags_every_section_no_items():
    report = DigestService().compile([], [], [])
    assert all(section.no_items for section in report.sections)
    assert all(section.items == () for section in report.sections)


def test_compile_no_section_flagged_no_items_when_all_populated():
    report = DigestService().compile([_scored_idea()], [_spike()], [_outlier()])
    assert not any(section.no_items for section in report.sections)


# ---------------------------------------------------------------------------
# deliver: no destination configured (13.4)
# ---------------------------------------------------------------------------


def test_deliver_with_no_destination_records_no_destination_configured():
    result = DigestService().deliver(_report(), _config(), {})

    assert isinstance(result, DeliveryResult)
    assert result.no_destination_configured is True
    assert result.outcomes == ()


def test_deliver_with_no_destination_makes_no_attempt():
    # Even if deliverers are available, none should be invoked.
    email = InMemoryDeliverer(DeliveryDestination.EMAIL)
    result = DigestService().deliver(
        _report(), _config(), {DeliveryDestination.EMAIL: email}
    )

    assert result.no_destination_configured is True
    assert email.attempts == 0


# ---------------------------------------------------------------------------
# deliver: happy path (13.3)
# ---------------------------------------------------------------------------


def test_deliver_to_single_destination_succeeds_on_first_attempt():
    report = _report()
    email = InMemoryDeliverer(DeliveryDestination.EMAIL)

    result = DigestService().deliver(
        report, _config(DeliveryDestination.EMAIL), {DeliveryDestination.EMAIL: email}
    )

    assert result.no_destination_configured is False
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.destination is DeliveryDestination.EMAIL
    assert outcome.status == STATUS_DELIVERED
    assert outcome.attempts == 1
    assert email.delivered_reports == [report]


def test_deliver_to_all_three_destinations_succeeds():
    report = _report()
    deliverers = {
        DeliveryDestination.EMAIL: InMemoryDeliverer(DeliveryDestination.EMAIL),
        DeliveryDestination.SLACK: InMemoryDeliverer(DeliveryDestination.SLACK),
        DeliveryDestination.NOTION: InMemoryDeliverer(DeliveryDestination.NOTION),
    }
    config = _config(
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    )

    result = DigestService().deliver(report, config, deliverers)

    assert len(result.outcomes) == 3
    assert all(o.status == STATUS_DELIVERED for o in result.outcomes)
    assert all(o.attempts == 1 for o in result.outcomes)
    # Outcomes preserve configuration order.
    assert [o.destination for o in result.outcomes] == [
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    ]


# ---------------------------------------------------------------------------
# deliver: bounded retry (13.6)
# ---------------------------------------------------------------------------


def test_deliver_retries_then_succeeds_within_attempt_bound():
    # Fails attempts 1 and 2, succeeds on attempt 3 (still within the bound).
    email = InMemoryDeliverer(DeliveryDestination.EMAIL, fail_first=2)

    result = DigestService().deliver(
        _report(), _config(DeliveryDestination.EMAIL), {DeliveryDestination.EMAIL: email}
    )

    outcome = result.outcomes[0]
    assert outcome.status == STATUS_DELIVERED
    assert outcome.attempts == 3
    assert email.attempts == 3


def test_deliver_records_delivery_failed_after_exhausting_attempts():
    email = InMemoryDeliverer(DeliveryDestination.EMAIL, always_fail=True)

    result = DigestService().deliver(
        _report(), _config(DeliveryDestination.EMAIL), {DeliveryDestination.EMAIL: email}
    )

    outcome = result.outcomes[0]
    assert outcome.status == STATUS_DELIVERY_FAILED
    assert outcome.attempts == MAX_DELIVERY_ATTEMPTS == 3
    # Never retries beyond the bounded maximum.
    assert email.attempts == MAX_DELIVERY_ATTEMPTS


def test_deliver_does_not_retry_a_destination_that_succeeds_immediately():
    email = InMemoryDeliverer(DeliveryDestination.EMAIL)

    DigestService().deliver(
        _report(), _config(DeliveryDestination.EMAIL), {DeliveryDestination.EMAIL: email}
    )

    assert email.attempts == 1


# ---------------------------------------------------------------------------
# deliver: independence across destinations (13.7)
# ---------------------------------------------------------------------------


def test_deliver_failure_at_one_destination_does_not_block_others():
    report = _report()
    email = InMemoryDeliverer(DeliveryDestination.EMAIL, always_fail=True)
    slack = InMemoryDeliverer(DeliveryDestination.SLACK)
    notion = InMemoryDeliverer(DeliveryDestination.NOTION)
    deliverers = {
        DeliveryDestination.EMAIL: email,
        DeliveryDestination.SLACK: slack,
        DeliveryDestination.NOTION: notion,
    }
    config = _config(
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    )

    result = DigestService().deliver(report, config, deliverers)
    by_dest = {o.destination: o for o in result.outcomes}

    # Email exhausted all attempts and failed...
    assert by_dest[DeliveryDestination.EMAIL].status == STATUS_DELIVERY_FAILED
    assert by_dest[DeliveryDestination.EMAIL].attempts == MAX_DELIVERY_ATTEMPTS
    # ...but Slack and Notion still got delivered independently.
    assert by_dest[DeliveryDestination.SLACK].status == STATUS_DELIVERED
    assert by_dest[DeliveryDestination.NOTION].status == STATUS_DELIVERED
    assert slack.delivered_reports == [report]
    assert notion.delivered_reports == [report]


def test_deliver_each_destination_gets_independent_attempt_budget():
    # Both fail twice then succeed; each must get its own 3-attempt budget.
    email = InMemoryDeliverer(DeliveryDestination.EMAIL, fail_first=2)
    slack = InMemoryDeliverer(DeliveryDestination.SLACK, fail_first=2)
    deliverers = {
        DeliveryDestination.EMAIL: email,
        DeliveryDestination.SLACK: slack,
    }
    config = _config(DeliveryDestination.EMAIL, DeliveryDestination.SLACK)

    result = DigestService().deliver(_report(), config, deliverers)

    assert all(o.status == STATUS_DELIVERED for o in result.outcomes)
    assert email.attempts == 3
    assert slack.attempts == 3


def test_deliver_missing_deliverer_for_configured_destination_fails_independently():
    # Slack is configured but no deliverer is provided for it; email still delivers.
    email = InMemoryDeliverer(DeliveryDestination.EMAIL)
    config = _config(DeliveryDestination.EMAIL, DeliveryDestination.SLACK)

    result = DigestService().deliver(
        _report(), config, {DeliveryDestination.EMAIL: email}
    )
    by_dest = {o.destination: o for o in result.outcomes}

    assert by_dest[DeliveryDestination.EMAIL].status == STATUS_DELIVERED
    assert by_dest[DeliveryDestination.SLACK].status == STATUS_DELIVERY_FAILED
    assert by_dest[DeliveryDestination.SLACK].attempts == 0


# ---------------------------------------------------------------------------
# Supported destinations (13.5)
# ---------------------------------------------------------------------------


def test_supported_destinations_are_exactly_email_slack_notion():
    assert DigestService.SUPPORTED == {
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    }
