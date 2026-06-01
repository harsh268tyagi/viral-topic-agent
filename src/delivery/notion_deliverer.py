"""The real :class:`NotionDeliverer` that records a digest in Notion (Req. 9).

This is the concrete, network-backed replacement for the in-memory
``NotionDeliverer`` stub in :mod:`delivery.deliverer`. It implements the existing
single-method ``Deliverer`` boundary (``deliver(report) -> None``, raising
:class:`~delivery.deliverer.DeliveryError` on failure), preserving the
one-attempt contract so the ``DigestService`` keeps owning per-destination retry
(design.md -> *Deliverers*; Requirement 9, 16.5).

Behaviour (Requirement 9):

- **Conformance (9.1).** Expose a single :meth:`NotionDeliverer.deliver` that
  accepts a :class:`~domain.models.DigestReport`.
- **Render-and-verify first (9.2, 9.4).** Before recording, the report is
  rendered through the shared, pure
  :func:`~delivery.digest_renderer.render_digest` and verified to contain all
  three report sections, each empty section carrying the no-items indicator. The
  "three sections + no-items indicator" guarantee is implemented once in the
  shared renderer and only *checked* here, so it cannot drift between deliverers
  and a malformed payload is never recorded.
- **Record over the injected ``HttpTransport`` (9.3).** Recording is a single
  ``pages.create`` POST (``POST {api_base_url}/pages``) carrying a bearer token
  and the Notion API-version header. Notion signals the real outcome with the
  HTTP status, so success requires a 2xx status; on success :meth:`deliver`
  returns ``None``.
- **Redacted failure (9.5, 9.6).** Any recording failure -- a transport error or
  a non-2xx status -- raises a :class:`~delivery.deliverer.DeliveryError` whose
  reason describes the failure and is scrubbed of every
  :class:`~config.secrets.Secret` value via :func:`~config.secrets.redact`. The
  integration token therefore never appears in a reason, log line, or traceback.

The token is revealed (via :meth:`Secret.reveal`) only at the transport
boundary, where it must actually be transmitted; nowhere else (Requirement 12).

Requirements traceability: 9.1, 9.2, 9.3, 9.5, 9.6 (and 7.4/8.4/9.4 via the
shared renderer; 16.5 via the single-attempt, no-retry contract).
"""

from __future__ import annotations

import json
from typing import Callable, NoReturn

from config.secrets import Secret, redact
from config.settings import NotionSettings
from delivery.deliverer import DeliveryError
from delivery.digest_renderer import (
    NO_ITEMS_INDICATOR,
    RenderedDigest,
    RenderedSection,
    render_digest,
)
from domain.models import DigestReport
from infrastructure.http_transport import HttpTransport, HttpTransportError

__all__ = ["NotionDeliverer"]


# Notion REST path used to create (record) a page in a database
# (design.md -> Notion: "REST `pages.create` over `HttpTransport`").
_PAGES_PATH = "pages"

# Notion's title property column on a freshly-created database defaults to
# "Name"; the digest subject is recorded there.
_TITLE_PROPERTY = "Name"

# Notion rejects a single rich-text content value longer than 2000 characters,
# so each paragraph block's text is bounded to this length defensively.
_MAX_RICH_TEXT = 2000


# A renderer is any pure callable from a report to a rendered payload; the
# shared :func:`render_digest` is the default and the only production renderer.
DigestRenderer = Callable[[DigestReport], RenderedDigest]


class _NotionApiError(Exception):
    """Internal signal that a recording attempt did not succeed.

    Raised inside :meth:`NotionDeliverer._record` for a non-2xx status. It never
    escapes this module: :meth:`deliver` maps it (and any transport error) to a
    redacted :class:`DeliveryError` (9.5, 9.6).
    """


