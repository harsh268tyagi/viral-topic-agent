"""Hypothesis property tests for ``ResilientDataSource`` (tasks 2.3-2.6).

Example-based and edge-case scenarios live in ``test_resilient_data_source.py``.
This module validates the four universal properties of the resilience layer
(design.md -> Properties 30-33), one Hypothesis property test each:

- Property 30 (16.1, 16.5): rate-limit pause interval + cumulative-pause bound.
- Property 31 (16.2, 16.4): transient retry bound + minimum spacing.
- Property 32 (16.3, 16.6): non-transient recorded once with target + reason.
- Property 33 (16.7): a failed request does not block independent requests.

Every property drives time with :class:`FakeClock`, so pauses, backoff, and the
30 s timeout are exact and instant. Generators are constrained to the input
space each property is scoped to, and each property compares the observed
outcome against a small reference model of the documented policy.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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
#
# ``RecordingSource`` replays a queue of scripted behaviors and records the
# clock time of every invocation, so a property can assert on attempt counts
# and the spacing between successive retries. Each behavior is a callable that
# receives the shared clock and either returns a payload, raises a
# DataSourceError, or consumes clock time to model a slow response.
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


class RecordingSource:
    """A :class:`DataSource` stub that replays scripted behaviors in order.

    Each call (regardless of which protocol method) records the current clock
    time and pops the next behavior, invoking it with the shared clock. The
    recorded ``call_times`` let a property assert that successive retries are
    spaced by at least the configured minimum backoff.
    """

    def __init__(self, clock: FakeClock, behaviors):
        self.clock = clock
        self.behaviors = list(behaviors)
        self.calls: list[str] = []
        self.call_times: list[float] = []

    def _next(self, name: str):
        self.calls.append(name)
        self.call_times.append(self.clock.monotonic())
        if not self.behaviors:
            raise AssertionError(f"unexpected extra call to {name}")
        return self.behaviors.pop(0)(self.clock)

    def get_channel_metadata(self, channel_id):
        return self._next("get_channel_metadata")

    def get_videos(self, channel_id, published_within_days=None):
        return self._next("get_videos")

    def get_audience_activity(self, channel_id, days):
        return self._next("get_audience_activity")

    def get_keyword_metrics(self, category, max_keywords):
        return self._next("get_keyword_metrics")

    def get_template_performance(self, category):
        return self._next("get_template_performance")


def _metadata_request(target: str) -> DataRequest:
    return DataRequest(
        operation=DataOperation.CHANNEL_METADATA,
        target=target,
        params={"channel_id": target},
    )


def _make(clock: FakeClock, behaviors, policy: RetryPolicy | None = None):
    source = RecordingSource(clock, behaviors)
    rds = ResilientDataSource(source, policy or RetryPolicy(), clock)
    return rds, source


# ---------------------------------------------------------------------------
# Property 30 (task 2.3): rate-limit pause interval + cumulative-pause bound
# ---------------------------------------------------------------------------

# Reported retry intervals: ``None`` -> default 60 s pause; a non-positive value
# -> default 60 s pause; a positive value -> that exact pause. ``max_size`` is
# bounded only to keep generation cheap; FakeClock makes any pause instant.
_intervals = st.lists(
    st.one_of(
        st.none(),
        st.floats(min_value=0.0, max_value=400.0, allow_nan=False, allow_infinity=False),
    ),
    min_size=1,
    max_size=10,
)


def _simulate_rate_limit(intervals, policy: RetryPolicy):
    """Reference model of the rate-limit pause loop.

    Returns ``("timeout", total_paused)`` when a pause would push the cumulative
    pause past the bound, otherwise ``("ok", total_paused)`` once every reported
    interval has been paused for and the request finally succeeds.
    """
    cumulative = 0.0
    for raw in intervals:
        pause = raw if (raw is not None and raw > 0) else policy.default_rate_limit_pause_seconds
        if cumulative + pause > policy.max_total_pause_seconds:
            return "timeout", cumulative
        cumulative += pause
    return "ok", cumulative


# Feature: viral-topic-agent, Property 30: Rate-limited requests pause for the correct interval and time out at the cumulative bound
@settings(max_examples=200)
@given(target=st.text(min_size=1, max_size=20), intervals=_intervals)
def test_rate_limit_pauses_for_interval_and_times_out_at_cumulative_bound(target, intervals):
    """For any sequence of rate-limit responses, the decorator pauses for each
    reported interval (or the 60 s default when none/non-positive is reported)
    and records a ``rate-limit-timeout`` naming the target once the cumulative
    pause would exceed 300 s (16.1, 16.5)."""
    policy = RetryPolicy()
    expected_outcome, expected_paused = _simulate_rate_limit(intervals, policy)

    clock = FakeClock()
    behaviors = [_raise(RateLimitError(retry_after_seconds=raw)) for raw in intervals]
    behaviors.append(_return("payload"))  # consumed only if no timeout occurs
    rds, _ = _make(clock, behaviors, policy)

    result = rds.call(_metadata_request(target))

    # The cumulative pause never exceeds the bound, and equals the modeled total.
    assert clock.total_slept <= policy.max_total_pause_seconds + 1e-9
    assert clock.total_slept == pytest.approx(expected_paused)

    if expected_outcome == "ok":
        assert result.is_ok()
        assert result.unwrap() == "payload"
    else:
        assert result.is_err()
        failure = result.unwrap_err()
        assert failure.classification is FailureClassification.RATE_LIMIT_TIMEOUT
        assert failure.target == target
        assert "rate-limit timeout" in failure.reason


# ---------------------------------------------------------------------------
# Property 31 (task 2.4): transient retry bound + minimum spacing
# ---------------------------------------------------------------------------

# A "transient producer" is any behavior the layer must treat as a transient
# failure: an explicit TransientError, its TimeoutError subtype, or a response
# that completes only after exceeding the 30 s timeout (16.4).
_TRANSIENT_KINDS = ("transient", "timeout", "slow")


def _transient_behavior(kind: str):
    if kind == "transient":
        return _raise(TransientError("temporary unavailable"))
    if kind == "timeout":
        return _raise(TimeoutError("no response in time"))
    return _slow_return("late", seconds=31.0)  # > 30 s request timeout


@st.composite
def _transient_scenario(draw: st.DrawFn):
    """A scenario that either recovers within the attempt bound or exhausts it.

    When ``succeed`` is true, fewer than ``max_attempts`` transient failures
    precede a success. Otherwise exactly ``max_attempts`` transient failures
    occur so the request exhausts its budget and records a failure.
    """
    policy = RetryPolicy()
    succeed = draw(st.booleans())
    if succeed:
        n = draw(st.integers(min_value=0, max_value=policy.max_attempts - 1))
    else:
        n = policy.max_attempts
    kinds = [draw(st.sampled_from(_TRANSIENT_KINDS)) for _ in range(n)]
    return succeed, kinds


# Feature: viral-topic-agent, Property 31: Transient failures are retried within bound with minimum spacing
@settings(max_examples=200)
@given(scenario=_transient_scenario())
def test_transient_failures_retry_within_bound_with_minimum_spacing(scenario):
    """For any run of transient errors (including 30 s no-response), the
    decorator makes at most 3 total attempts, waits at least 2 s between
    successive attempts, and records a transient failure only after the bound is
    exhausted (16.2, 16.4)."""
    succeed, kinds = scenario
    policy = RetryPolicy()

    clock = FakeClock()
    behaviors = [_transient_behavior(kind) for kind in kinds]
    if succeed:
        behaviors.append(_return("recovered"))
    rds, source = _make(clock, behaviors, policy)

    result = rds.call(_metadata_request("chan-T"))

    # Bound: never more than max_attempts inner calls.
    assert len(source.calls) <= policy.max_attempts

    # Minimum spacing: every successive attempt starts >= 2 s after the prior one.
    for earlier, later in zip(source.call_times, source.call_times[1:]):
        assert later - earlier >= policy.min_backoff_seconds - 1e-9

    if succeed:
        # Recovered within the bound; failure recorded only after exhaustion, so
        # none is recorded here.
        assert result.is_ok()
        assert result.unwrap() == "recovered"
        assert len(source.calls) == len(kinds) + 1
    else:
        assert result.is_err()
        failure = result.unwrap_err()
        assert failure.classification is FailureClassification.TRANSIENT
        assert failure.attempts == policy.max_attempts
        assert len(source.calls) == policy.max_attempts


# ---------------------------------------------------------------------------
# Property 32 (task 2.5): non-transient recorded once with target + reason
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 32: Non-transient failures are recorded once with target and reason
@settings(max_examples=200)
@given(
    target=st.text(min_size=1, max_size=40),
    reason=st.text(min_size=1, max_size=80),
)
def test_non_transient_failures_recorded_once_with_target_and_reason(target, reason):
    """For any non-transient error, the decorator makes exactly one attempt and
    records a failure carrying the request target and the failure reason, with
    no retry and no backoff (16.3, 16.6)."""
    clock = FakeClock()
    rds, source = _make(clock, [_raise(NonTransientError(reason))])

    result = rds.call(_metadata_request(target))

    assert result.is_err()
    failure = result.unwrap_err()
    assert failure.classification is FailureClassification.NON_TRANSIENT
    assert failure.target == target
    assert failure.reason == reason
    assert failure.attempts == 1
    # Exactly one attempt: no retry, no backoff pause.
    assert len(source.calls) == 1
    assert clock.total_slept == 0.0


# ---------------------------------------------------------------------------
# Property 33 (task 2.6): a failed request does not block independent requests
# ---------------------------------------------------------------------------


@st.composite
def _batch_outcomes(draw: st.DrawFn):
    """A batch of per-request outcomes (True = success, False = failure) with at
    least one failure, so the property always exercises a recorded failure."""
    n = draw(st.integers(min_value=1, max_value=8))
    outcomes = draw(st.lists(st.booleans(), min_size=n, max_size=n))
    if all(outcomes):
        outcomes[draw(st.integers(min_value=0, max_value=n - 1))] = False
    return outcomes


# Feature: viral-topic-agent, Property 33: A failed request does not block independent requests
@settings(max_examples=200)
@given(outcomes=_batch_outcomes())
def test_failed_request_does_not_block_independent_requests(outcomes):
    """For any batch in which at least one request fails, every independent
    request still produces exactly its own correct result: successes return
    their own payload and failures name their own target, with no cross-request
    leakage (16.7)."""
    clock = FakeClock()
    behaviors = []
    for i, ok in enumerate(outcomes):
        if ok:
            behaviors.append(_return(f"ok-{i}"))
        else:
            behaviors.append(_raise(NonTransientError(f"reason-{i}")))
    rds, _ = _make(clock, behaviors)

    results = [rds.call(_metadata_request(f"target-{i}")) for i in range(len(outcomes))]

    for i, ok in enumerate(outcomes):
        result = results[i]
        if ok:
            assert result.is_ok()
            assert result.unwrap() == f"ok-{i}"
        else:
            assert result.is_err()
            failure = result.unwrap_err()
            assert failure.classification is FailureClassification.NON_TRANSIENT
            assert failure.target == f"target-{i}"
            assert failure.reason == f"reason-{i}"
