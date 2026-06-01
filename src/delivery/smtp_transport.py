"""The ``SmtpTransport`` port and its stdlib ``smtplib``-backed implementation.

The :class:`EmailDeliverer` (design.md -> *Deliverers*, task 9.3) needs to push a
rendered digest out over email, but the *act* of transmitting one message is an
external, side-effecting operation. To keep the deliverer testable without a
real SMTP server, that act lives behind the minimal :class:`SmtpTransport` port
defined here, mirroring the ``HttpTransport`` seam used by the Slack/Notion
deliverers (design.md -> *SmtpTransport (port)*).

Contract (chosen so :class:`EmailDeliverer` can map a transmission failure to a
``DeliveryError`` -- Requirements 7.5, 7.6):

- A :class:`SmtpTransport` exposes a single :meth:`SmtpTransport.send` method
  that attempts to transmit *one* :class:`~email.message.EmailMessage`.
- On success it returns ``None``.
- On failure it raises :class:`SmtpTransportError`.

Like ``HttpTransport``, this port performs exactly one transmission attempt with
no retry or backoff of its own; retry policy stays with the ``DigestService``
(the single-attempt ``Deliverer`` contract, 16.5).

Two implementations live alongside the port:

- :class:`SmtplibSmtpTransport` is the production transport over the standard
  library's :mod:`smtplib` (Requirement 15.3 prefers the stdlib where it
  suffices); it opens a connection, optionally upgrades to TLS, authenticates
  when credentials are supplied, sends the message, and raises
  :class:`SmtpTransportError` on any failure.
- ``FakeSmtpTransport`` (in ``tests/edge_fakes.py``) records sent messages or
  raises to drive the :class:`EmailDeliverer` failure path without a network.

Secret hygiene (Requirement 7.6): the SMTP password is only ever handed to
:meth:`smtplib.SMTP.login`; it is never interpolated into a
:class:`SmtpTransportError` reason. Reasons name the host and port and the
underlying error *type* only, so a leaked traceback or log line carries no
credential value.

Requirements traceability: 16.3 (injected, testable transport), 15.3 (stdlib
``smtplib``); supports 7.5, 7.6 once :class:`EmailDeliverer` consumes it.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol, runtime_checkable

__all__ = [
    "SmtpTransport",
    "SmtpTransportError",
    "SmtplibSmtpTransport",
]


# ---------------------------------------------------------------------------
# Failure signal
# ---------------------------------------------------------------------------


class SmtpTransportError(Exception):
    """Raised by a :class:`SmtpTransport` when a single send attempt fails.

    ``reason`` is a human-readable description that, by construction, never
    contains a secret value (Requirement 7.6). :class:`EmailDeliverer` catches
    this and raises a redacted ``DeliveryError`` (7.5, 7.6).
    """

    def __init__(self, reason: str = "smtp transmission failed") -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# SmtpTransport boundary
# ---------------------------------------------------------------------------


@runtime_checkable
class SmtpTransport(Protocol):
    """Transmits a single email message to an SMTP server.

    Implementations attempt exactly one transmission per :meth:`send` call,
    returning ``None`` on success and raising :class:`SmtpTransportError` on
    failure. They perform no retry or backoff of their own.
    """

    def send(self, message: EmailMessage) -> None:
        """Transmit ``message``; raise :class:`SmtpTransportError` on failure."""
        ...


# ---------------------------------------------------------------------------
# Standard-library implementation
# ---------------------------------------------------------------------------


class SmtplibSmtpTransport:
    """A :class:`SmtpTransport` backed by the standard-library :mod:`smtplib`.

    Connection details are injected at construction so the same transport can
    serve every send. Each :meth:`send` opens a fresh connection, optionally
    upgrades to TLS, authenticates when a ``username`` is configured, transmits
    the message, and closes the connection.

    The ``password`` is supplied as a plain string at the transport boundary
    (the only place a secret is revealed, per the design); it is never recorded
    in a :class:`SmtpTransportError` reason (Requirement 7.6).
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        use_tls: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_tls = use_tls
        self._timeout_seconds = timeout_seconds

    def send(self, message: EmailMessage) -> None:
        """Transmit ``message`` over a single SMTP connection.

        Raises :class:`SmtpTransportError` -- with a reason that excludes every
        secret value -- if connecting, the optional TLS upgrade, login, or
        transmission fails for any reason.
        """
        try:
            with smtplib.SMTP(
                self._host, self._port, timeout=self._timeout_seconds
            ) as client:
                if self._use_tls:
                    client.starttls()
                if self._username is not None:
                    client.login(self._username, self._password or "")
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            # Name the destination and the error *type* only; never echo the
            # exception's free-form text or the password (7.6).
            raise SmtpTransportError(
                f"SMTP transmission to {self._host}:{self._port} failed: "
                f"{type(exc).__name__}"
            ) from exc
