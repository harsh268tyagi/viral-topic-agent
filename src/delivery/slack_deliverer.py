"""The real :class:`SlackDeliverer` that posts a digest to Slack (Requirement 8).

This is the concrete, network-backed replacement for the in-memory
``SlackDeliverer`` stub in :mod:`delivery.deliverer`. It implements the existing
single-method ``Deliverer`` boundary (``deliver(report) -> None``, raising
:class:`~delivery.deliverer.DeliveryError` on failure), preserving the
one-attempt contract so the ``DigestService`` keeps owning per-destination retry
(design.md -> *Deliverers*; Requirement 8, 16.5).

Behaviour (Requirement 8):

- **Render-and-verify first (8.1, 8.2, 8.4).** Before posting, the report is
  rendered through the shared, pure :func:`~delivery.digest_renderer.render_digest`
  and verified to contain all three report sections, each empty section carrying
  the no-items indicator. The "three sections + no-items indicator" guarantee is
  implemented once in the shared renderer and only *checked* here, so it cannot
  drift between deliverers.
- **Post over the injected ``HttpTransport`` (8.3).** Delivery is a single
  ``chat.postMessage`` POST carrying a bearer token. The Slack Web API returns
  HTTP 200 even for logical failures, signalling the real outcome with an
  ``{"ok": bool, "error": str}`` body, so success requires *both* a 2xx status
  *and* ``ok == true``. On success :meth:`deliver` returns ``None``.
- **Redacted failure (8.5, 8.7).** Any posting failure -- a transport error, a
  non-2xx status, or an ``ok == false`` body -- raises a ``DeliveryError`` whose
  reason describes the failure and is scrubbed of every :class:`Secret` value via
  :func:`~config.secrets.redact`. The bearer token therefore never appears in a
  reason, log line, or traceback.
- **Last-resort fallback (8.6).** If a ``DeliveryError`` cannot itself be
  constructed or raised after a posting failure, the deliverer records a
  ``delivery-failed`` log entry that identifies the Slack destination and still
  surfaces a failure to the caller, so a posting failure is never silently
  swallowed.

The token is revealed (via :meth:`Secret.reveal`) only at the transport
boundary, where it must actually be transmitted; nowhere else (Requirement 12).

Requirements traceability: 8.1, 8.2, 8.3, 8.5, 8.6, 8.7 (and 7.4/8.4/9.4 via the
shared renderer; 16.5 via the single-attempt, no-retry contract).
"""

from __future__ import annotations

import json
import logging
from typing import Callable, NoReturn

from config.secrets import Secret, redact
from delivery.deliverer import DeliveryError
from delivery.digest_renderer import RenderedDigest, render_digest
from config.settings import SlackSettings
from domain.models import DigestReport
from infrastructure.http_transport import HttpTransport, HttpTransportError

__all__ = ["SlackDeliverer"]


logger = logging.getLogger(__name__)

# Slack Web API method used to post a message to a channel (design.md -> Slack).
_POST_MESSAGE_METHOD = "chat.postMessage"

# Fallback label used when even the configured destination cannot be read while
# recording the last-resort delivery-failed log entry (8.6).
_UNKNOWN_DESTINATION = "<unknown Slack destination>"


# Type of the injected renderer: a pure function from a report to a rendered
# payload. Defaults to the shared :func:`render_digest`.
DigestRenderer = Callable[[DigestReport], RenderedDigest]


class _SlackApiError(Exception):
    """Internal signal that a posting attempt did not succeed.

    Raised inside :meth:`SlackDeliverer._post` for a non-2xx status or an
    ``ok == false`` body. It never escapes this module: :meth:`deliver` maps it
    (and any transport error) to a redacted :class:`DeliveryError` (8.5, 8.7).
    """


