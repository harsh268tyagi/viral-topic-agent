"""Shared edge test doubles for the Real Provider Integration feature.

This module collects the injectable fakes the edge components are tested
against -- ``FakeHttpTransport``, ``FakeSmtpTransport``, the spy ``LLMClient``,
and friends -- so the property and example tests can exercise every branch
with no real network access (Requirements 16.3, 16.4).

Each task appends only its own fakes here; keep additions self-contained and
import-light so the file stays usable by every edge test module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Mapping

from delivery.smtp_transport import SmtpTransportError
from domain.models import ChannelCategory, KeywordMetric
from generation.llm_client import LLMClient, LLMClientError
from infrastructure.http_transport import (
    HttpResponse,
    HttpTimeoutError,
    HttpTransportError,
)

__all__ = [
    "LLMCompletion",
    "SpyLLMClient",
    "RecordedRequest",
    "FakeHttpTransport",
    "FakeSmtpTransport",
    "FakeKeywordMetricsProvider",
]


# ---------------------------------------------------------------------------
# LLMClient spy/fake (task 1.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMCompletion:
    """A single recorded ``complete`` invocation against :class:`SpyLLMClient`."""

    prompt: str
    n: int
    timeout_seconds: float | None


@dataclass
class SpyLLMClient:
    """A deterministic spy/fake :class:`LLMClient` for tests.

    Structurally satisfies the :class:`~generation.llm_client.LLMClient`
    protocol. It records every call (so tests can assert that a request was or
    was not issued and with what arguments) and returns scripted output:

    - ``responses`` is consulted in order: each call pops the next scripted
      result. A result may be a ``tuple[str, ...]`` to return, or an
      ``Exception`` instance to raise (drives the failure/timeout branch, 6.6).
    - When ``responses`` is exhausted (or never set), the spy synthesizes
      ``n`` deterministic completions from ``default_text`` so a test that only
      cares about cardinality needs no script.
    - ``fail_with``, when set, makes every call raise that exception regardless
      of ``responses`` -- a quick way to force the failure path.

    The spy never touches the network and is a pure function of its
    configuration and call history.
    """

    responses: list[tuple[str, ...] | Exception] = field(default_factory=list)
    default_text: str = "completion"
    fail_with: Exception | None = None
    calls: list[LLMCompletion] = field(default_factory=list)

    def complete(
        self, prompt: str, *, n: int = 1, timeout_seconds: float | None = None
    ) -> tuple[str, ...]:
        self.calls.append(
            LLMCompletion(prompt=prompt, n=n, timeout_seconds=timeout_seconds)
        )

        if self.fail_with is not None:
            raise self.fail_with

        if self.responses:
            scripted = self.responses.pop(0)
            if isinstance(scripted, Exception):
                raise scripted
            return tuple(scripted)

        if n < 1:
            raise LLMClientError(f"completion count must be >= 1, got {n}")
        return tuple(f"{self.default_text} {i + 1}" for i in range(n))

    @property
    def call_count(self) -> int:
        """How many times :meth:`complete` has been invoked."""
        return len(self.calls)


# ---------------------------------------------------------------------------
# HttpTransport fake (task 1.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecordedRequest:
    """A single request observed by :class:`FakeHttpTransport`."""

    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None
    timeout_seconds: float | None


class FakeHttpTransport:
    """A scripted :class:`~infrastructure.http_transport.HttpTransport` double.

    Construct it with an ordered sequence of *outcomes*. Each call to
    :meth:`request` consumes the next outcome:

    - an :class:`HttpResponse` is returned as-is;
    - an instance (or subclass) of :class:`HttpTransportError` /
      :class:`HttpTimeoutError` is raised to drive a transport-failure branch.

    Every request is recorded in :attr:`requests` (and the latest in
    :attr:`last_request`) so a test can assert the method, url, headers, body,
    and timeout, and confirm that exactly one request was performed (16.5).

    Running out of scripted outcomes raises :class:`AssertionError`, surfacing
    an unexpected extra request rather than silently returning ``None``.
    """

    def __init__(
        self, outcomes: list[HttpResponse | HttpTransportError] | None = None
    ) -> None:
        self._outcomes: list[HttpResponse | HttpTransportError] = list(outcomes or [])
        self.requests: list[RecordedRequest] = []

    # -- Scripting helpers --------------------------------------------------

    def queue_response(self, response: HttpResponse) -> "FakeHttpTransport":
        """Append a scripted :class:`HttpResponse`. Returns ``self`` for chaining."""
        self._outcomes.append(response)
        return self

    def queue_error(self, error: HttpTransportError) -> "FakeHttpTransport":
        """Append a scripted transport error to raise. Returns ``self``."""
        self._outcomes.append(error)
        return self

    # -- Inspection ---------------------------------------------------------

    @property
    def call_count(self) -> int:
        """How many requests have been performed against this fake."""
        return len(self.requests)

    @property
    def last_request(self) -> RecordedRequest | None:
        """The most recent request, or ``None`` if none has been made."""
        return self.requests[-1] if self.requests else None

    # -- HttpTransport protocol --------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        self.requests.append(
            RecordedRequest(
                method=method,
                url=url,
                headers=dict(headers or {}),
                body=body,
                timeout_seconds=timeout_seconds,
            )
        )
        if not self._outcomes:
            raise AssertionError(
                f"FakeHttpTransport received an unscripted request: {method} {url}"
            )
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, HttpTransportError):
            raise outcome
        return outcome


# ---------------------------------------------------------------------------
# SmtpTransport fake (task 1.2)
# ---------------------------------------------------------------------------


@dataclass
class FakeSmtpTransport:
    """A scripted :class:`~delivery.smtp_transport.SmtpTransport` double.

    Structurally satisfies the ``SmtpTransport`` port. Each call to
    :meth:`send` records the transmitted :class:`~email.message.EmailMessage`
    in :attr:`sent` so a test can assert what was transmitted, then either
    returns ``None`` (the success path) or raises :class:`SmtpTransportError`
    when ``fail_with`` is configured (the failure path, driving the
    :class:`~delivery.email_deliverer.EmailDeliverer` redacted-failure branch).

    It never touches a network and is a pure function of its configuration and
    call history.
    """

    fail_with: SmtpTransportError | None = None
    sent: list[EmailMessage] = field(default_factory=list)

    def send(self, message: EmailMessage) -> None:
        self.sent.append(message)
        if self.fail_with is not None:
            raise self.fail_with

    @property
    def call_count(self) -> int:
        """How many times :meth:`send` has been invoked."""
        return len(self.sent)


# ---------------------------------------------------------------------------
# KeywordMetricsProvider fake (task 6.5)
# ---------------------------------------------------------------------------


@dataclass
class FakeKeywordMetricsProvider:
    """A scripted :class:`~infrastructure.keyword_metrics_provider.KeywordMetricsProvider`.

    Structurally satisfies the ``KeywordMetricsProvider`` protocol. It returns a
    fixed list of :class:`~domain.models.KeywordMetric` values regardless of the
    requested ``max_keywords`` (the authoritative cap is applied by
    :class:`~infrastructure.youtube_data_source.YouTubeDataSource`, 4.2), records
    every ``fetch`` invocation for inspection, and -- when ``fail_with`` is set --
    raises that exception instead (to drive the classified-failure branch, 4.4).

    It never touches a network and is a pure function of its configuration and
    call history.
    """

    metrics: list[KeywordMetric] = field(default_factory=list)
    fail_with: Exception | None = None
    calls: list[tuple[ChannelCategory, int]] = field(default_factory=list)

    def fetch(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]:
        self.calls.append((category, max_keywords))
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.metrics)

    @property
    def call_count(self) -> int:
        """How many times :meth:`fetch` has been invoked."""
        return len(self.calls)
