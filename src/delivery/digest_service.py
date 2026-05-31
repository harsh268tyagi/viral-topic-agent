"""Recurring digest compilation and delivery (Requirement 13).

The :class:`DigestService` is the Delivery-layer component that turns the
analysis results into a single :class:`~viral_topic_agent.models.DigestReport`
and pushes that report to each configured
:class:`~viral_topic_agent.models.DeliveryDestination`.

Design references (``.kiro/specs/viral-topic-agent/design.md`` -> Digest_Service):

- :meth:`DigestService.compile` builds **one** report with a distinct section
  for each of the three item types -- scored ideas, competitor spikes, and
  outliers (13.1). A section whose item type contains zero items carries a
  ``no-items`` indicator (``DigestSection.no_items``) (13.2).
- :meth:`DigestService.deliver` delivers the report to every destination
  configured in the :class:`~viral_topic_agent.models.Configuration` (13.3),
  supporting exactly email, Slack, and Notion (13.5, see :attr:`SUPPORTED`).
- When **no** destination is configured, the service makes no delivery attempt
  and records a ``no-destination-configured`` status (13.4).
- Delivery to a destination is retried up to a bounded **3 total attempts**;
  if every attempt fails the service records a per-destination
  ``delivery-failed`` status (13.6).
- Destinations are delivered to **independently**: a failure (even after
  exhausting retries) at one destination never prevents delivery to the
  remaining destinations (13.7).

Boundary
--------
The *act* of pushing the report to a single destination lives behind the
:class:`~viral_topic_agent.delivery.Deliverer` boundary (task 18.1). A
:class:`Deliverer` attempts exactly one delivery: it returns ``None`` on
success and raises :class:`~viral_topic_agent.delivery.DeliveryError` on
failure. This keeps :class:`DigestService` itself pure with respect to its
inputs -- the *policy* (retry count, independence, status recording) lives
here, while the side-effecting single attempt lives in the deliverer. Tests
inject deterministic :class:`~viral_topic_agent.delivery.InMemoryDeliverer`
stubs to exercise every branch without real I/O.

Surfacing the no-destination-configured status
-----------------------------------------------
The design sketches ``deliver(...) -> list[DeliveryOutcome]``, but a bare list
cannot cleanly express the 13.4 "no destination configured" status without
inventing a sentinel :class:`~viral_topic_agent.models.DeliveryOutcome` with a
``None`` destination (which the model's typing forbids). Following the
codebase's established result-object pattern (``FilterResult``,
``OutlierResult``, ``KeywordGapResult`` ...), :meth:`deliver` returns a
:class:`DeliveryResult` that carries the per-destination ``outcomes`` tuple plus
an explicit ``no_destination_configured`` flag. The per-destination outcomes
remain first-class and iterable for the retry/independence property tests.

Requirements traceability: 13.1, 13.2, 13.3, 13.4, 13.6, 13.7.
"""

from __future__ import annotations

from dataclasses import dataclass

from delivery.deliverer import Deliverer, DeliveryError
from domain.models import (
    CompetitorSpike,
    Configuration,
    DeliveryDestination,
    DeliveryOutcome,
    DigestReport,
    DigestSection,
    Outlier,
    ScoredIdea,
)

__all__ = [
    "MAX_DELIVERY_ATTEMPTS",
    "SECTION_SCORED_IDEAS",
    "SECTION_COMPETITOR_SPIKES",
    "SECTION_OUTLIERS",
    "STATUS_DELIVERED",
    "STATUS_DELIVERY_FAILED",
    "STATUS_NO_DESTINATION_CONFIGURED",
    "DeliveryResult",
    "DigestService",
]


# Maximum total delivery attempts per destination, including the first (13.6).
MAX_DELIVERY_ATTEMPTS = 3

# Stable item-type identifiers for the three distinct report sections (13.1).
SECTION_SCORED_IDEAS = "scored_ideas"
SECTION_COMPETITOR_SPIKES = "competitor_spikes"
SECTION_OUTLIERS = "outliers"

# Per-destination delivery statuses recorded on a DeliveryOutcome.
STATUS_DELIVERED = "delivered"
STATUS_DELIVERY_FAILED = "delivery-failed"  # all attempts exhausted (13.6)

# Status recorded when the Configuration names no destination at all (13.4).
STATUS_NO_DESTINATION_CONFIGURED = "no-destination-configured"


