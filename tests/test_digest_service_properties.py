"""Hypothesis property tests for the Digest_Service (tasks 18.3 and 18.4).

This module hosts the two universal correctness properties for the
``Digest_Service`` (design.md -> *Digest_Service*). Concrete, hand-checked
examples and the no-destination-configured / supported-set edge cases live in
``tests/test_digest_service.py`` and ``tests/test_delivery_destinations.py``;
this module is the universal layer.

Property 23 (compile): *for any* sets of scored ideas, competitor spikes, and
outliers, the compiled report SHALL contain exactly three distinct sections --
one for each item type, preserving their items -- and a section SHALL carry a
``no-items`` indicator *if and only if* it contains zero items.

Property 24 (deliver): *for any* set of configured destinations with arbitrary
per-destination success or failure, every configured destination SHALL receive
its own delivery outcome, each destination SHALL be attempted at most 3 times, a
destination whose attempts all fail SHALL be recorded ``delivery-failed``, and a
failure at one destination SHALL NOT prevent successful delivery to the others.

Validates: Requirements 13.1, 13.2 (Property 23); 13.3, 13.6, 13.7 (Property 24)
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from delivery import InMemoryDeliverer
from delivery.digest_service import (
    MAX_DELIVERY_ATTEMPTS,
    SECTION_COMPETITOR_SPIKES,
    SECTION_OUTLIERS,
    SECTION_SCORED_IDEAS,
    STATUS_DELIVERED,
    STATUS_DELIVERY_FAILED,
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
# Item builders (one distinct item per index so order/content preservation is
# observable when comparing a section's items against its source list).
# ---------------------------------------------------------------------------

_TEMPLATE = ViralTemplate(
    template_id="t0",
    name="tier-list ranking",
    category=ChannelCategory.GAMING,
    observed_performance=1000.0,
)


def _scored_idea(idea_id: str) -> ScoredIdea:
    idea = ContentIdea(
        idea_id=idea_id,
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(_TEMPLATE,),
        observed_metric_value=5000.0,
    )
    return ScoredIdea(idea=idea, score=50, confidence=Confidence.NORMAL)


def _spike(video_id: str) -> CompetitorSpike:
    return CompetitorSpike(
        channel_id=f"comp-{video_id}",
        video_id=video_id,
        view_count=9000,
        spike_factor=3.5,
    )


def _outlier(video_id: str) -> Outlier:
    return Outlier(video_id=video_id, view_count=50000, outlier_factor=6.0)


# ---------------------------------------------------------------------------
# Strategies
#
# Counts span zero so both the populated and the empty (no-items) branch of
# every section are exercised; distinct ids make item preservation observable.
# ---------------------------------------------------------------------------

_counts = st.integers(min_value=0, max_value=5)


@st.composite
def _item_lists(
    draw: st.DrawFn,
) -> tuple[list[ScoredIdea], list[CompetitorSpike], list[Outlier]]:
    scored = [_scored_idea(f"i{i}") for i in range(draw(_counts))]
    spikes = [_spike(f"v{i}") for i in range(draw(_counts))]
    outliers = [_outlier(f"o{i}") for i in range(draw(_counts))]
    return scored, spikes, outliers


# A non-empty set of configured destinations (the empty / no-destination case is
# covered by the 13.4 smoke tests). Each destination carries an injected plan:
# ``always_fail`` plus a ``fail_first`` count, which together determine whether
# delivery succeeds within the bounded number of attempts.
@st.composite
def _delivery_plan(
    draw: st.DrawFn,
) -> dict[DeliveryDestination, tuple[bool, int]]:
    destinations = draw(
        st.lists(
            st.sampled_from(list(DeliveryDestination)),
            unique=True,
            min_size=1,
            max_size=len(DeliveryDestination),
        )
    )
    plan: dict[DeliveryDestination, tuple[bool, int]] = {}
    for destination in destinations:
        always_fail = draw(st.booleans())
        # 0..2 => succeeds within the bound; >= MAX => never succeeds in time.
        fail_first = draw(st.integers(min_value=0, max_value=5))
        plan[destination] = (always_fail, fail_first)
    return plan


def _report() -> DigestReport:
    """A representative compiled report; delivery does not inspect its contents."""
    return DigestService().compile([_scored_idea("i0")], [_spike("v0")], [_outlier("o0")])


def _config(*destinations: DeliveryDestination) -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=tuple(destinations),
    )


# ---------------------------------------------------------------------------
# Property 23
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 23: Digest report has three typed sections with correct no-items indicators
@settings(max_examples=200)
@given(items=_item_lists())
def test_report_has_three_typed_sections_with_no_items_iff_empty(
    items: tuple[list[ScoredIdea], list[CompetitorSpike], list[Outlier]],
) -> None:
    """Exactly three distinct typed sections, items preserved, no_items iff empty.

    Validates: Requirements 13.1, 13.2
    """
    scored, spikes, outliers = items
    report = DigestService().compile(scored, spikes, outliers)

    # (13.1) exactly three sections, one distinct section per item type.
    assert len(report.sections) == 3
    item_types = [section.item_type for section in report.sections]
    assert len(set(item_types)) == 3
    assert set(item_types) == {
        SECTION_SCORED_IDEAS,
        SECTION_COMPETITOR_SPIKES,
        SECTION_OUTLIERS,
    }

    by_type = {section.item_type: section for section in report.sections}

    # (13.1) each section preserves exactly its source items, in order.
    assert by_type[SECTION_SCORED_IDEAS].items == tuple(scored)
    assert by_type[SECTION_COMPETITOR_SPIKES].items == tuple(spikes)
    assert by_type[SECTION_OUTLIERS].items == tuple(outliers)

    # (13.2) a section carries the no-items indicator iff it has zero items.
    for section in report.sections:
        assert section.no_items == (len(section.items) == 0)


# ---------------------------------------------------------------------------
# Property 24
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 24: Delivery is per-destination, independent, and retry-bounded
@settings(max_examples=200)
@given(plan=_delivery_plan())
def test_delivery_is_per_destination_independent_and_retry_bounded(
    plan: dict[DeliveryDestination, tuple[bool, int]],
) -> None:
    """One bounded outcome per destination; failures never block successes.

    Validates: Requirements 13.3, 13.6, 13.7
    """
    report = _report()
    deliverers = {
        destination: InMemoryDeliverer(
            destination, always_fail=always_fail, fail_first=fail_first
        )
        for destination, (always_fail, fail_first) in plan.items()
    }
    config = _config(*plan.keys())

    result = DigestService().deliver(report, config, deliverers)

    # A configured set means delivery is attempted (not the 13.4 empty case).
    assert result.no_destination_configured is False

    # (13.3) every configured destination receives its own outcome, exactly one.
    assert len(result.outcomes) == len(plan)
    assert {outcome.destination for outcome in result.outcomes} == set(plan)
    by_dest = {outcome.destination: outcome for outcome in result.outcomes}

    for destination, (always_fail, fail_first) in plan.items():
        outcome = by_dest[destination]
        deliverer = deliverers[destination]

        # (13.6) each destination is attempted at most MAX_DELIVERY_ATTEMPTS times,
        # and the outcome's attempt count matches the deliverer's actual attempts.
        assert outcome.attempts <= MAX_DELIVERY_ATTEMPTS
        assert deliverer.attempts <= MAX_DELIVERY_ATTEMPTS
        assert outcome.attempts == deliverer.attempts

        succeeds_within_bound = (not always_fail) and fail_first < MAX_DELIVERY_ATTEMPTS
        if succeeds_within_bound:
            assert outcome.status == STATUS_DELIVERED
            assert outcome.attempts == fail_first + 1
            assert deliverer.delivered_reports == [report]
        else:
            # (13.6) all attempts fail -> delivery-failed after the full bound.
            assert outcome.status == STATUS_DELIVERY_FAILED
            assert outcome.attempts == MAX_DELIVERY_ATTEMPTS
            assert deliverer.delivered is False

    # (13.7) independence: every destination that can succeed within the bound is
    # delivered regardless of any other destination failing, and only those are.
    delivered = {
        outcome.destination
        for outcome in result.outcomes
        if outcome.status == STATUS_DELIVERED
    }
    expected_delivered = {
        destination
        for destination, (always_fail, fail_first) in plan.items()
        if (not always_fail) and fail_first < MAX_DELIVERY_ATTEMPTS
    }
    assert delivered == expected_delivered
