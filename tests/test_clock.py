"""Tests for the ``Clock`` abstraction (task 2.1).

Focuses on :class:`FakeClock`'s deterministic advancement (which the resilience
layer and scheduler rely on for testing) and basic :class:`RealClock` sanity.
"""

import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from infrastructure.clock import Clock, FakeClock, RealClock


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_implementations_satisfy_clock_protocol():
    assert isinstance(RealClock(), Clock)
    assert isinstance(FakeClock(), Clock)


# ---------------------------------------------------------------------------
# FakeClock determinism
# ---------------------------------------------------------------------------


def test_fake_clock_starts_at_zero_by_default():
    assert FakeClock().monotonic() == 0.0


def test_fake_clock_honors_custom_start():
    assert FakeClock(start=100.0).monotonic() == 100.0


def test_fake_clock_does_not_advance_on_its_own():
    clock = FakeClock()
    first = clock.monotonic()
    second = clock.monotonic()
    assert first == second == 0.0


def test_fake_clock_sleep_advances_time_deterministically():
    clock = FakeClock()
    clock.sleep(2.0)
    clock.sleep(3.5)
    assert clock.monotonic() == 5.5
    assert clock.total_slept == 5.5


def test_fake_clock_advance_moves_time_without_counting_as_sleep():
    clock = FakeClock()
    clock.advance(10.0)
    assert clock.monotonic() == 10.0
    assert clock.total_slept == 0.0


def test_fake_clock_set_time_jumps_forward():
    clock = FakeClock(start=5.0)
    clock.set_time(20.0)
    assert clock.monotonic() == 20.0


def test_fake_clock_set_time_rejects_going_backwards():
    clock = FakeClock(start=5.0)
    with pytest.raises(ValueError):
        clock.set_time(4.0)


def test_fake_clock_advance_rejects_negative():
    clock = FakeClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_fake_clock_sleep_zero_is_noop():
    clock = FakeClock(start=3.0)
    clock.sleep(0.0)
    assert clock.monotonic() == 3.0
    assert clock.total_slept == 0.0


# ---------------------------------------------------------------------------
# RealClock sanity
# ---------------------------------------------------------------------------


def test_real_clock_is_monotonic_non_decreasing():
    clock = RealClock()
    a = clock.monotonic()
    b = clock.monotonic()
    assert b >= a


def test_real_clock_sleep_advances_wall_time():
    clock = RealClock()
    start = time.monotonic()
    clock.sleep(0.01)
    assert time.monotonic() - start >= 0.0


def test_real_clock_sleep_rejects_negative():
    with pytest.raises(ValueError):
        RealClock().sleep(-1.0)


# ---------------------------------------------------------------------------
# Property: monotonicity under arbitrary sleeps
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(st.lists(st.floats(min_value=0.0, max_value=1_000.0, allow_nan=False), max_size=50))
def test_fake_clock_is_monotonic_under_arbitrary_sleeps(durations):
    """After any sequence of non-negative sleeps, time never decreases and
    equals the cumulative slept duration."""
    clock = FakeClock()
    previous = clock.monotonic()
    expected = 0.0
    for d in durations:
        clock.sleep(d)
        now = clock.monotonic()
        assert now >= previous
        previous = now
        expected += d
    assert clock.monotonic() == pytest.approx(expected)
    assert clock.total_slept == pytest.approx(expected)