class SlackDeliverer:
    """Posts a :class:`DigestReport` to Slack via ``chat.postMessage`` (Req. 8).

    Implements the single-method ``Deliverer`` boundary. Constructor-injected
    with an :class:`HttpTransport` (so it is testable without a network, 16.3),
    the :class:`SlackSettings` naming the destination and bearer token, and the
    shared :func:`render_digest` (overridable for tests).
    """

    def __init__(
        self,
        transport: HttpTransport,
        settings: SlackSettings,
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
        """Render, verify, and post ``report`` to Slack in a single attempt.

        Returns ``None`` on success (8.3). Raises :class:`DeliveryError` with a
        redacted reason on any posting failure (8.5, 8.7); if that error cannot
        be constructed/raised, logs a delivery-failed entry naming the
        destination and still surfaces a failure (8.6).
        """
        # Render-and-verify all three sections *before* attempting to post
        # (8.2, 8.4). A render/verify problem is a delivery failure too.
        try:
            rendered = self._render_and_verify(report)
        except Exception as exc:  # noqa: BLE001 - mapped to a redacted failure
            self._surface_failure(exc)

        # Single posting attempt; no retry/backoff lives here (16.5).
        try:
            self._post(rendered)
        except (HttpTransportError, _SlackApiError) as exc:
            self._surface_failure(exc)

    # -- Render-and-verify (8.2, 8.4) --------------------------------------

    def _render_and_verify(self, report: DigestReport) -> RenderedDigest:
        """Render ``report`` and confirm the three-sections + indicators guarantee.

        The shared renderer already guarantees this shape; verifying here makes
        the contract explicit and ensures a malformed payload is never posted.
        """
        rendered = self._renderer(report)

        if len(rendered.sections) != 3:
            raise _SlackApiError(
                f"rendered digest has {len(rendered.sections)} sections, expected 3"
            )
        for section in rendered.sections:
            if section.no_items and section.no_items_indicator is None:
                raise _SlackApiError(
                    f"empty section {section.item_type!r} is missing its "
                    "no-items indicator"
                )
        return rendered

    # -- Posting (8.3, 8.5) -------------------------------------------------

    def _post(self, rendered: RenderedDigest) -> None:
        """Perform the single ``chat.postMessage`` POST and check the outcome.

        Returns ``None`` only when the HTTP status is 2xx *and* the Slack body
        reports ``ok == true``. Otherwise raises :class:`_SlackApiError`; a
        transport-level failure propagates as :class:`HttpTransportError`.
        """
        url = f"{self._settings.api_base_url.rstrip('/')}/{_POST_MESSAGE_METHOD}"
        payload = json.dumps(
            {"channel": self._settings.channel, "text": rendered.body}
        ).encode("utf-8")
        headers = {
            # reveal() only here, at the transport boundary (Requirement 12).
            "Authorization": f"Bearer {self._settings.token.reveal()}",
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
            raise _SlackApiError(
                f"Slack responded with HTTP status {response.status}"
            )

        # Slack signals logical failure with HTTP 200 + {"ok": false, ...}.
        ok, error_code = self._parse_outcome(response.body)
        if not ok:
            detail = f": {error_code}" if error_code else ""
            raise _SlackApiError(f"Slack reported a failed post{detail}")

    @staticmethod
    def _parse_outcome(body: bytes) -> tuple[bool, str | None]:
        """Extract ``(ok, error_code)`` from a Slack response body.

        A body that is not valid JSON, or not a JSON object, is treated as a
        failed post rather than trusted as success.
        """
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False, "unparseable response body"
        if not isinstance(parsed, dict):
            return False, "unexpected response body"
        ok = bool(parsed.get("ok", False))
        error_code = parsed.get("error")
        return ok, error_code if isinstance(error_code, str) else None

    # -- Failure surfacing (8.5, 8.6, 8.7) ---------------------------------

    def _surface_failure(self, cause: BaseException) -> NoReturn:
        """Map a posting/verify failure to a redacted ``DeliveryError`` (8.5, 8.7).

        If the ``DeliveryError`` cannot itself be constructed or raised, record a
        delivery-failed log entry identifying the destination and still surface a
        failure to the caller (8.6) by re-raising the original ``cause``.
        """
        try:
            reason = redact(
                f"Slack delivery to {self._settings.channel} failed: {cause}",
                self._secrets(),
            )
            error = DeliveryError(reason)
        except Exception:  # noqa: BLE001 - 8.6 last-resort fallback
            self._log_delivery_failed()
            # Still surface a failure: re-raise the underlying posting failure so
            # the caller never mistakes a failed post for a success.
            raise cause
        raise error from cause

    def _secrets(self) -> tuple[Secret, ...]:
        """The secrets to scrub from any failure reason (currently the token)."""
        return (self._settings.token,)

    def _log_delivery_failed(self) -> None:
        """Record a delivery-failed entry naming the Slack destination (8.6).

        Reading the destination is itself guarded so the fallback log entry is
        emitted even if the settings cannot be inspected.
        """
        try:
            destination = self._settings.channel
        except Exception:  # noqa: BLE001 - defensive: still log a failure
            destination = _UNKNOWN_DESTINATION
        logger.error("delivery-failed: Slack destination %s", destination)
