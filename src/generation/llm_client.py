"""The injected ``LLMClient`` port and a stdlib-backed HTTP implementation.

The :class:`~generation.llm_generation_provider.LLMGenerationProvider`
(Requirement 6) never talks to a concrete large-language-model vendor directly.
It depends on the :class:`LLMClient` abstraction defined here, so the concrete
backend (an OpenAI-style chat-completions endpoint, a hosted service, a local
model, etc.) is a configuration detail and tests can inject a deterministic
spy/fake with no real network access (Requirement 16.3).

Design references (``.kiro/specs/real-provider-integration/design.md`` ->
*LLMGenerationProvider*):

- ``LLMClient.complete(prompt, *, n=1, timeout_seconds=None) -> tuple[str, ...]``
  is the single operation. The provider requests ``n`` candidates for titles and
  thumbnails and one for outline/script/description, and receives the model's
  *raw* completion strings back (one per produced item).

The concrete :class:`HttpLLMClient` performs exactly one HTTP request over the
injected :class:`~infrastructure.http_transport.HttpTransport` port (stdlib
``urllib`` in production, Requirement 15.3) and parses an OpenAI-style
chat-completions response. It performs **no** retry or backoff of its own --
resilience policy lives elsewhere -- and it surfaces every failure as an
:class:`LLMClientError`, leaving the provider to translate that into the domain
``GenerationError`` (Requirement 6.6).

The ``HttpTransport``/``HttpResponse`` types are imported only for type checking
(under ``TYPE_CHECKING``): at runtime the client uses the injected transport
instance structurally, so this module never hard-imports the transport package.

Requirements traceability: 16.3 (injected client, testable without network).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Mapping, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover - types only, no runtime dependency
    from infrastructure.http_transport import HttpResponse, HttpTransport

__all__ = [
    "LLMClient",
    "LLMClientError",
    "HttpLLMClient",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class LLMClientError(Exception):
    """Raised by a concrete :class:`LLMClient` when a completion request fails.

    Covers transport-level failure/timeout, a non-success HTTP status, and a
    malformed or unparseable response body. The
    :class:`~generation.llm_generation_provider.LLMGenerationProvider` catches
    this and raises a domain ``GenerationError`` naming the failed item and
    idea (6.6). The reason is a short, secret-free description; callers that log
    it are responsible for redaction at their boundary.
    """


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMClient(Protocol):
    """Abstraction over the large-language-model completion backend.

    A single operation produces one or more raw completion strings for a
    prompt. Implementations may be non-deterministic in production but must be a
    pure function of their arguments in tests. Implementations raise
    :class:`LLMClientError` (or another exception) when the request cannot be
    completed; they do **not** retry internally.
    """

    def complete(
        self, prompt: str, *, n: int = 1, timeout_seconds: float | None = None
    ) -> tuple[str, ...]:
        """Return up to ``n`` raw completion strings for ``prompt``.

        ``timeout_seconds`` bounds the single request; ``None`` defers to the
        implementation's configured default. The returned strings are raw model
        output -- callers (the provider and its consumers) enforce any
        distinctness, length, or non-emptiness constraints.
        """
        ...


# ---------------------------------------------------------------------------
# Stdlib-backed HTTP implementation
# ---------------------------------------------------------------------------


class HttpLLMClient:
    """An :class:`LLMClient` over an injected :class:`HttpTransport`.

    Performs exactly one ``POST`` to a chat-completions endpoint and returns the
    raw ``choices[*].message.content`` strings (falling back to a top-level
    ``text`` field for completion-style responses). It does no retry or backoff
    (15.3, 16.5-style edge policy lives in the resilience layer), and raises
    :class:`LLMClientError` on transport failure/timeout, a non-2xx status, or a
    malformed body.

    The vendor is a configuration detail: ``endpoint_url`` and ``model`` select
    it, and the optional ``api_key`` is sent as a bearer token. The key is used
    only here, at the transport boundary; this client never logs it.
    """

    def __init__(
        self,
        transport: "HttpTransport",
        *,
        endpoint_url: str,
        model: str,
        api_key: str | None = None,
        default_timeout_seconds: float | None = None,
    ) -> None:
        self._transport = transport
        self._endpoint_url = endpoint_url
        self._model = model
        self._api_key = api_key
        self._default_timeout_seconds = default_timeout_seconds

    def complete(
        self, prompt: str, *, n: int = 1, timeout_seconds: float | None = None
    ) -> tuple[str, ...]:
        if n < 1:
            # Never issue a request that cannot return a usable candidate.
            raise LLMClientError(f"completion count must be >= 1, got {n}")

        body = self._build_body(prompt, n)
        headers = self._build_headers()
        timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self._default_timeout_seconds
        )

        try:
            response = self._transport.request(
                "POST",
                self._endpoint_url,
                headers=headers,
                body=body,
                timeout_seconds=timeout,
            )
        except Exception as exc:  # transport-level failure/timeout
            raise LLMClientError(f"llm request failed: {exc}") from exc

        return self._parse_response(response)

    # -- request shaping ----------------------------------------------------

    def _build_body(self, prompt: str, n: int) -> bytes:
        payload = {
            "model": self._model,
            "n": n,
            "messages": [{"role": "user", "content": prompt}],
        }
        return json.dumps(payload).encode("utf-8")

    def _build_headers(self) -> Mapping[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    # -- response parsing ---------------------------------------------------

    def _parse_response(self, response: "HttpResponse") -> tuple[str, ...]:
        if not 200 <= response.status < 300:
            raise LLMClientError(f"llm request returned status {response.status}")

        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LLMClientError("llm response body was not valid JSON") from exc

        choices = payload.get("choices") if isinstance(payload, Mapping) else None
        if not isinstance(choices, list):
            raise LLMClientError("llm response is missing a 'choices' list")

        texts: list[str] = []
        for choice in choices:
            texts.append(self._extract_text(choice))
        # An empty choices list yields an empty tuple; the provider decides
        # whether zero produced items is an error (6.6).
        return tuple(texts)

    @staticmethod
    def _extract_text(choice: object) -> str:
        if isinstance(choice, Mapping):
            message = choice.get("message")
            if isinstance(message, Mapping):
                content = message.get("content")
                if isinstance(content, str):
                    return content
            # Completion-style fallback: a top-level "text" field.
            text = choice.get("text")
            if isinstance(text, str):
                return text
        raise LLMClientError("llm choice is missing string content")
