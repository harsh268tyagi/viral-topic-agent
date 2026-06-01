"""Pure mapping of YouTube failure signals to the ``DataSourceError`` hierarchy.

The design (``.kiro/specs/real-provider-integration/design.md`` -> *Error
Handling* / *YouTube error mapping*) requires ``YouTubeDataSource`` to classify
every failure signal it observes into exactly one member of the existing
``DataSourceError`` hierarchy, in a fixed precedence, and to build every reason
string through :func:`redact` so no secret leaks (Requirement 2, Property 5,
Property 17).

This module isolates that classification as the pure, side-effect-free function
:func:`classify_response`. It performs **no** retry, backoff, or network access
(16.5, Property 7): given a failure signal it simply returns the error a caller
should raise. ``YouTubeDataSource`` (and the Analytics endpoint, a configured
``KeywordMetricsProvider``, or a ``TemplatePerformanceStrategy`` retrieval) then
``raise``\\s the returned instance, leaving all resilience policy to
``ResilientDataSource``.

A failure signal is one of:

- an :class:`~infrastructure.http_transport.HttpResponse` carrying an HTTP error
  status (the transport returns error statuses as responses, not exceptions);
- an :class:`~infrastructure.http_transport.HttpTimeoutError` (the response did
  not complete within the configured timeout);
- an :class:`~infrastructure.http_transport.HttpTransportError` (a connection
  failure or reset).

The documented precedence, evaluated in order, is:

1. HTTP 429 -> :class:`RateLimitError` (2.1)
2. HTTP 403 with a quota / rate-limit reason -> :class:`RateLimitError` (2.2)
3. HTTP 400 / 401 / 404, or 403 with an auth / permission reason ->
   :class:`NonTransientError` naming the request (2.8)
4. HTTP >= 500 -> :class:`TransientError` (2.7)
5. No complete response within the timeout -> :class:`TimeoutError` (2.5)
6. Connection error / reset -> :class:`TransientError` (2.6)
7. Any other HTTP error status -> :class:`NonTransientError` naming the request
   and the received status (2.11)

A response and a transport error are mutually exclusive signals (a transport
error means no HTTP status was obtained), so steps 1-4/7 apply to a response and
steps 5-6 apply to a transport error; the ordering above is preserved so the
mapping is total and unambiguous (2.10).

Requirements traceability: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10,
2.11; reused for 3.5, 4.4, 5.4.
"""

from __future__ import annotations

import json
import math
from typing import Iterable

from config.secrets import Secret, redact
from infrastructure.datasource import (
    DataSourceError,
    NonTransientError,
    RateLimitError,
    TimeoutError,
    TransientError,
)
from infrastructure.http_transport import (
    HttpResponse,
    HttpTimeoutError,
    HttpTransportError,
)

__all__ = ["classify_response", "RATE_LIMIT_REASONS"]


# YouTube Data API ``error.errors[].reason`` / ``error.status`` values that
# indicate a quota or rate-limit condition (compared case-insensitively). Any
# 403 carrying one of these maps to ``RateLimitError`` (2.2); any other 403
# is treated as an authorization/permission failure (2.8).
RATE_LIMIT_REASONS = frozenset(
    {
        "quotaexceeded",
        "ratelimitexceeded",
        "userratelimitexceeded",
        "dailylimitexceeded",
        "resource_exhausted",
    }
)


