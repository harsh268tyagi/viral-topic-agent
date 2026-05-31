"""Example/edge-case tests for ``ResilientDataSource`` (task 2.2, Requirement 16).

These tests drive time with :class:`FakeClock` so every pause, backoff, and
timeout is exact and instant. They cover the concrete branches of the
resilience layer:

- rate-limit pause for the reported interval / default 60 s, and the 300 s
  cumulative-pause bound -> ``rate-limit-timeout`` (16.1, 16.5);
- transient retry up to 3 attempts with >= 2 s spacing, recording only after
  exhaustion (16.2);
- the 30 s no-complete-response path treated as transient, via both an inner
  ``TimeoutError`` and elapsed clock time (16.4);
- non-transient errors recorded once with target + reason, no retry (16.3);
- independence: a failed call does not leak state into or block a later
  independent call (16.6, 16.7).

The dedicated Hypothesis property tests for Properties 30-33 live in later
tasks (2.3-2.6); these examples complement them with specific scenarios.
"""

from __future__ import annotations

import pytest

from infrastructure.clock import FakeClock
from infrastructure.datasource import (
    DataOperation,
    DataRequest,
    FailureClassification,
    NonTransientError,
    RateLimitError,
    TimeoutError,
    TransientError,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _return(value):
    """A behavior that returns ``value`` immediately (no clock movement)."""

    def behavior(clock: FakeClock):
        return value

    return behavior


def _raise(exc: Exception):
    """A behavior that raises ``exc``."""

    def behavior(clock: FakeClock):
        raise exc

    return behavior


def _slow_return(value, *, seconds: float):
    """A behavior that consumes ``seconds`` of clock time, then returns ``value``."""

    def behavior(clock: FakeClock):
        clock.advance(seconds)
        return value

    return behavior


class ScriptedSource:
    """A :class:`DataSource` stub that replays a queue of scripted behaviors.

    Each call (regardless of which protocol method) pops the next behavior and
    invokes it with the shared clock, so a test can express an exact sequence
    of successes, raises, and slow responses. ``calls`` records every
    invocation for assertions about attempt counts.
    """

    def __init__(self, clock: FakeClock, behaviors):
        self.clock = clock
        self.behaviors = list(behaviors)
        self.calls: list[tuple[str, dict]] = []

    def _next(self, name: str, kwargs: dict):
        self.calls.append((name, kwargs))
        if not self.behaviors:
            raise AssertionError(f"unexpected extra call to {name}({kwargs})")
        behavior = self.behaviors.pop(0)
        return behavior(self.clock)

    # DataSource protocol methods (only the ones exercised are needed, but all
    # are provided so the stub structurally satisfies the protocol).
    def get_channel_metadata(self, channel_id):
        return self._next("get_channel_metadata", {"channel_id": channel_id})

    def get_videos(self, channel_id, published_within_days=None):
        return self._next(
            "get_videos",
            {"channel_id": channel_id, "published_within_days": published_within_days},
        )

    def get_audience_activity(self, channel_id, days):
        return self._next("get_audience_activity", {"channel_id": channel_id, "days": days})

    def get_keyword_metrics(self, category, max_keywords):
        return self._next(
            "get_keyword_metrics", {"category": category, "max_keywords": max_keywords}
        )

    def get_template_performance(self, category):
        return self._next("get_template_performance", {"category": category})


def _metadata_request(channel_id: str = "chan-1") -> DataRequest:
    return DataRequest(
        operation=DataOperation.CHANNEL_METADATA,
        target=channel_id,
        params={"channel_id": channel_id},
    )


def _make(clock: FakeClock, behaviors, policy: RetryPolicy | None = None):
    source = ScriptedSource(clock, behaviors)
    rds = ResilientDataSource(source, policy or RetryPolicy(), clock)
    return rds, source


# ---------------------------------------------------------------------------
# RetryPolicy defaults / validation
# ---------------------------------------------------------------------------


def test_retry_policy_documented_defaults():
    policy = RetryPolicy()
    assert policy.max_attempts == 3
    assert policy.min_backoff_seconds == 2.0
    assert policy.request_timeout_seconds == 30.0
    assert policy.default_rate_limit_pause_seconds == 60.0
    assert policy.max_total_pause_seconds == 300.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"min_backoff_seconds": -1.0},
        {"request_timeout_seconds": 0.0},
        {"default_rate_limit_pause_seconds": 0.0},
        {"max_total_pause_seconds": -5.0},
    ],
)
def test_retry_policy_rejects_invalid_values(kwargs):
    with pytest.raises(ValueError):
        RetryPolicy(**kwargs)


