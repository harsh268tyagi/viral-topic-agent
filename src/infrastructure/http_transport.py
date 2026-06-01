"""A minimal synchronous HTTP port and a stdlib-backed implementation.

The design (``.kiro/specs/real-provider-integration/design.md`` -> *HttpTransport
(port)*) calls for a thin, injectable HTTP seam so the YouTube / Slack / Notion
edges stay testable without real network access (16.3). The port deliberately
does **no** retry or backoff (that is ``ResilientDataSource``'s job, 16.5); it
performs exactly one request and either returns a structured
:class:`HttpResponse` or signals a transport-level failure.

Contents:

- :class:`HttpResponse` - a frozen response value carrying ``status``,
  case-insensitive ``headers``, and the raw ``body`` bytes.
- :class:`HttpTransport` - a ``Protocol`` with a single ``request`` method so
  any conforming transport can be injected.
- :class:`HttpTransportError` / :class:`HttpTimeoutError` - the two
  transport-level failure signals. A connection failure or reset surfaces as
  :class:`HttpTransportError`; a response that does not complete within the
  timeout surfaces as :class:`HttpTimeoutError`.
- :class:`UrllibHttpTransport` - the production implementation over stdlib
  ``urllib.request`` (15.3), performing one request with no retry/backoff.

Note that an HTTP *error status* (e.g. 404, 429, 500) is **not** a transport
failure here: it is returned as an :class:`HttpResponse` so that the downstream
error-classification logic (``classify_response``) can map it. Only genuine
transport problems (connection refused/reset, DNS failure, timeout) raise.

Requirements traceability: 16.3 (injectable, testable seam), 16.5 (no internal
retry/backoff), 15.3 (prefer the standard library).
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Mapping, Protocol, runtime_checkable

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "HttpTransportError",
    "HttpTimeoutError",
    "CaseInsensitiveHeaders",
    "UrllibHttpTransport",
]


# ---------------------------------------------------------------------------
# Transport-level failure signals
# ---------------------------------------------------------------------------


class HttpTransportError(Exception):
    """A transport-level failure: connection refused/reset, DNS failure, etc.

    Raised when no HTTP response could be obtained at all. An HTTP response
    carrying an error *status* is not a transport failure - it is returned as
    an :class:`HttpResponse` for downstream classification.
    """


class HttpTimeoutError(HttpTransportError):
    """The response did not complete within the requested timeout.

    A subtype of :class:`HttpTransportError` so callers may catch either, but
    distinct so a timeout can be mapped separately (e.g. to a transient
    ``TimeoutError`` by ``classify_response``). Callers that handle both must
    catch :class:`HttpTimeoutError` *before* :class:`HttpTransportError`.
    """


# ---------------------------------------------------------------------------
# Case-insensitive header mapping
# ---------------------------------------------------------------------------


class CaseInsensitiveHeaders(Mapping[str, str]):
    """A read-only ``Mapping`` of HTTP headers with case-insensitive lookup.

    Header names are matched case-insensitively (per RFC 7230) while the
    original casing is preserved for iteration and display. When the same
    header name is supplied more than once differing only in case, the last
    value wins.
    """

    __slots__ = ("_store",)

    def __init__(
        self, data: Mapping[str, str] | Iterable[tuple[str, str]] | None = None
    ) -> None:
        # Maps lower-cased name -> (original-cased name, value).
        self._store: dict[str, tuple[str, str]] = {}
        if data is None:
            items: Iterable[tuple[str, str]] = ()
        elif isinstance(data, Mapping):
            items = data.items()
        else:
            items = data
        for key, value in items:
            self._store[key.lower()] = (key, value)

    def __getitem__(self, key: str) -> str:
        return self._store[key.lower()][1]

    def __iter__(self) -> Iterator[str]:
        return (original for original, _ in self._store.values())

    def __len__(self) -> int:
        return len(self._store)

    def __repr__(self) -> str:
        rendered = ", ".join(
            f"{original!r}: {value!r}" for original, value in self._store.values()
        )
        return f"{type(self).__name__}({{{rendered}}})"


# ---------------------------------------------------------------------------
# HttpResponse value
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResponse:
    """A structured HTTP response returned by an :class:`HttpTransport`.

    ``headers`` supports case-insensitive lookup regardless of the mapping
    supplied at construction: a plain ``dict`` is normalized into a
    :class:`CaseInsensitiveHeaders` so that ``response.headers["retry-after"]``
    and ``response.headers["Retry-After"]`` resolve identically.
    """

    status: int
    headers: Mapping[str, str] = field(default_factory=CaseInsensitiveHeaders)
    body: bytes = b""

    def __post_init__(self) -> None:
        if not isinstance(self.headers, CaseInsensitiveHeaders):
            object.__setattr__(
                self, "headers", CaseInsensitiveHeaders(self.headers)
            )


# ---------------------------------------------------------------------------
# HttpTransport protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class HttpTransport(Protocol):
    """A minimal synchronous HTTP port.

    Implementations perform exactly one request and return an
    :class:`HttpResponse` (including for HTTP error statuses), or raise
    :class:`HttpTransportError` / :class:`HttpTimeoutError` for transport-level
    failures. They perform no retry, backoff, or rate-limit handling (16.5).
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] = ...,
        body: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse: ...
    # Raises HttpTransportError for connection failure/reset and
    # HttpTimeoutError when the response does not complete within timeout.


# ---------------------------------------------------------------------------
# Production implementation
# ---------------------------------------------------------------------------


class UrllibHttpTransport:
    """A production :class:`HttpTransport` over stdlib ``urllib.request`` (15.3).

    Performs exactly one request with no retry, backoff, or rate-limit handling
    (16.5). An HTTP error status (4xx/5xx) is captured and returned as an
    :class:`HttpResponse` rather than raised, so the caller's classification
    logic decides how to react. Genuine transport problems raise:
    :class:`HttpTimeoutError` on a timeout and :class:`HttpTransportError` on a
    connection failure or reset.
    """

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(url=url, data=body, method=method.upper())
        for name, value in (headers or {}).items():
            request.add_header(name, value)

        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=getattr(response, "status", response.getcode()),
                    headers=CaseInsensitiveHeaders(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            # An HTTP error *status* is a valid structured response, not a
            # transport failure; surface it for downstream classification.
            try:
                error_body = exc.read()
            except Exception:  # pragma: no cover - defensive: body already consumed
                error_body = b""
            error_headers = exc.headers.items() if exc.headers is not None else ()
            return HttpResponse(
                status=exc.code,
                headers=CaseInsensitiveHeaders(error_headers),
                body=error_body,
            )
        except (socket.timeout, TimeoutError) as exc:
            raise HttpTimeoutError(f"request to {url} timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise HttpTimeoutError(f"request to {url} timed out") from exc
            raise HttpTransportError(f"request to {url} failed: {exc.reason}") from exc
        except (ConnectionError, OSError) as exc:
            raise HttpTransportError(f"request to {url} failed: {exc}") from exc
