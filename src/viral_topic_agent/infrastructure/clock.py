"""An injectable ``Clock`` abstraction for deterministic time control.

The design (``.kiro/specs/viral-topic-agent/design.md`` -> *Technology
Choices* and *ResilientDataSource*) calls for a pluggable ``Clock`` so that
time-dependent behavior - retry backoff spacing, the 30 s request timeout, the
60 s rate-limit pause, the 300 s cumulative-pause bound, and scheduling - is
testable without real waits.

Two implementations are provided:

- :class:`RealClock` delegates to the system monotonic clock and a real sleep.
- :class:`FakeClock` keeps a virtual "now" that only advances when explicitly
  told to, or when :meth:`FakeClock.sleep` is called. This lets tests drive
  retry counts, backoff spacing, and timeouts deterministically and instantly.

Time is measured in seconds as a ``float``. ``monotonic`` returns a value that
never decreases, which is what the resilience layer needs for measuring
elapsed time and cumulative pause. (Wall-clock semantics are intentionally not
modeled here; scheduling concerns layer on top.)

Requirements traceability: supports 16.1, 16.2, 16.4, 16.5 (and scheduling in
Requirement 14) by making the passage of time injectable.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "RealClock", "FakeClock"]


@runtime_checkable
class Clock(Protocol):
    """A source of monotonic time and a way to wait.

    Implementations must guarantee that successive calls to :meth:`monotonic`
    never return a smaller value, and that calling :meth:`sleep` advances the
    value returned by :meth:`monotonic` by at least ``seconds``.
    """

    def monotonic(self) -> float:
        """Return a monotonically non-decreasing time in seconds."""
        ...

    def sleep(self, seconds: float) -> None:
        """Wait for (at least) ``seconds``; advances :meth:`monotonic`."""
        ...


class RealClock:
    """A :class:`Clock` backed by the system clock and real sleeping."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("sleep() seconds must be non-negative")
        if seconds:
            time.sleep(seconds)


class FakeClock:
    """A deterministic :class:`Clock` whose time only moves on demand.

    The virtual clock starts at ``start`` (default ``0.0``). Time advances
    only when :meth:`sleep` or :meth:`advance` is called, so tests can assert
    on exact elapsed time, backoff spacing, and timeout boundaries without any
    real delay.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        # Total virtual time slept, useful for assertions about cumulative
        # pause (e.g. the 300 s rate-limit bound in 16.5).
        self._total_slept = 0.0

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` (must be non-negative)."""
        self.advance(seconds)
        self._total_slept += float(seconds)

    def advance(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` without counting it as sleep.

        Useful for simulating elapsed work (e.g. a request that takes longer
        than the 30 s timeout) independently of explicit pauses.
        """
        if seconds < 0:
            raise ValueError("cannot advance time by a negative amount")
        self._now += float(seconds)

    def set_time(self, value: float) -> None:
        """Set virtual time to ``value`` (must not move time backwards)."""
        if value < self._now:
            raise ValueError("FakeClock time must not move backwards")
        self._now = float(value)

    @property
    def total_slept(self) -> float:
        """Total virtual seconds spent in :meth:`sleep` calls."""
        return self._total_slept