# ---------------------------------------------------------------------------
# Success path
# ---------------------------------------------------------------------------


def test_successful_call_returns_ok_without_sleeping():
    clock = FakeClock()
    rds, source = _make(clock, [_return("payload")])

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert result.unwrap() == "payload"
    assert len(source.calls) == 1
    assert clock.total_slept == 0.0


# ---------------------------------------------------------------------------
# Rate limit (16.1, 16.5)
# ---------------------------------------------------------------------------


def test_rate_limit_pauses_for_reported_interval_then_resumes():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(RateLimitError(retry_after_seconds=120.0)), _return("ok")],
    )

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert result.unwrap() == "ok"
    # Paused for exactly the reported interval, then succeeded on the 2nd call.
    assert clock.total_slept == 120.0
    assert len(source.calls) == 2


def test_rate_limit_uses_default_pause_when_none_reported():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(RateLimitError(retry_after_seconds=None)), _return("ok")],
    )

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert clock.total_slept == 60.0  # default_rate_limit_pause_seconds


def test_rate_limit_records_timeout_when_cumulative_pause_exceeds_bound():
    clock = FakeClock()
    # Six rate-limit responses at the default 60s pause: 5 pauses reach exactly
    # 300s; the 6th would exceed 300s, triggering a rate-limit-timeout.
    rds, source = _make(clock, [_raise(RateLimitError()) for _ in range(6)])

    result = rds.call(_metadata_request("chan-X"))

    assert result.is_err()
    failure = result.unwrap_err()
    assert failure.classification is FailureClassification.RATE_LIMIT_TIMEOUT
    assert failure.target == "chan-X"
    assert "rate-limit timeout" in failure.reason
    # Five 60s pauses were actually performed before giving up at the bound.
    assert clock.total_slept == 300.0


def test_rate_limit_timeout_with_reported_interval_does_not_overshoot():
    clock = FakeClock()
    # Reported 200s interval: first pause (200) is within budget; a second
    # would reach 400 > 300, so we stop without performing the over-budget wait.
    rds, source = _make(clock, [_raise(RateLimitError(retry_after_seconds=200.0)) for _ in range(2)])

    result = rds.call(_metadata_request())

    assert result.is_err()
    assert result.unwrap_err().classification is FailureClassification.RATE_LIMIT_TIMEOUT
    assert clock.total_slept == 200.0  # only the in-budget pause happened


# ---------------------------------------------------------------------------
# Transient (16.2, 16.4)
# ---------------------------------------------------------------------------


def test_transient_retries_up_to_three_attempts_then_records_failure():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(TransientError("connection reset")) for _ in range(3)],
    )

    result = rds.call(_metadata_request("chan-T"))

    assert result.is_err()
    failure = result.unwrap_err()
    assert failure.classification is FailureClassification.TRANSIENT
    assert failure.target == "chan-T"
    assert failure.reason == "connection reset"
    assert failure.attempts == 3
    # Exactly 3 inner calls and 2 backoff gaps of >= 2s between them.
    assert len(source.calls) == 3
    assert clock.total_slept == 4.0  # two 2s gaps


