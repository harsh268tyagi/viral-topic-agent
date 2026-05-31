"""A minimal ``Result[T, E]`` type for branching without exceptions.

The design (``.kiro/specs/viral-topic-agent/design.md`` -> *Design Goals* and
*ResilientDataSource*) calls for external calls to return a
``Result[T, DataSourceFailure]`` so that callers branch on success/failure as
data rather than catching exceptions. This keeps the bulk of the system pure
and property-testable, and ensures a failure of one request never silently
corrupts unrelated results.

This module provides two frozen variants, :class:`Ok` and :class:`Err`, plus a
``Result`` alias. The API is deliberately small (the only thing the codebase
needs is reliable construction, inspection, and unwrapping):

- ``is_ok`` / ``is_err`` for branching,
- ``value`` / ``error`` accessors that raise if used on the wrong variant,
- ``unwrap`` / ``unwrap_err`` and ``unwrap_or`` for extraction,
- ``map`` / ``map_err`` for transforming the carried value.

Both variants are ``frozen`` dataclasses, giving them value-based equality and
hashability, which is convenient in tests and when results are stored in other
frozen domain models.

Requirements traceability: 16.6 (failures returned as data carrying enough
context for the caller to render them).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, NoReturn, TypeVar, Union

__all__ = ["Ok", "Err", "Result", "UnwrapError"]

T = TypeVar("T")
E = TypeVar("E")
U = TypeVar("U")
F = TypeVar("F")


class UnwrapError(Exception):
    """Raised when a ``Result`` is unwrapped on the wrong variant.

    For example, calling :meth:`Ok.unwrap_err` or :meth:`Err.unwrap`. This is a
    programming error (a contract violation), not an expected degraded state,
    so it is surfaced as an exception rather than another ``Result``.
    """


@dataclass(frozen=True)
class Ok(Generic[T]):
    """A successful result carrying a value of type ``T``."""

    _value: T

    # -- Inspection ---------------------------------------------------------
    def is_ok(self) -> bool:
        return True

    def is_err(self) -> bool:
        return False

    # -- Accessors ----------------------------------------------------------
    @property
    def value(self) -> T:
        """The contained success value."""
        return self._value

    @property
    def error(self) -> NoReturn:
        """Accessing the error of an :class:`Ok` is a contract violation."""
        raise UnwrapError("Ok has no error")

    # -- Extraction ---------------------------------------------------------
    def unwrap(self) -> T:
        """Return the success value."""
        return self._value

    def unwrap_or(self, default: T) -> T:
        """Return the success value (the default is never used for ``Ok``)."""
        return self._value

    def unwrap_err(self) -> NoReturn:
        """Unwrapping an error from an :class:`Ok` is a contract violation."""
        raise UnwrapError(f"Called unwrap_err on an Ok value: {self._value!r}")

    # -- Transformation -----------------------------------------------------
    def map(self, fn: Callable[[T], U]) -> "Ok[U]":
        """Apply ``fn`` to the success value, returning a new :class:`Ok`."""
        return Ok(fn(self._value))

    def map_err(self, fn: Callable[..., object]) -> "Ok[T]":
        """No-op for :class:`Ok`; returns ``self`` unchanged."""
        return self


@dataclass(frozen=True)
class Err(Generic[E]):
    """A failed result carrying an error of type ``E``."""

    _error: E

    # -- Inspection ---------------------------------------------------------
    def is_ok(self) -> bool:
        return False

    def is_err(self) -> bool:
        return True

    # -- Accessors ----------------------------------------------------------
    @property
    def value(self) -> NoReturn:
        """Accessing the value of an :class:`Err` is a contract violation."""
        raise UnwrapError("Err has no value")

    @property
    def error(self) -> E:
        """The contained error."""
        return self._error

    # -- Extraction ---------------------------------------------------------
    def unwrap(self) -> NoReturn:
        """Unwrapping a value from an :class:`Err` is a contract violation."""
        raise UnwrapError(f"Called unwrap on an Err value: {self._error!r}")

    def unwrap_or(self, default: U) -> U:
        """Return ``default`` because there is no success value."""
        return default

    def unwrap_err(self) -> E:
        """Return the contained error."""
        return self._error

    # -- Transformation -----------------------------------------------------
    def map(self, fn: Callable[..., object]) -> "Err[E]":
        """No-op for :class:`Err`; returns ``self`` unchanged."""
        return self

    def map_err(self, fn: Callable[[E], F]) -> "Err[F]":
        """Apply ``fn`` to the error, returning a new :class:`Err`."""
        return Err(fn(self._error))


# A ``Result`` is either an ``Ok`` or an ``Err``.
Result = Union[Ok[T], Err[E]]
