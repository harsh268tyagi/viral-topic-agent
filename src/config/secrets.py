"""Secret-handling primitives for the Real Provider Integration.

This module implements the secret-protection foundation that the design
(``.kiro/specs/real-provider-integration/design.md`` -> *Secret and
CredentialReference*) requires, satisfying Requirement 12.

Contents:

- :class:`CredentialReference` - a non-secret name/handle that identifies a
  secret and is safe to record in logs, error reasons, and run summaries
  (12.1).
- :class:`Secret` - a wrapper whose value is reachable only through the
  explicit :meth:`Secret.reveal` call. Its ``__repr__``/``__str__`` return a
  redacted placeholder so the value cannot leak through logs, f-strings, or
  tracebacks (12.1, 12.3).
- :func:`redact` - a module-level helper that scrubs any secret value that
  might otherwise appear in a free-form reason string (12.3).

Every error reason, log entry, and run/startup summary in the feature records
the redacted reference in place of the secret value; the value is revealed
only where a transport must actually transmit it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

__all__ = ["CredentialReference", "Secret", "redact"]


@dataclass(frozen=True)
class CredentialReference:
    """A non-secret handle naming a secret; safe to log and summarize (12.1).

    ``name`` is the documented configuration handle for the secret it
    identifies (for example ``"youtube_api_key"`` or ``"smtp_password"``). It
    never carries the secret value itself, so it can appear freely in logs,
    error reasons, validation reports, and run summaries.
    """

    name: str


class Secret:
    """Wraps a secret value so it cannot leak through ordinary rendering.

    The value is reachable only through the explicit :meth:`reveal` call, used
    solely where a transport must transmit it. Everywhere else - ``repr``,
    ``str``, f-string interpolation, logging, and tracebacks - the redacted
    placeholder ``<Secret {name}>`` is what renders (12.1, 12.3).
    """

    __slots__ = ("_value", "_reference")

    def __init__(self, value: str, reference: CredentialReference) -> None:
        self._value = value
        self._reference = reference

    def reveal(self) -> str:
        """Return the wrapped secret value.

        This is the single, explicit way to access the value; it should be
        called only at the transport boundary where the value must be sent.
        """
        return self._value

    @property
    def reference(self) -> CredentialReference:
        """The non-secret :class:`CredentialReference` identifying this secret."""
        return self._reference

    def __repr__(self) -> str:
        return f"<Secret {self._reference.name}>"

    __str__ = __repr__


def redact(text: str, secrets: Iterable[Secret]) -> str:
    """Return ``text`` with every secret value replaced by its placeholder.

    Scrubs any secret value that might otherwise appear in a free-form string
    (such as an error reason built from a third-party message), replacing each
    occurrence with the secret's redacted placeholder ``<Secret {name}>``
    (12.3). Empty secret values are ignored so they cannot match everywhere,
    and longer values are replaced first so a value that is a substring of
    another is handled correctly.
    """
    # Sort by descending value length so overlapping values redact correctly.
    ordered = sorted(
        (s for s in secrets if s.reveal()),
        key=lambda s: len(s.reveal()),
        reverse=True,
    )
    redacted = text
    for secret in ordered:
        redacted = redacted.replace(secret.reveal(), repr(secret))
    return redacted