def classify_response(
    request_description: str,
    *,
    response: HttpResponse | None = None,
    transport_error: HttpTransportError | None = None,
    secrets: Iterable[Secret] = (),
) -> DataSourceError:
    """Map a single YouTube failure signal to one ``DataSourceError`` member.

    Exactly one of ``response`` (an HTTP error status) or ``transport_error``
    (a timeout or connection failure) describes the signal. ``request_description``
    is a non-secret, human-readable identifier of the failing call (e.g.
    ``"get_channel_metadata for UC123"``) used to name the request in the
    returned error's reason (2.8, 2.11). ``secrets`` are scrubbed from every
    reason via :func:`redact` so no secret value can leak (2.9, Property 17).

    The function is pure: it performs no retry, backoff, sleeping, or network
    access (16.5, Property 7); it returns the error the caller should raise.

    Returns exactly one member of the hierarchy chosen by the documented
    precedence (Property 5). Raises :class:`ValueError` only on misuse (neither
    or both signals supplied).
    """
    if (response is None) == (transport_error is None):
        raise ValueError(
            "classify_response requires exactly one of `response` or "
            "`transport_error`"
        )

    secrets = tuple(secrets)

    # --- Transport-level signals (no HTTP status was obtained) -------------
    if transport_error is not None:
        # A timeout is the more specific signal; check it before the generic
        # transport error (HttpTimeoutError subclasses HttpTransportError).
        if isinstance(transport_error, HttpTimeoutError):
            reason = (
                f"{request_description} did not complete within the request "
                f"timeout"
            )
            return TimeoutError(redact(reason, secrets))
        # Connection error / reset -> transient (2.6).
        reason = f"{request_description} failed: connection error: {transport_error}"
        return TransientError(redact(reason, secrets))

    # --- HTTP error-status signals -----------------------------------------
    assert response is not None  # narrowed by the guard above
    status = response.status
    retry_after = _parse_retry_after(response)

    # 1. HTTP 429 -> RateLimitError (2.1)
    if status == 429:
        reason = f"{request_description} was rate limited (HTTP 429)"
        return RateLimitError(redact(reason, secrets), retry_after_seconds=retry_after)

    # 2. HTTP 403 with a quota / rate-limit reason -> RateLimitError (2.2)
    if status == 403 and _has_quota_reason(response):
        reason = f"{request_description} was rate limited (HTTP 403 quota/rate limit)"
        return RateLimitError(redact(reason, secrets), retry_after_seconds=retry_after)

    # 3. HTTP 400/401/404, or 403 with auth/permission reason ->
    #    NonTransientError naming the request (2.8).
    if status in (400, 401, 404):
        reason = f"{request_description} failed (HTTP {status})"
        return NonTransientError(redact(reason, secrets))
    if status == 403:
        reason = (
            f"{request_description} failed: authorization or permission denied "
            f"(HTTP 403)"
        )
        return NonTransientError(redact(reason, secrets))

    # 4. HTTP >= 500 -> TransientError (2.7)
    if status >= 500:
        reason = f"{request_description} failed: server error (HTTP {status})"
        return TransientError(redact(reason, secrets))

    # 7. Any other HTTP error status -> NonTransientError naming request +
    #    status (2.11).
    reason = f"{request_description} failed (HTTP {status})"
    return NonTransientError(redact(reason, secrets))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_retry_after(response: HttpResponse) -> float | None:
    """Return a numeric ``Retry-After`` seconds value, or ``None`` (2.3, 2.4).

    The lookup is case-insensitive (the header mapping guarantees it). A header
    that is absent, non-numeric (e.g. an HTTP-date), negative, or non-finite
    yields ``None`` so the caller leaves ``retry_after_seconds`` unset.
    """
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except (ValueError, AttributeError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _has_quota_reason(response: HttpResponse) -> bool:
    """Whether a 403 response body reports a quota / rate-limit reason (2.2)."""
    return bool(_extract_reasons(response.body) & RATE_LIMIT_REASONS)


def _extract_reasons(body: bytes) -> set[str]:
    """Extract lower-cased YouTube error reason tokens from a JSON error body.

    Reads ``error.status`` and every ``error.errors[].reason`` from the
    standard Google API error envelope. Returns an empty set when the body is
    absent, not JSON, or not shaped like that envelope, so an unparseable 403
    falls through to the authorization branch.
    """
    if not body:
        return set()
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return set()
    if not isinstance(data, dict):
        return set()
    error = data.get("error")
    if not isinstance(error, dict):
        return set()

    reasons: set[str] = set()
    status = error.get("status")
    if isinstance(status, str):
        reasons.add(status.lower())
    errors = error.get("errors")
    if isinstance(errors, list):
        for entry in errors:
            if isinstance(entry, dict):
                reason = entry.get("reason")
                if isinstance(reason, str):
                    reasons.add(reason.lower())
    return reasons
