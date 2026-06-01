"""Layered configuration sources for the Real Provider Integration.

The design (``.kiro/specs/real-provider-integration/design.md`` -> *ConfigLoader,
Settings, ConfigurationSource*) assembles validated ``Settings`` from a
precedence-ordered set of :class:`ConfigurationSource`s. This module defines the
:class:`ConfigurationSource` ``Protocol`` and the four concrete sources the
``ConfigLoader`` consults, in **decreasing** precedence:

1. :class:`OverridesSource` - explicit, in-process overrides (highest).
2. :class:`EnvSource` - process environment variables.
3. :class:`DotEnvSource` - a local ``.env`` file.
4. :class:`ConfigFileSource` - configuration-file defaults (lowest).

Each source exposes a single :meth:`ConfigurationSource.values` method that maps
documented configuration keys to their raw string values; the ``ConfigLoader``
(a later task) resolves precedence across them and maps each value to its
``Settings`` field by documented key (10.1, 10.2, 10.5).

This module also owns the ``.env`` parsing rules (Requirement 10.4, 10.6),
exposed both as the pure :func:`parse_dotenv` function and through
:class:`DotEnvSource`:

- a line whose first non-whitespace character is ``#`` is a comment and ignored;
- a blank line (empty or only whitespace) is ignored;
- every other line is parsed as ``KEY=VALUE`` split at the *first* ``=`` with
  surrounding whitespace trimmed from the key and the value (10.4);
- a non-blank, non-comment line that contains no ``=`` is reported as malformed,
  identified by its 1-based line number, and contributes no value (10.6).

Only the standard library is used here, keeping the configuration layer
dependency-free per the design's layer-placement table.

Requirements traceability: 10.4, 10.6 (and the source definitions supporting
10.1, 10.2, 10.5).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Iterable, Mapping, Protocol, runtime_checkable

__all__ = [
    "ConfigurationSource",
    "OverridesSource",
    "EnvSource",
    "DotEnvSource",
    "ConfigFileSource",
    "DotEnvParseResult",
    "parse_dotenv",
]


# ---------------------------------------------------------------------------
# ConfigurationSource protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ConfigurationSource(Protocol):
    """One source of configuration values in the layered model (Requirement 10).

    A source exposes the documented configuration keys it supplies, mapped to
    their raw (unparsed) string values. The ``ConfigLoader`` consults a fixed,
    precedence-ordered sequence of sources and, for each documented key, takes
    the value from the highest-precedence source whose :meth:`values` mapping
    contains it (10.1, 10.2).

    Implementations return only string values: any typing/coercion to a
    ``Settings`` field's type is the loader's responsibility (10.5). A source
    that supplies nothing returns an empty mapping rather than raising.
    """

    def values(self) -> Mapping[str, str]:
        """Return the documented keys this source supplies, mapped to raw strings."""
        ...


# ---------------------------------------------------------------------------
# .env parsing (Requirement 10.4, 10.6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DotEnvParseResult:
    """The outcome of parsing ``.env`` content.

    ``values`` holds every well-formed ``KEY=VALUE`` pair (key and value already
    trimmed, last assignment winning on duplicate keys). ``malformed_lines`` is
    the ascending tuple of 1-based line numbers of non-blank, non-comment lines
    that contained no ``=`` and therefore contributed no value (10.6).
    """

    values: Mapping[str, str]
    malformed_lines: tuple[int, ...] = ()


def parse_dotenv(text: str) -> DotEnvParseResult:
    """Parse ``.env`` ``text`` into values and malformed line numbers (10.4, 10.6).

    Applies the documented rules line by line (1-based numbering):

    - a blank line (empty or only whitespace) is ignored;
    - a line whose first non-whitespace character is ``#`` is a comment and
      ignored;
    - every other line is split at its *first* ``=`` into a key and a value,
      each trimmed of surrounding whitespace (10.4);
    - a non-blank, non-comment line with no ``=`` is recorded as malformed by
      its line number and contributes no value (10.6).

    Later assignments to the same key overwrite earlier ones, so the resolved
    mapping reflects the last well-formed occurrence of each key.
    """
    values: dict[str, str] = {}
    malformed: list[int] = []

    # splitlines() handles \n, \r\n and \r and does not emit a trailing empty
    # entry for a final newline, so line numbers track the file's lines exactly.
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        stripped = raw_line.strip()
        if not stripped:
            # Blank line (empty or whitespace only).
            continue
        if stripped.startswith("#"):
            # First non-whitespace character is '#': a comment.
            continue
        if "=" not in raw_line:
            # Non-blank, non-comment line with no '=': malformed (10.6).
            malformed.append(line_number)
            continue
        key, _, value = raw_line.partition("=")
        values[key.strip()] = value.strip()

    return DotEnvParseResult(values=values, malformed_lines=tuple(malformed))


# ---------------------------------------------------------------------------
# OverridesSource (highest precedence)
# ---------------------------------------------------------------------------


class OverridesSource:
    """Explicit, in-process configuration overrides (highest precedence).

    Wraps a caller-supplied mapping of documented keys to values, taking a
    defensive copy and coercing every value to ``str`` so the source obeys the
    string-valued :class:`ConfigurationSource` contract. Typically used to inject
    values programmatically (for example from command-line flags or a secret
    provider) that must win over the environment, ``.env`` file, and defaults.
    """

    __slots__ = ("_values",)

    def __init__(self, overrides: Mapping[str, object] | None = None) -> None:
        self._values: dict[str, str] = {
            str(key): str(value) for key, value in (overrides or {}).items()
        }

    def values(self) -> Mapping[str, str]:
        # Return a copy so callers cannot mutate the source's state.
        return dict(self._values)


# ---------------------------------------------------------------------------
# EnvSource (process environment variables)
# ---------------------------------------------------------------------------


class EnvSource:
    """Configuration from process environment variables (Requirement 10.1).

    Reads from a supplied environment mapping (defaulting to ``os.environ``) at
    construction time, taking a snapshot so the source is a stable, pure value.
    When ``keys`` is provided, only those documented keys are read from the
    environment; otherwise the full environment snapshot is exposed and the
    ``ConfigLoader`` selects the documented keys it recognizes.
    """

    __slots__ = ("_values",)

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        keys: Iterable[str] | None = None,
    ) -> None:
        source = os.environ if environ is None else environ
        if keys is None:
            self._values: dict[str, str] = {
                str(key): str(value) for key, value in source.items()
            }
        else:
            self._values = {
                str(key): str(source[key]) for key in keys if key in source
            }

    def values(self) -> Mapping[str, str]:
        return dict(self._values)


# ---------------------------------------------------------------------------
# DotEnvSource (a local .env file)
# ---------------------------------------------------------------------------


class DotEnvSource:
    """Configuration from a local ``.env`` file (Requirements 10.4, 10.6).

    Parses ``.env`` content using :func:`parse_dotenv` at construction time and
    exposes the resulting values through :meth:`values`. The line numbers of any
    malformed (non-blank, non-comment, no-``=``) lines are retained on
    :attr:`malformed_lines` so the ``ConfigLoader`` can report each malformed
    line by its number while applying no value from it (10.6).

    Either a file ``path`` or raw ``text`` may be supplied (``text`` takes
    precedence, which keeps the parser testable without touching the file
    system). A missing file is treated as empty content - no values and no
    malformed lines - so an absent ``.env`` simply contributes nothing rather
    than raising.
    """

    __slots__ = ("_result",)

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        text: str | None = None,
        encoding: str = "utf-8",
    ) -> None:
        if text is None:
            text = self._read(path, encoding)
        self._result: DotEnvParseResult = parse_dotenv(text)

    @staticmethod
    def _read(path: str | os.PathLike[str] | None, encoding: str) -> str:
        if path is None:
            return ""
        try:
            with open(path, "r", encoding=encoding) as handle:
                return handle.read()
        except FileNotFoundError:
            # An absent .env file contributes nothing (no values, no malformed).
            return ""

    def values(self) -> Mapping[str, str]:
        return dict(self._result.values)

    @property
    def malformed_lines(self) -> tuple[int, ...]:
        """The 1-based line numbers of malformed ``.env`` lines (10.6)."""
        return self._result.malformed_lines


# ---------------------------------------------------------------------------
# ConfigFileSource (configuration-file defaults, lowest precedence)
# ---------------------------------------------------------------------------


class ConfigFileSource:
    """Configuration-file defaults (lowest precedence).

    Supplies the documented per-key defaults baked into a JSON configuration
    file (a single flat object of ``key`` -> scalar value). Scalar values
    (strings, numbers, booleans) are coerced to their string form so the source
    honors the string-valued :class:`ConfigurationSource` contract; ``null`` and
    nested objects/arrays are ignored. A ``values`` mapping may be supplied
    directly instead of a path for testing. A missing file is treated as empty.
    """

    __slots__ = ("_values",)

    def __init__(
        self,
        path: str | os.PathLike[str] | None = None,
        *,
        values: Mapping[str, object] | None = None,
        encoding: str = "utf-8",
    ) -> None:
        raw = values if values is not None else self._read(path, encoding)
        self._values: dict[str, str] = {}
        for key, value in raw.items():
            coerced = self._coerce(value)
            if coerced is not None:
                self._values[str(key)] = coerced

    @staticmethod
    def _read(
        path: str | os.PathLike[str] | None, encoding: str
    ) -> Mapping[str, object]:
        if path is None:
            return {}
        try:
            with open(path, "r", encoding=encoding) as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return {}
        if not isinstance(document, Mapping):
            raise ValueError(
                "configuration file must contain a top-level JSON object of "
                "key/value defaults"
            )
        return document

    @staticmethod
    def _coerce(value: object) -> str | None:
        """Coerce a scalar default to its string form; ignore null/complex values."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            # bool is a subclass of int; render as the lowercase literal.
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        # Nested objects/arrays are not flat configuration values; skip them.
        return None

    def values(self) -> Mapping[str, str]:
        return dict(self._values)
