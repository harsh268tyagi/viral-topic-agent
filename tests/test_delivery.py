"""Tests for the ``Deliverer`` boundary and in-memory delivery stubs (task 18.1).

Focuses on the stubs' injectable success/failure behaviour and attempt
counting, which the ``Digest_Service`` (task 18.2) relies on to exercise
per-destination, retry-bounded, independent delivery (13.3, 13.6, 13.7). Also
confirms the supported destinations are exactly email, Slack, Notion (13.5).
"""

import pytest

from viral_topic_agent.delivery import (
    Deliverer,
    DeliveryError,
    EmailDeliverer,
    InMemoryDeliverer,
    NotionDeliverer,
    SlackDeliverer,
)
from viral_topic_agent.domain.models import DeliveryDestination, DigestReport, DigestSection


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _empty_report() -> DigestReport:
    """A minimal valid report: three distinct typed sections, each empty."""
    return DigestReport(
        sections=(
            DigestSection(item_type="scored_ideas", items=(), no_items=True),
            DigestSection(item_type="competitor_spikes", items=(), no_items=True),
            DigestSection(item_type="outliers", items=(), no_items=True),
        )
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_in_memory_deliverer_satisfies_deliverer_protocol():
    assert isinstance(InMemoryDeliverer(), Deliverer)


def test_per_destination_stubs_satisfy_deliverer_protocol():
    assert isinstance(EmailDeliverer(), Deliverer)
    assert isinstance(SlackDeliverer(), Deliverer)
    assert isinstance(NotionDeliverer(), Deliverer)


# ---------------------------------------------------------------------------
# Default: always succeed
# ---------------------------------------------------------------------------


def test_default_deliverer_succeeds_and_records_report():
    deliverer = InMemoryDeliverer()
    report = _empty_report()

    deliverer.deliver(report)

    assert deliverer.attempts == 1
    assert deliverer.delivered is True
    assert deliverer.delivered_reports == [report]


def test_default_deliverer_counts_each_successful_attempt():
    deliverer = InMemoryDeliverer()
    report = _empty_report()

    deliverer.deliver(report)
    deliverer.deliver(report)
    deliverer.deliver(report)

    assert deliverer.attempts == 3
    assert deliverer.delivered_reports == [report, report, report]


# ---------------------------------------------------------------------------
# Injectable: always fail
# ---------------------------------------------------------------------------


def test_always_fail_raises_and_never_records_delivery():
    deliverer = InMemoryDeliverer(always_fail=True)

    with pytest.raises(DeliveryError):
        deliverer.deliver(_empty_report())

    assert deliverer.attempts == 1
    assert deliverer.delivered is False
    assert deliverer.delivered_reports == []


def test_always_fail_counts_every_attempt():
    deliverer = InMemoryDeliverer(always_fail=True)

    for _ in range(3):
        with pytest.raises(DeliveryError):
            deliverer.deliver(_empty_report())

    assert deliverer.attempts == 3
    assert deliverer.delivered is False


def test_failure_reason_is_carried_on_the_error():
    deliverer = InMemoryDeliverer(always_fail=True, failure_reason="smtp unreachable")

    with pytest.raises(DeliveryError) as excinfo:
        deliverer.deliver(_empty_report())

    assert excinfo.value.reason == "smtp unreachable"


# ---------------------------------------------------------------------------
# Injectable: fail the first N attempts, then succeed
# ---------------------------------------------------------------------------


def test_fail_first_then_succeed_on_next_attempt():
    deliverer = InMemoryDeliverer(fail_first=2)
    report = _empty_report()

    # Attempt 1 fails.
    with pytest.raises(DeliveryError):
        deliverer.deliver(report)
    # Attempt 2 fails.
    with pytest.raises(DeliveryError):
        deliverer.deliver(report)
    # Attempt 3 succeeds.
    deliverer.deliver(report)

    assert deliverer.attempts == 3
    assert deliverer.delivered is True
    assert deliverer.delivered_reports == [report]


def test_fail_first_zero_is_equivalent_to_always_succeed():
    deliverer = InMemoryDeliverer(fail_first=0)

    deliverer.deliver(_empty_report())

    assert deliverer.attempts == 1
    assert deliverer.delivered is True


def test_fail_first_rejects_negative():
    with pytest.raises(ValueError):
        InMemoryDeliverer(fail_first=-1)


# ---------------------------------------------------------------------------
# Per-destination binding (13.5)
# ---------------------------------------------------------------------------


def test_per_destination_stubs_bind_expected_destinations():
    assert EmailDeliverer().destination is DeliveryDestination.EMAIL
    assert SlackDeliverer().destination is DeliveryDestination.SLACK
    assert NotionDeliverer().destination is DeliveryDestination.NOTION


def test_per_destination_stubs_accept_injectable_failure():
    email = EmailDeliverer(always_fail=True)
    slack = SlackDeliverer(fail_first=1)
    report = _empty_report()

    with pytest.raises(DeliveryError):
        email.deliver(report)
    assert email.attempts == 1
    assert email.delivered is False

    # Slack fails once then succeeds.
    with pytest.raises(DeliveryError):
        slack.deliver(report)
    slack.deliver(report)
    assert slack.attempts == 2
    assert slack.delivered is True