class NotionDeliverer:
    """Records a :class:`DigestReport` in Notion via ``pages.create`` (Req. 9).

    Structurally satisfies the existing single-method
    :class:`~delivery.deliverer.Deliverer` boundary (9.1) without modifying it.
    Constructor-injected with an :class:`HttpTransport` (so it is testable
    without a network, 16.3), the :class:`NotionSettings` naming the target
    database, API version, and bearer token, and the shared
    :func:`render_digest` (overridable for tests).
    """

    def __init__(
        self,
        transport: HttpTransport,
        settings: NotionSettings,
        renderer: DigestRenderer = render_digest,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        self._transport = transport
        self._settings = settings
        self._renderer = renderer
        self._timeout_seconds = timeout_seconds

    # -- Deliverer boundary -------------------------------------------------

    def deliver(self, report: DigestReport) -> None:
        """Render, verify, and record ``report`` in Notion in a single attempt.

        Renders the report and verifies that all three sections are present
        (with a no-items indicator for each empty section) before recording
        (9.2, 9.4). Returns ``None`` on a successful record (9.3). Raises a
        :class:`DeliveryError` with a redacted reason if verification fails or
        the recording attempt fails (9.5, 9.6).
        """
        # Render-and-verify all three sections *before* attempting to record
        # (9.2, 9.4). A render/verify problem is a delivery failure too.
        try:
            rendered = self._render_and_verify(report)
        except _NotionApiError as exc:
            self._surface_failure(exc)

        # Single recording attempt; no retry/backoff lives here (16.5).
        try:
            self._record(rendered)
        except (HttpTransportError, _NotionApiError) as exc:
            self._surface_failure(exc)

    # -- Render-and-verify (9.2, 9.4) --------------------------------------

    def _render_and_verify(self, report: DigestReport) -> RenderedDigest:
        """Render ``report`` and confirm the three-sections + indicators guarantee.

        The shared renderer already guarantees this shape; verifying here makes
        the contract explicit and ensures a malformed payload is never recorded.
        """
        rendered = self._renderer(report)

        if len(rendered.sections) != 3:
            raise _NotionApiError(
                f"rendered digest has {len(rendered.sections)} sections, expected 3"
            )
        for section in rendered.sections:
            if section.no_items and section.no_items_indicator != NO_ITEMS_INDICATOR:
                raise _NotionApiError(
                    f"empty section {section.item_type!r} is missing its "
                    "no-items indicator"
                )
        return rendered

    # -- Recording (9.3, 9.5) ----------------------------------------------

    def _record(self, rendered: RenderedDigest) -> None:
        """Perform the single ``pages.create`` POST and check the outcome.

        Returns ``None`` only when the HTTP status is 2xx (9.3); otherwise raises
        :class:`_NotionApiError`. A transport-level failure propagates as
        :class:`HttpTransportError`.
        """
        url = f"{self._settings.api_base_url.rstrip('/')}/{_PAGES_PATH}"
        payload = json.dumps(self._build_page(rendered)).encode("utf-8")
        headers = {
            # reveal() only here, at the transport boundary (Requirement 12).
            "Authorization": f"Bearer {self._settings.token.reveal()}",
            "Notion-Version": self._settings.api_version,
            "Content-Type": "application/json; charset=utf-8",
        }

        response = self._transport.request(
            "POST",
            url,
            headers=headers,
            body=payload,
            timeout_seconds=self._timeout_seconds,
        )

        if not 200 <= response.status < 300:
            raise _NotionApiError(
                f"Notion responded with HTTP status {response.status}"
            )

    def _build_page(self, rendered: RenderedDigest) -> dict[str, object]:
        """Build the ``pages.create`` request body recording the digest.

        The digest subject becomes the page title and each rendered section
        becomes one paragraph block, so every section (and its no-items
        indicator) is recorded in the page body (9.4).
        """
        return {
            "parent": {"database_id": self._settings.database_id},
            "properties": {
                _TITLE_PROPERTY: {
                    "title": [{"text": {"content": rendered.subject}}]
                }
            },
            "children": [
                self._paragraph_block(section) for section in rendered.sections
            ],
        }

    @staticmethod
    def _paragraph_block(section: RenderedSection) -> dict[str, object]:
        """Render one section as a Notion paragraph block.

        The heading and the section's lines (which include the no-items
        indicator for an empty section) are joined into a single rich-text run,
        bounded to Notion's per-value length limit.
        """
        content = "\n".join((section.heading, *section.lines))[:_MAX_RICH_TEXT]
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }

    # -- Failure surfacing (9.5, 9.6) --------------------------------------

    def _surface_failure(self, cause: BaseException) -> NoReturn:
        """Map a recording/verify failure to a redacted ``DeliveryError`` (9.5, 9.6).

        The reason describes the failure and is scrubbed of every configured
        secret value, so the integration token never leaks through an error
        reason, log line, or traceback.
        """
        reason = redact(
            f"Notion delivery to database {self._settings.database_id} failed: {cause}",
            self._secrets(),
        )
        raise DeliveryError(reason) from cause

    def _secrets(self) -> tuple[Secret, ...]:
        """The secrets to scrub from any failure reason (currently the token)."""
        return (self._settings.token,)
