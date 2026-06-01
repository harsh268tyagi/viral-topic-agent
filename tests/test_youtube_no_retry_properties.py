"""Hypothesis property test for no internal retry or backoff (task 5.7).

This module validates a single universal property of ``YouTubeDataSource``
(design.md -> *Correctness Properties* -> Property 7):

- Property 7 (Requirement 16.5): for any call that FAILS, ``YouTubeDataSource``
  makes exactly one transport attempt, does NOT call ``Clock.sleep``, and
  signals the failure only by raising a member of the ``DataSourceError``
  hierarchy -- leaving all retry, backoff, and timeout policy to
  ``ResilientDataSource``.

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3): a single failure signal is queued as the one
scripted outcome -- either an HTTP error status (e.g. 429 / 403 / 404 / 500 /
other) returned as an :class:`HttpResponse`, or a transport-level failure
(:class:`HttpTimeoutError` / :class:`HttpTransportError`) raised by the fake.

The injected :class:`Clock` is a real :class:`FakeClock` wrapped in a small
sleep-recording spy defined in this test, so the property can assert directly
that no ``sleep`` (and hence no backoff) ever occurs. The :class:`AuthManager`
is a real one built over the same transport/clock and an :class:`AuthSettings`
carrying a :class:`Secret` API key, since the failing public-data calls only
consume ``auth.data_api_params()`` (no OAuth, no network).

The property asserts, for each failing call:
  (a) it raises a member of the ``DataSourceError`` hierarchy (16.5);
  (b) exactly one transport attempt was made -- no internal retry (16.5);
  (c) no backoff occurred -- ``Clock.sleep`` was never called and virtual time
      did not advance (16.5).

The specific hierarchy member chosen per signal is Property 5's concern (task
4.2); this property is scoped to the no-retry / no-backoff guarantee (16.5).
"""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import Clock, FakeClock
from infrastructure.datasource import DataSourceError
from infrastructure.http_transport import (
    HttpResponse,
    HttpTimeoutError,
    HttpTransportError,
)
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 30.0

# A channel id is any non-empty identifier; it is url-encoded into the request.
_channel_ids = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)

# Optional Retry-After header values for the rate-limit signals; their presence
# does not change the no-retry/no-backoff guarantee being tested here.
_retry_after_headers = st.one_of(
    st.none(),
    st.sampled_from(["0", "30", "120", "soon", ""]),
)

# HTTP error statuses spanning every precedence branch of the error mapping:
# 429 / 403 (rate-limit), 400 / 401 / 404 / 403 (auth), >= 500 (server), and
# assorted "other" error statuses. Every one is a failure signal that must map
# to a DataSourceError without any retry or backoff.
_HTTP_ERROR_STATUSES = [
    429, 403, 400, 401, 404, 500, 502, 503, 402, 405, 409, 418, 422, 451, 499,
]


def _quota_body(status: int) -> bytes:
    """A Google-API error envelope for an error status (realistic payload)."""
    return json.dumps({"error": {"code": status, "status": "ERROR"}}).encode("utf-8")


@st.composite
def _failure_signal(draw: st.DrawFn) -> dict:
    """Draw one failure signal to queue as the transport's single outcome.

    Returns a ``kind`` discriminator plus the data needed to script the
    :class:`FakeHttpTransport`: an HTTP error status (with optional Retry-After
    header and body), a non-completing response (timeout), or a connection
    failure/reset.
    """
    kind = draw(st.sampled_from(["http", "timeout", "connection"]))
    if kind == "http":
        status = draw(st.sampled_from(_HTTP_ERROR_STATUSES))
        raw_retry_after = draw(_retry_after_headers)
        headers = {} if raw_retry_after is None else {"Retry-After": raw_retry_after}
        body = draw(st.sampled_from([b"", b"{}", _quota_body(status), b"not-json"]))
        return {"kind": "http", "status": status, "headers": headers, "body": body}
    return {"kind": kind}


class _SleepSpyClock:
    """A :class:`Clock` wrapper that records every ``sleep`` call.

    Delegates to an inner :class:`FakeClock` while counting the seconds passed
    to each :meth:`sleep`, so a test can assert that no backoff ever happened
    (16.5). It is defined here (not in ``src`` or ``edge_fakes``) purely to give
    Property 7 a direct, unambiguous "sleep was never called" assertion.
    """

    def __init__(self, inner: FakeClock) -> None:
        self._inner = inner
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self._inner.monotonic()

    def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        self._inner.sleep(seconds)

    def advance(self, seconds: float) -> None:  # parity with FakeClock
        self._inner.advance(seconds)


def _queue_failure(transport: FakeHttpTransport, signal: dict) -> None:
    """Queue ``signal`` as the transport's single scripted outcome."""
    if signal["kind"] == "http":
        transport.queue_response(
            HttpResponse(
                status=signal["status"],
                headers=signal["headers"],
                body=signal["body"],
            )
        )
    elif signal["kind"] == "timeout":
        transport.queue_error(HttpTimeoutError("request did not complete in time"))
    else:  # "connection"
        transport.queue_error(HttpTransportError("connection reset by peer"))


def _build_data_source(
    transport: FakeHttpTransport, clock: Clock
) -> YouTubeDataSource:
    """Build a YouTubeDataSource over the transport/clock with a real AuthManager.

    The ``AuthManager`` carries a ``Secret`` API key; the public-data calls
    exercised here only consume ``auth.data_api_params()``, so no OAuth or
    network round-trip is involved.
    """
    auth_settings = AuthSettings(
        youtube_api_key=Secret("api-key-value", CredentialReference("youtube_api_key"))
    )
    auth = AuthManager(auth_settings, transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url=API_BASE_URL,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
    )


# Feature: real-provider-integration, Property 7: The data source performs no internal retry or backoff
# Validates: Requirements 16.5
@settings(max_examples=200)
@given(
    signal=_failure_signal(),
    channel_id=_channel_ids,
    method=st.sampled_from(["get_channel_metadata", "get_videos"]),
)
def test_data_source_performs_no_internal_retry_or_backoff(signal, channel_id, method):
    """For any failing call, ``YouTubeDataSource`` makes exactly one transport
    attempt, never calls ``Clock.sleep`` (no backoff), and signals the failure
    only by raising a member of the ``DataSourceError`` hierarchy (16.5)."""
    transport = FakeHttpTransport()
    _queue_failure(transport, signal)

    inner_clock = FakeClock()
    clock = _SleepSpyClock(inner_clock)
    start_time = clock.monotonic()

    data_source = _build_data_source(transport, clock)
    call = getattr(data_source, method)

    # (a) The failure is signalled only by raising a DataSourceError member.
    with pytest.raises(DataSourceError):
        call(channel_id)

    # (b) Exactly one transport attempt was made -- no internal retry (16.5).
    assert transport.call_count == 1

    # (c) No backoff occurred: Clock.sleep was never called and virtual time
    #     did not advance (16.5). Leaving retry/backoff/timeout policy to
    #     ResilientDataSource means the source itself never pauses.
    assert clock.sleep_calls == []
    assert inner_clock.total_slept == 0.0
    assert clock.monotonic() == start_time
