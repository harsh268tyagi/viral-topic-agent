"""The ``ResilientDataSource`` decorator and its ``RetryPolicy`` (Requirement 16).

Every external ``DataSource`` access in the system is funnelled through this
single decorator (``.kiro/specs/viral-topic-agent/design.md`` ->
*ResilientDataSource (infrastructure)*). It is the one place that classifies
errors, applies rate-limit backoff, bounds transient retries, enforces the
request timeout, and records failures - so that no caller needs exception
handling and a single failure never blocks unrelated work.

The two public types are:

- :class:`RetryPolicy` - a frozen dataclass carrying the documented defaults
  (3 attempts, >= 2 s backoff, 30 s request timeout, 60 s default rate-limit
  pause, 300 s cumulative-pause bound).
- :class:`ResilientDataSource` - wraps an inner :class:`DataSource` and exposes
  a single :meth:`~ResilientDataSource.call` returning
  ``Result[Any, DataSourceFailure]``.

Time is taken from an injected :class:`Clock`, so every pause, backoff, and
timeout is driven by virtual time under :class:`FakeClock` in tests and by real
time in production - no behavior branch differs between the two.

How the 30 s timeout (16.4) is detected
---------------------------------------
A synchronous Python call cannot be pre-emptively cancelled, so the timeout is
modeled in two complementary, ``FakeClock``-testable ways:

1. The inner source may raise :class:`datasource.TimeoutError` (a
   ``TransientError``) when it gives up waiting for a response. This is the
   primary mechanism for a source that enforces its own deadline.
2. ``ResilientDataSource`` measures the elapsed clock time around each inner
   call. If a call consumes more than ``request_timeout_seconds`` of clock time
   (even when it ultimately returns), the response is treated as not having
   completed in time and is classified as a transient timeout.

Both paths converge on the same transient-retry logic. In tests, a stub that
``clock.advance(31)`` before returning exercises path (2); a stub that raises
``TimeoutError`` exercises path (1).

Requirements traceability: 16.1 (rate-limit pause + default 60 s), 16.2
(transient retry up to 3 attempts, >= 2 s spacing), 16.3 (non-transient
recorded immediately, no retry), 16.4 (no response in 30 s is transient),
16.5 (cumulative pause > 300 s -> rate-limit-timeout), 16.6 (failures carry
target + reason), 16.7 (each call is independent).
"""

from __future__ import annotations

from dataclasses import dataclass

from viral_topic_agent.infrastructure.clock import Clock
from viral_topic_agent.infrastructure.datasource import (
    DataRequest,
    DataSource,
    DataSourceError,
    DataSourceFailure,
    FailureClassification,
    NonTransientError,
    RateLimitError,
    TransientError,
)
from viral_topic_agent.infrastructure.result import Err, Ok, Result
from typing import Any

__all__ = ["RetryPolicy", "ResilientDataSource"]


@dataclass(frozen=True)
class RetryPolicy:
    """Tunable bounds for :class:`ResilientDataSource` (Requirement 16 defaults).

    All values are in seconds except :attr:`max_attempts`, which is a count of
    total invocations (the first try plus retries) permitted for a transiently
    failing request.
    """

    max_attempts: int = 3  # total transient attempts incl. the first (16.2)
    min_backoff_seconds: float = 2.0  # minimum wait between transient retries (16.2)
    request_timeout_seconds: float = 30.0  # no complete response within this -> transient (16.4)
    default_rate_limit_pause_seconds: float = 60.0  # pause when none reported (16.1)
    max_total_pause_seconds: float = 300.0  # cumulative rate-limit pause bound (16.5)

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.min_backoff_seconds < 0:
            raise ValueError("min_backoff_seconds must be non-negative")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self.default_rate_limit_pause_seconds <= 0:
            raise ValueError("default_rate_limit_pause_seconds must be positive")
        if self.max_total_pause_seconds <= 0:
            raise ValueError("max_total_pause_seconds must be positive")