def test_transient_then_success_recovers_within_attempt_bound():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(TransientError("temporary unavailable")), _return("recovered")],
    )

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert result.unwrap() == "recovered"
    assert len(source.calls) == 2
    assert clock.total_slept == 2.0  # single backoff before the successful retry


def test_no_response_within_timeout_is_treated_as_transient_via_timeout_error():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(TimeoutError("no response in time")) for _ in range(3)],
    )

    result = rds.call(_metadata_request())

    assert result.is_err()
    assert result.unwrap_err().classification is FailureClassification.TRANSIENT
    assert result.unwrap_err().attempts == 3


def test_slow_response_exceeding_timeout_is_treated_as_transient_then_recovers():
    clock = FakeClock()
    # First call consumes 31s (> 30s timeout) before returning -> treated as a
    # transient timeout; second call returns promptly -> success.
    rds, source = _make(
        clock,
        [_slow_return("ignored-late", seconds=31.0), _return("fast")],
    )

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert result.unwrap() == "fast"
    assert len(source.calls) == 2
    # 31s elapsed during the slow call + a single 2s backoff before the retry.
    assert clock.total_slept == 2.0
    assert clock.monotonic() == pytest.approx(33.0)


def test_response_just_within_timeout_is_accepted():
    clock = FakeClock()
    rds, source = _make(clock, [_slow_return("on-time", seconds=30.0)])

    result = rds.call(_metadata_request())

    assert result.is_ok()
    assert result.unwrap() == "on-time"
    assert len(source.calls) == 1


# ---------------------------------------------------------------------------
# Non-transient (16.3, 16.6)
# ---------------------------------------------------------------------------


def test_non_transient_recorded_once_with_target_and_reason_no_retry():
    clock = FakeClock()
    rds, source = _make(
        clock,
        [_raise(NonTransientError("authentication rejected"))],
    )

    result = rds.call(_metadata_request("chan-NT"))

    assert result.is_err()
    failure = result.unwrap_err()
    assert failure.classification is FailureClassification.NON_TRANSIENT
    assert failure.target == "chan-NT"
    assert failure.reason == "authentication rejected"
    assert failure.attempts == 1
    # Exactly one attempt, no retry, no backoff.
    assert len(source.calls) == 1
    assert clock.total_slept == 0.0


# ---------------------------------------------------------------------------
# Independence / isolation (16.7)
# ---------------------------------------------------------------------------


def test_failed_call_does_not_block_or_leak_into_independent_call():
    clock = FakeClock()
    # First request fails (non-transient); a second, independent request on the
    # same decorator must still succeed and start with a clean attempt budget.
    rds, source = _make(
        clock,
        [
            _raise(NonTransientError("invalid request")),
            _return("second-ok"),
        ],
    )

    first = rds.call(_metadata_request("chan-A"))
    second = rds.call(
        DataRequest(
            operation=DataOperation.VIDEOS,
            target="chan-B",
            params={"channel_id": "chan-B"},
        )
    )

    assert first.is_err()
    assert first.unwrap_err().target == "chan-A"
    assert second.is_ok()
    assert second.unwrap() == "second-ok"


def test_rate_limit_state_does_not_persist_across_independent_calls():
    clock = FakeClock()
    # A first call that times out on cumulative rate-limit must not leave the
    # decorator "primed" — the next call gets a fresh 300s budget.
    rds, source = _make(
        clock,
        [
            *[_raise(RateLimitError()) for _ in range(6)],  # first call -> rate-limit-timeout
            _raise(RateLimitError(retry_after_seconds=60.0)),  # second call: one pause...
            _return("ok-after-pause"),  # ...then success
        ],
    )

    first = rds.call(_metadata_request("chan-1"))
    assert first.is_err()
    assert first.unwrap_err().classification is FailureClassification.RATE_LIMIT_TIMEOUT

    second = rds.call(_metadata_request("chan-2"))
    assert second.is_ok()
    assert second.unwrap() == "ok-after-pause"
