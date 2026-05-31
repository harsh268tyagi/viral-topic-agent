"""Smoke/unit tests for the Digest_Service delivery destinations (task 18.5).

Two narrow, requirement-anchored checks that property testing is not suited to:

- 13.5: the supported Delivery_Destinations are *exactly* email, Slack, and
  Notion -- no more, no fewer. This pins both the ``DeliveryDestination`` enum
  and the ``DigestService.SUPPORTED`` set so an accidental addition/removal of a
  destination is caught.
- 13.4: when *no* Delivery_Destination is configured, the service makes no
  delivery attempt and records the ``no-destination-configured`` status.

Per-destination retry/independence (13.3, 13.6, 13.7) is covered by the
Property 24 test in ``tests/test_digest_service_properties.py`` and by the
example tests in ``tests/test_digest_service.py``.
"""

from __future__ import annotations

from delivery import (
    EmailDeliverer,
    InMemoryDeliverer,
    NotionDeliverer,
    SlackDeliverer,
)
from delivery.digest_service import (
    STATUS_NO_DESTINATION_CONFIGURED,
    DeliveryResult,
    DigestService,
)
from domain.models import (
    CompetitorSpike,
    Configuration,
    DeliveryDestination,
    DigestReport,
    Outlier,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _config(*destinations: DeliveryDestination) -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=tuple(destinations),
    )


def _report() -> DigestReport:
    spikes = [CompetitorSpike("c1", "v1", 9000, 3.5)]
    outliers = [Outlier("o1", 50000, 6.0)]
    return DigestService().compile([], spikes, outliers)


# ---------------------------------------------------------------------------
# 13.5: supported destinations are exactly email, Slack, Notion
# ---------------------------------------------------------------------------


def test_delivery_destination_enum_is_exactly_email_slack_notion():
    # The enum itself defines no destination beyond the three supported ones.
    assert {d.value for d in DeliveryDestination} == {"email", "slack", "notion"}
    assert set(DeliveryDestination) == {
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    }


def test_digest_service_supported_set_is_exactly_email_slack_notion():
    assert DigestService.SUPPORTED == {
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    }
    # And nothing more: SUPPORTED matches the full enum, no extra/missing member.
    assert set(DigestService.SUPPORTED) == set(DeliveryDestination)


def test_each_supported_destination_has_a_bound_deliverer_stub():
    # 13.5: a per-destination deliverer exists for each supported destination.
    stubs = {
        DeliveryDestination.EMAIL: EmailDeliverer(),
        DeliveryDestination.SLACK: SlackDeliverer(),
        DeliveryDestination.NOTION: NotionDeliverer(),
    }
    for destination, stub in stubs.items():
        assert stub.destination is destination
    assert set(stubs) == DigestService.SUPPORTED


# ---------------------------------------------------------------------------
# 13.4: no destination configured -> no-destination-configured status
# ---------------------------------------------------------------------------


def test_no_destination_configured_records_status_and_skips_delivery():
    result = DigestService().deliver(_report(), _config(), {})

    assert isinstance(result, DeliveryResult)
    assert result.no_destination_configured is True
    assert result.outcomes == ()


def test_no_destination_configured_makes_no_attempt_even_with_deliverers():
    # Deliverers are available, but with no configured destination none is used.
    email = InMemoryDeliverer(DeliveryDestination.EMAIL)
    slack = InMemoryDeliverer(DeliveryDestination.SLACK)
    notion = InMemoryDeliverer(DeliveryDestination.NOTION)

    result = DigestService().deliver(
        _report(),
        _config(),
        {
            DeliveryDestination.EMAIL: email,
            DeliveryDestination.SLACK: slack,
            DeliveryDestination.NOTION: notion,
        },
    )

    assert result.no_destination_configured is True
    assert email.attempts == 0
    assert slack.attempts == 0
    assert notion.attempts == 0


def test_no_destination_configured_status_constant_value():
    # Pin the recorded status string to the requirement's wording (13.4).
    assert STATUS_NO_DESTINATION_CONFIGURED == "no-destination-configured"