class ResilientDataSource:
    """Decorates a :class:`DataSource`, centralizing Requirement 16 behavior.

    A single :meth:`call` classifies whatever the inner source raises and
    returns a :class:`Result`: ``Ok`` with the payload on success, or ``Err``
    carrying a :class:`DataSourceFailure` (target + reason + classification)
    when the request is ultimately recorded as failed. ``call`` never raises a
    :class:`DataSourceError`; failures are data so callers branch without
    try/except and so one failed request cannot abort independent ones (16.7).
    """

    def __init__(self, inner: DataSource, policy: RetryPolicy, clock: Clock) -> None:
        self.inner = inner
        self.policy = policy
        self.clock = clock

    def call(self, request: DataRequest) -> Result[Any, DataSourceFailure]:
        """Invoke ``request`` against the inner source with full resilience.

        Returns ``Ok(payload)`` on success, otherwise ``Err(DataSourceFailure)``
        after applying the rate-limit, transient-retry, timeout, and
        non-transient rules described in this module's docstring.
        """
        policy = self.policy
        target = request.target

        invocations = 0  # total inner calls made (reported as ``attempts``)
        transient_attempts = 0  # counts only transient/timeout tries (bounded by max_attempts)
        cumulative_pause = 0.0  # total rate-limit pause so far (bounded by max_total_pause)

        while True:
            invocations += 1
            started_at = self.clock.monotonic()
            try:
                payload = request.invoke(self.inner)
            except RateLimitError as exc:
                # 16.1 / 16.5: pause for the reported interval (or the default
                # when none/non-positive is reported), accumulating toward the
                # cumulative bound. If the next pause would push the total over
                # the bound, stop and record a rate-limit-timeout instead of
                # performing a wasteful over-budget wait.
                pause = exc.retry_after_seconds
                if pause is None or pause <= 0:
                    pause = policy.default_rate_limit_pause_seconds
                if cumulative_pause + pause > policy.max_total_pause_seconds:
                    return Err(
                        DataSourceFailure(
                            target=target,
                            reason=(
                                "rate-limit timeout: cumulative pause would exceed "
                                f"{policy.max_total_pause_seconds:g}s "
                                f"(reason: {exc.reason})"
                            ),
                            classification=FailureClassification.RATE_LIMIT_TIMEOUT,
                            attempts=invocations,
                        )
                    )
                cumulative_pause += pause
                self.clock.sleep(pause)
                continue  # rate-limit retries are bounded by pause budget, not attempts
            except NonTransientError as exc:
                # 16.3: record immediately with target + reason, no retry.
                return Err(
                    DataSourceFailure(
                        target=target,
                        reason=exc.reason,
                        classification=FailureClassification.NON_TRANSIENT,
                        attempts=invocations,
                    )
                )
            except TransientError as exc:
                # 16.2 / 16.4: TransientError (and its TimeoutError subtype) is
                # retryable. Record only after attempts are exhausted.
                transient_attempts += 1
                if transient_attempts >= policy.max_attempts:
                    return Err(
                        DataSourceFailure(
                            target=target,
                            reason=exc.reason,
                            classification=FailureClassification.TRANSIENT,
                            attempts=invocations,
                        )
                    )
                self.clock.sleep(policy.min_backoff_seconds)
                continue
            except DataSourceError as exc:
                # Any other DataSourceError subtype is treated conservatively as
                # non-transient (recorded once, no retry) rather than risk an
                # unbounded retry loop on an unclassified error.
                return Err(
                    DataSourceFailure(
                        target=target,
                        reason=exc.reason,
                        classification=FailureClassification.NON_TRANSIENT,
                        attempts=invocations,
                    )
                )

            # No exception: verify the response completed within the timeout.
            elapsed = self.clock.monotonic() - started_at
            if elapsed > policy.request_timeout_seconds:
                # 16.4: no complete response within the budget -> transient.
                transient_attempts += 1
                if transient_attempts >= policy.max_attempts:
                    return Err(
                        DataSourceFailure(
                            target=target,
                            reason=(
                                "no complete response within "
                                f"{policy.request_timeout_seconds:g}s"
                            ),
                            classification=FailureClassification.TRANSIENT,
                            attempts=invocations,
                        )
                    )
                self.clock.sleep(policy.min_backoff_seconds)
                continue

            return Ok(payload)
