"""The ``Deliverer`` boundary and in-memory test stubs for digest delivery.

The ``Digest_Service`` (design.md -> *Digest_Service*, task 18.2) delivers a
compiled :class:`~viral_topic_agent.models.DigestReport` to each configured
:class:`~viral_topic_agent.models.DeliveryDestination` (email, Slack, Notion -
13.5). The *act* of pushing a report to one destination is an external,
side-effecting operation, so it lives behind the :class:`Deliverer` boundary
defined here. This keeps the ``Digest_Service`` itself pure and lets tests
inject deterministic success/failure without real email/Slack/Notion calls.

Contract (chosen so task 18.2 can layer per-destination retry on top, 13.6):

- A :class:`Deliverer` exposes a single :meth:`Deliverer.deliver` method that
  attempts *one* delivery of a report.
- On success it returns ``None``.
- On failure it raises :class:`DeliveryError`.

Raising (rather than returning a status) is deliberate: the retry loop in the
``Digest_Service`` becomes a plain ``try/except`` that counts attempts and
records a per-destination ``delivery-failed`` only after the bounded number of
attempts is exhausted. The single attempt stays a clean "did it work or not"
question; the *policy* (how many attempts, how to record the outcome) belongs
to the service, not the deliverer.

This module also provides :class:`InMemoryDeliverer` - a deterministic stub
with injectable success/failure and attempt counting - plus three thin
per-destination subclasses (:class:`EmailDeliverer`, :class:`SlackDeliverer`,
:class:`NotionDeliverer`). They are the substitutes used by ``Digest_Service``
tests (18.2-18.5) to exercise independent, retry-bounded delivery.

Requirements traceability: 13.5 (supported destinations are exactly email,
Slack, Notion); supports 13.3, 13.6, 13.7 once ``Digest_Service`` consumes it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.models import DeliveryDestination, DigestReport

__all__ = [
    "Deliverer",
    "DeliveryError",
    "InMemoryDeliverer",
    "EmailDeliverer",
    "SlackDeliverer",
    "NotionDeliverer",
]


# ---------------------------------------------------------------------------
# Failure signal
# ---------------------------------------------------------------------------


class DeliveryError(Exception):
    """Raised by a :class:`Deliverer` when a single delivery attempt fails.

    ``reason`` is a human-readable description. The ``Digest_Service`` catches
    this, counts the attempt, and retries up to the bounded maximum before
    recording a per-destination ``delivery-failed`` status (13.6).
    """

    def __init__(self, reason: str = "delivery failed") -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Deliverer boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class Deliverer(Protocol):
    """Delivers a compiled digest report to a single destination.

    Implementations attempt exactly one delivery per :meth:`deliver` call.
    They return ``None`` on success and raise :class:`DeliveryError` on
    failure so the caller (the ``Digest_Service``) can apply per-destination
    retry and record the outcome (13.6, 13.7).
    """

    def deliver(self, report: DigestReport) -> None:
        """Attempt to deliver ``report``; raise :class:`DeliveryError` on failure."""
        ...


# ---------------------------------------------------------------------------
# In-memory stub deliverer
# ---------------------------------------------------------------------------


class InMemoryDeliverer:
    """A deterministic, in-memory :class:`Deliverer` stub for tests.

    The stub does no real I/O. Its success/failure behaviour is injected at
    construction so tests can reproduce every branch the ``Digest_Service``
    must handle:

    - **Always succeed** (the default): ``InMemoryDeliverer()``.
    - **Always fail** (permanent failure -> exhausts all retries, 13.6):
      ``InMemoryDeliverer(always_fail=True)``.
    - **Fail the first N attempts, then succeed** (transient failure that
      recovers on retry): ``InMemoryDeliverer(fail_first=2)`` fails attempts 1
      and 2 and succeeds on attempt 3.

    Every call to :meth:`deliver` increments :attr:`attempts`, whether it
    succeeds or fails, so tests can assert exactly how many delivery attempts
    were made. Successfully delivered reports are recorded in
    :attr:`delivered_reports`.
    """

    def __init__(
        self,
        destination: DeliveryDestination | None = None,
        *,
        always_fail: bool = False,
        fail_first: int = 0,
        failure_reason: str = "delivery failed",
    ) -> None:
        if fail_first < 0:
            raise ValueError("fail_first must be non-negative")
        self.destination = destination
        self.always_fail = always_fail
        self.fail_first = fail_first
        self.failure_reason = failure_reason
        # Total number of delivery attempts made against this stub.
        self.attempts = 0
        # Reports that were delivered successfully (in order).
        self.delivered_reports: list[DigestReport] = []

    def deliver(self, report: DigestReport) -> None:
        """Attempt one delivery, honouring the injected success/failure plan.

        Increments :attr:`attempts` on every call. Raises
        :class:`DeliveryError` when configured to fail for this attempt;
        otherwise records ``report`` in :attr:`delivered_reports`.
        """
        self.attempts += 1
        if self.always_fail or self.attempts <= self.fail_first:
            raise DeliveryError(self.failure_reason)
        self.delivered_reports.append(report)

    @property
    def delivered(self) -> bool:
        """Whether at least one report was delivered successfully."""
        return bool(self.delivered_reports)


# ---------------------------------------------------------------------------
# Per-destination stubs (13.5: exactly email, Slack, Notion)
# ---------------------------------------------------------------------------


class EmailDeliverer(InMemoryDeliverer):
    """In-memory stub bound to :attr:`DeliveryDestination.EMAIL` (13.5)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(DeliveryDestination.EMAIL, **kwargs)  # type: ignore[arg-type]


class SlackDeliverer(InMemoryDeliverer):
    """In-memory stub bound to :attr:`DeliveryDestination.SLACK` (13.5)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(DeliveryDestination.SLACK, **kwargs)  # type: ignore[arg-type]


class NotionDeliverer(InMemoryDeliverer):
    """In-memory stub bound to :attr:`DeliveryDestination.NOTION` (13.5)."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(DeliveryDestination.NOTION, **kwargs)  # type: ignore[arg-type]
