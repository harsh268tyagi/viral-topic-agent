"""Tests for the ``Result[T, E]`` type (task 2.1).

Covers construction, inspection, accessors, extraction, transformation, and
value equality for both the :class:`Ok` and :class:`Err` variants.
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.infrastructure.result import Err, Ok, UnwrapError


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------


def test_ok_is_ok_and_not_err():
    r = Ok(42)
    assert r.is_ok() is True
    assert r.is_err() is False


def test_err_is_err_and_not_ok():
    r = Err("boom")
    assert r.is_err() is True
    assert r.is_ok() is False


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def test_ok_value_returns_payload():
    assert Ok(7).value == 7


def test_ok_error_raises():
    with pytest.raises(UnwrapError):
        _ = Ok(7).error


def test_err_error_returns_payload():
    assert Err("nope").error == "nope"


def test_err_value_raises():
    with pytest.raises(UnwrapError):
        _ = Err("nope").value


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def test_ok_unwrap_returns_value():
    assert Ok("data").unwrap() == "data"


def test_ok_unwrap_err_raises():
    with pytest.raises(UnwrapError):
        Ok("data").unwrap_err()


def test_err_unwrap_raises():
    with pytest.raises(UnwrapError):
        Err("fail").unwrap()


def test_err_unwrap_err_returns_error():
    assert Err("fail").unwrap_err() == "fail"


def test_unwrap_or_uses_value_for_ok_and_default_for_err():
    assert Ok(1).unwrap_or(99) == 1
    assert Err("e").unwrap_or(99) == 99


# ---------------------------------------------------------------------------
# Transformation
# ---------------------------------------------------------------------------


def test_map_transforms_ok_only():
    assert Ok(2).map(lambda x: x * 10) == Ok(20)
    # map is a no-op on Err and must not invoke the function.
    assert Err("e").map(lambda x: pytest.fail("must not be called")) == Err("e")


def test_map_err_transforms_err_only():
    assert Err("e").map_err(lambda s: s.upper()) == Err("E")
    # map_err is a no-op on Ok and must not invoke the function.
    assert Ok(5).map_err(lambda s: pytest.fail("must not be called")) == Ok(5)


# ---------------------------------------------------------------------------
# Value equality / hashing
# ---------------------------------------------------------------------------


def test_value_equality_and_hashing():
    assert Ok(1) == Ok(1)
    assert Err("x") == Err("x")
    assert Ok(1) != Err(1)
    assert Ok(1) != Ok(2)
    # Frozen dataclasses are hashable.
    assert len({Ok(1), Ok(1), Err("x")}) == 2


# ---------------------------------------------------------------------------
# Property: round-trip extraction
# ---------------------------------------------------------------------------


@settings(max_examples=100)
@given(st.integers())
def test_ok_round_trips_any_value(value):
    """For any value, Ok carries it back out unchanged via value/unwrap."""
    r = Ok(value)
    assert r.is_ok()
    assert r.value == value
    assert r.unwrap() == value
    assert r.unwrap_or(value + 1) == value


@settings(max_examples=100)
@given(st.text())
def test_err_round_trips_any_error(message):
    """For any error payload, Err carries it back out unchanged."""
    r = Err(message)
    assert r.is_err()
    assert r.error == message
    assert r.unwrap_err() == message