@dataclass(frozen=True)
class DeliveryResult:
    """Outcome of delivering a digest report across all configured destinations.

    ``outcomes`` holds one :class:`~viral_topic_agent.models.DeliveryOutcome`
    per configured destination, in configuration order (13.3, 13.6, 13.7).
    ``no_destination_configured`` is ``True`` exactly when the
    :class:`~viral_topic_agent.models.Configuration` named no destination, in
    which case no delivery was attempted and ``outcomes`` is empty (13.4).
    """

    outcomes: tuple[DeliveryOutcome, ...]
    no_destination_configured: bool = False


class DigestService:
    """Compiles and delivers the recurring digest report (Requirement 13)."""

    # Exactly the destinations the service supports (13.5).
    SUPPORTED = frozenset(
        {
            DeliveryDestination.EMAIL,
            DeliveryDestination.SLACK,
            DeliveryDestination.NOTION,
        }
    )

    def compile(
        self,
        scored: list[ScoredIdea],
        spikes: list[CompetitorSpike],
        outliers: list[Outlier],
    ) -> DigestReport:
        """Compile the three item collections into one report (13.1, 13.2).

        The report always has exactly three distinct typed sections, in a
        fixed order: scored ideas, competitor spikes, then outliers (13.1).
        Each section's ``no_items`` flag is set when -- and only when -- that
        section's item type contains zero items (13.2).
        """
        return DigestReport(
            sections=(
                self._section(SECTION_SCORED_IDEAS, scored),
                self._section(SECTION_COMPETITOR_SPIKES, spikes),
                self._section(SECTION_OUTLIERS, outliers),
            )
        )

    @staticmethod
    def _section(item_type: str, items: list) -> DigestSection:
        """Build one typed section, flagging ``no_items`` for an empty collection."""
        materialized = tuple(items)
        return DigestSection(
            item_type=item_type,
            items=materialized,
            no_items=len(materialized) == 0,
        )

    def deliver(
        self,
        report: DigestReport,
        config: Configuration,
        deliverers: dict[DeliveryDestination, Deliverer],
    ) -> DeliveryResult:
        """Deliver ``report`` to every configured destination (13.3, 13.6, 13.7).

        - No destination configured: no attempt is made and the result carries
          ``no_destination_configured=True`` with empty ``outcomes`` (13.4).
        - Otherwise each configured destination is delivered to independently
          (13.7), with up to :data:`MAX_DELIVERY_ATTEMPTS` total attempts;
          ``delivery-failed`` is recorded for a destination only after every
          attempt fails (13.6).
        """
        if not config.delivery_destinations:
            return DeliveryResult(outcomes=(), no_destination_configured=True)

        outcomes: list[DeliveryOutcome] = []
        for destination in config.delivery_destinations:
            outcomes.append(
                self._deliver_to_destination(report, destination, deliverers)
            )
        return DeliveryResult(outcomes=tuple(outcomes), no_destination_configured=False)

    def _deliver_to_destination(
        self,
        report: DigestReport,
        destination: DeliveryDestination,
        deliverers: dict[DeliveryDestination, Deliverer],
    ) -> DeliveryOutcome:
        """Attempt bounded, retrying delivery to a single destination (13.6).

        Each destination is handled in isolation so its outcome -- success or
        an exhausted ``delivery-failed`` -- never affects the others (13.7).
        A configured destination with no injected :class:`Deliverer` cannot be
        delivered to; it is recorded as ``delivery-failed`` with zero attempts
        rather than raising, preserving independence across destinations.
        """
        deliverer = deliverers.get(destination)
        if deliverer is None:
            return DeliveryOutcome(
                destination=destination,
                status=STATUS_DELIVERY_FAILED,
                attempts=0,
            )

        attempts = 0
        for attempts in range(1, MAX_DELIVERY_ATTEMPTS + 1):
            try:
                deliverer.deliver(report)
            except DeliveryError:
                # Failed attempt; retry until the bounded maximum is reached.
                continue
            else:
                return DeliveryOutcome(
                    destination=destination,
                    status=STATUS_DELIVERED,
                    attempts=attempts,
                )

        # Every attempt failed -> record per-destination delivery-failed (13.6).
        return DeliveryOutcome(
            destination=destination,
            status=STATUS_DELIVERY_FAILED,
            attempts=attempts,
        )
