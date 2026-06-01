"""The real :class:`EmailDeliverer` over the ``SmtpTransport`` port (Req. 7).

This module provides the concrete email :class:`~delivery.deliverer.Deliverer`
that replaces the in-memory stub for real runs (design.md -> *Deliverers*, task
9.3). It implements the existing single-method ``Deliverer`` boundary
(``deliver(report) -> None``, raising ``DeliveryError`` on failure), so the
``DigestService`` keeps owning per-destination retry unchanged (the one-attempt
contract, 16.5).

Responsibilities (Requirement 7):

- **Conformance (7.1):** expose a single :meth:`EmailDeliverer.deliver` that
  accepts a :class:`~domain.models.DigestReport`.
- **Render and verify (7.2, 7.4):** render the report through the shared, pure
  :func:`~delivery.digest_renderer.render_digest` and verify that all three
  report sections are present, including the no-items indicator for each empty
  section, *before* attempting transmission. The shared renderer is the single
  place that guarantees this shape; the deliverer re-checks it defensively so a
  malformed payload is never transmitted.
- **Success (7.3):** return ``None`` when the message is transmitted
  successfully to the configured recipient.
- **Failure (7.5, 7.6):** when transmission fails, raise a
  :class:`~delivery.deliverer.DeliveryError` whose reason describes the failure
  with every :class:`~config.secrets.Secret` value redacted.

Secret hygiene (7.6): the SMTP password is held as a :class:`Secret` on
:class:`~config.settings.EmailSettings` and is revealed only at the transport
boundary (inside :class:`~delivery.smtp_transport.SmtplibSmtpTransport`). Every
``DeliveryError`` reason this deliverer raises is passed through
:func:`~config.secrets.redact` against the configured secrets, so even a reason
derived from a third-party message cannot leak a credential value.

Requirements traceability: 7.1, 7.2, 7.3, 7.5, 7.6.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Callable

from config.secrets import Secret, redact
from config.settings import EmailSettings
from delivery.deliverer import DeliveryError
from delivery.digest_renderer import NO_ITEMS_INDICATOR, RenderedDigest, render_digest
from delivery.smtp_transport import SmtpTransport, SmtpTransportError
from domain.models import DigestReport

__all__ = ["EmailDeliverer"]


# A renderer is any pure callable from a report to a rendered payload; the
# shared :func:`render_digest` is the default and the only production renderer.
DigestRenderer = Callable[[DigestReport], RenderedDigest]


class EmailDeliverer:
    """Transmits a :class:`DigestReport` by email over a :class:`SmtpTransport`.

    Structurally satisfies the existing single-method
    :class:`~delivery.deliverer.Deliverer` boundary (7.1) without modifying it.
    The SMTP transport, addressing/credential settings, and the digest renderer
    are injected at construction so every branch is exercised against a fake
    transport with no real network access (16.3).
    """

    def __init__(
        self,
        smtp: SmtpTransport,
        settings: EmailSettings,
        renderer: DigestRenderer = render_digest,
    ) -> None:
        self._smtp = smtp
        self._settings = settings
        self._renderer = renderer

    # -- Deliverer boundary -------------------------------------------------

    def deliver(self, report: DigestReport) -> None:
        """Render, verify, and transmit ``report`` by email.

        Renders the report and verifies that all three sections are present
        (with a no-items indicator for each empty section) before transmitting
        (7.2, 7.4). Returns ``None`` on a successful send (7.3). Raises a
        :class:`DeliveryError` with a redacted reason if verification fails or
        the transport reports a transmission failure (7.5, 7.6).
        """
        rendered = self._renderer(report)
        self._verify(rendered)

        message = self._build_message(rendered)

        try:
            self._smtp.send(message)
        except SmtpTransportError as exc:
            raise DeliveryError(self._redact(f"email delivery failed: {exc.reason}")) from exc
        except Exception as exc:  # pragma: no cover - defensive: unexpected transport error
            # Any unexpected transport-level failure still surfaces as a
            # redacted DeliveryError; never echo the raw text, which could carry
            # a credential, and name the error type only (7.5, 7.6).
            raise DeliveryError(
                self._redact(f"email delivery failed: {type(exc).__name__}")
            ) from exc

    # -- Internal helpers ---------------------------------------------------

    def _verify(self, rendered: RenderedDigest) -> None:
        """Verify the rendered payload carries all three sections + indicators.

        Raises :class:`DeliveryError` (redacted) before any transmission if the
        rendering does not contain exactly three sections, or if a section that
        contains zero items lacks its no-items indicator (7.2, 7.4).
        """
        sections = rendered.sections
        if len(sections) != 3:
            raise DeliveryError(
                self._redact(
                    "email delivery aborted: rendered digest must contain "
                    f"three sections, found {len(sections)}"
                )
            )
        for section in sections:
            if section.no_items and section.no_items_indicator != NO_ITEMS_INDICATOR:
                raise DeliveryError(
                    self._redact(
                        "email delivery aborted: empty section "
                        f"'{section.item_type}' is missing its no-items indicator"
                    )
                )

    def _build_message(self, rendered: RenderedDigest) -> EmailMessage:
        """Build the :class:`EmailMessage` to transmit from ``rendered``."""
        message = EmailMessage()
        message["Subject"] = rendered.subject
        message["From"] = self._settings.sender
        message["To"] = self._settings.recipient
        message.set_content(rendered.body)
        return message

    def _redact(self, reason: str) -> str:
        """Scrub every configured secret value out of ``reason`` (7.6)."""
        return redact(reason, self._secrets())

    def _secrets(self) -> tuple[Secret, ...]:
        """The secrets that must never appear in an error reason."""
        return (self._settings.password,)
