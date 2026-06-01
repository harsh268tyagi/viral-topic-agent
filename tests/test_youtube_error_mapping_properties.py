"""Hypothesis property test for the YouTube error mapping (task 4.2).

This module validates a single universal property of the pure
``classify_response`` function (design.md -> *Error Handling* / *YouTube error
mapping*; Property 5), one Hypothesis property test:

- Property 5 (2.1, 2.2, 2.5, 2.6, 2.7, 2.8, 2.10, 2.11, 3.5, 4.4, 5.4): every
  failure signal from a YouTube endpoint maps to exactly one member of the
  ``DataSourceError`` hierarchy, chosen by the documented fixed precedence.

The failure signal is driven through an injected :class:`FakeHttpTransport`
(no real network access, 16.3): an HTTP error status is returned as an
``HttpResponse`` for classification, while a timeout or connection failure is
raised by the fake and caught, exactly as ``YouTubeDataSource`` would. The
property compares the concrete type of the returned error against a small
reference model of the documented precedence table, asserting an exact,
unambiguous mapping (``type(result) is expected``).

Per-status ``retry_after_seconds`` extraction (2.3, 2.4) is covered separately
by Property 6 (task 4.3); this property is scoped to which hierarchy member is
chosen.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

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
from infrastructure.youtube_error_mapping import RATE_LIMIT_REASONS, classify_response

from tests.edge_fakes import FakeHttpTransport


# ---------------------------------------------------------------------------
# Generators (composite strategies)
#
# `_failure_case` is the `error_signal` strategy from the design's Testing
# Strategy: a union of HTTP error statuses (including overlapping cases such as
# 403-quota vs 403-auth and arbitrary unmatched statuses), transport connection
# errors, and non-completing (timeout) responses, with optional Retry-After
# headers. Each generated case carries the `expected_type` its documented
# precedence selects, so the property needs no second engine.
# ---------------------------------------------------------------------------

# 4xx statuses that are not specially handled by criteria 1-3 and so fall to the
# "any other HTTP error status" branch (2.11) -> NonTransientError.
_OTHER_ERROR_STATUSES = [
    402, 405, 406, 407, 408, 409, 410, 411, 412, 413, 414, 415, 416,
    417, 418, 422, 423, 424, 425, 426, 428, 431, 451, 499,
]

# 403 reasons that indicate an auth/permission failure (not quota/rate limit).
# Each must be absent from RATE_LIMIT_REASONS so it maps to NonTransientError.
_AUTH_REASONS = [
    "forbidden",
    "insufficientPermissions",
    "accessNotConfigured",
    "accountSuspended",
]

# Optional Retry-After header values: absent, numeric, and non-numeric. The
# value does not affect which hierarchy member is chosen (Property 5); its
# numeric extraction is Property 6's concern.
_retry_after_headers = st.one_of(
    st.none(),
    st.sampled_from(["0", "30", "120", "12.5"]),
    st.sampled_from(["", "soon", "Wed, 21 Oct 2015 07:28:00 GMT"]),
)


def _quota_body(reason: str, in_status: bool) -> bytes:
    """A Google-API error envelope reporting a quota/rate-limit ``reason``."""
    if in_status:
        envelope = {"error": {"code": 403, "status": reason}}
    else:
        envelope = {"error": {"code": 403, "errors": [{"reason": reason}]}}
    return json.dumps(envelope).encode("utf-8")


@st.composite
def _failure_case(draw: st.DrawFn) -> dict:
    """Draw one failure signal plus the hierarchy member its precedence selects."""
    branch = draw(
        st.sampled_from(
            [
                "rate_429",
                "rate_403_quota",
                "auth_4xx",
                "auth_403",
                "server_5xx",
                "other_status",
                "timeout",
                "connection",
            ]
        )
    )

    if branch == "rate_429":
        return {
            "kind": "http",
            "status": 429,
            "headers": _maybe_retry_after(draw),
            "body": draw(st.sampled_from([b"", b"{}", b'{"error":{"code":429}}'])),
            "expected_type": RateLimitError,
        }

    if branch == "rate_403_quota":
        reason = draw(st.sampled_from(sorted(RATE_LIMIT_REASONS)))
        body = _quota_body(reason, in_status=draw(st.booleans()))
        return {
            "kind": "http",
            "status": 403,
            "headers": _maybe_retry_after(draw),
            "body": body,
            "expected_type": RateLimitError,
        }

    if branch == "auth_4xx":
        return {
            "kind": "http",
            "status": draw(st.sampled_from([400, 401, 404])),
            "headers": {},
            "body": draw(st.sampled_from([b"", b"{}", b"not-json"])),
            "expected_type": NonTransientError,
        }

    if branch == "auth_403":
        # A 403 whose body carries an auth reason, or is empty / unparseable:
        # all fall through to the authorization branch -> NonTransientError.
        body_choice = draw(st.sampled_from(["auth", "empty", "malformed"]))
        if body_choice == "auth":
            reason = draw(st.sampled_from(_AUTH_REASONS))
            body = json.dumps(
                {"error": {"code": 403, "errors": [{"reason": reason}]}}
            ).encode("utf-8")
        elif body_choice == "empty":
            body = b""
        else:
            body = b"<<not valid json>>"
        return {
            "kind": "http",
            "status": 403,
            "headers": {},
            "body": body,
            "expected_type": NonTransientError,
        }

    if branch == "server_5xx":
        return {
            "kind": "http",
            "status": draw(st.integers(min_value=500, max_value=599)),
            "headers": {},
            "body": draw(st.sampled_from([b"", b"server error"])),
            "expected_type": TransientError,
        }

    if branch == "other_status":
        return {
            "kind": "http",
            "status": draw(st.sampled_from(_OTHER_ERROR_STATUSES)),
            "headers": {},
            "body": draw(st.sampled_from([b"", b"{}"])),
            "expected_type": NonTransientError,
        }

    if branch == "timeout":
        return {"kind": "timeout", "expected_type": TimeoutError}

    # branch == "connection"
    return {"kind": "connection", "expected_type": TransientError}


def _maybe_retry_after(draw: st.DrawFn) -> dict:
    raw = draw(_retry_after_headers)
    return {} if raw is None else {"Retry-After": raw}


def _drive_through_transport(case: dict, *, request_description: str):
    """Replay ``case`` through a :class:`FakeHttpTransport` and classify it.

    Mirrors how ``YouTubeDataSource`` consumes the transport: an HTTP error
    status comes back as a response, while a timeout or connection failure is
    raised and caught. Asserts exactly one transport attempt is made (16.5).
    """
    transport = FakeHttpTransport()
    if case["kind"] == "http":
        transport.queue_response(
            HttpResponse(
                status=case["status"], headers=case["headers"], body=case["body"]
            )
        )
    elif case["kind"] == "timeout":
        transport.queue_error(HttpTimeoutError("request did not complete in time"))
    else:
        transport.queue_error(HttpTransportError("connection reset by peer"))

    response = None
    transport_error = None
    try:
        response = transport.request(
            "GET", "https://example.test/v3", headers={}, timeout_seconds=30.0
        )
    except HttpTimeoutError as exc:  # must precede HttpTransportError
        transport_error = exc
    except HttpTransportError as exc:
        transport_error = exc

    # The source never retries internally: exactly one attempt was made.
    assert transport.call_count == 1

    return classify_response(
        request_description,
        response=response,
        transport_error=transport_error,
    )


# Feature: real-provider-integration, Property 5: Every error response maps to exactly one error-hierarchy member by the documented precedence
@settings(max_examples=200)
@given(
    case=_failure_case(),
    request_description=st.text(
        alphabet=st.characters(min_codepoint=97, max_codepoint=122),
        min_size=1,
        max_size=30,
    ),
)
def test_every_failure_signal_maps_to_exactly_one_hierarchy_member(
    case, request_description
):
    """For any YouTube failure signal -- an HTTP error status, a timeout, or a
    connection error/reset -- ``classify_response`` returns exactly one member
    of the ``DataSourceError`` hierarchy chosen by the documented precedence:
    429 / 403-quota -> RateLimitError; 400/401/404 / 403-auth -> NonTransient;
    >= 500 -> Transient; timeout -> TimeoutError; connection -> Transient; any
    other error status -> NonTransient naming the request and status."""
    expected_type = case["expected_type"]

    result = _drive_through_transport(case, request_description=request_description)

    # Exactly one member: the concrete type matches the precedence selection.
    assert isinstance(result, DataSourceError)
    assert type(result) is expected_type

    # The reason always names the failing request (2.8, 2.11) and never leaks.
    assert request_description in result.reason

    # For an HTTP-status signal that resolves to a NonTransientError, the reason
    # also names the received status (2.8, 2.11).
    if case["kind"] == "http" and expected_type is NonTransientError:
        assert str(case["status"]) in result.reason
