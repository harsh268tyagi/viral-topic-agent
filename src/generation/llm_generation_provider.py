"""The concrete :class:`LLMGenerationProvider` (Requirement 6).

This is the real :class:`~generation.provider.GenerationProvider` implementation
that backs the Concept_Generator and Script_Generator against a large language
model. It implements the existing protocol *exactly* (6.1) over the injected
:class:`~generation.llm_client.LLMClient` port, so it is fully testable with a
deterministic spy and no real network access (16.3).

Design references (``.kiro/specs/real-provider-integration/design.md`` ->
*LLMGenerationProvider*):

- Requests ``N`` candidates for titles/thumbnails and one artifact for
  outline/script/description, and returns one artifact per produced item
  (6.2, 6.3, 6.4).
- Returns the model's *raw* artifacts without enforcing the domain
  title-distinctness, title-length, thumbnail-overlay-length, or
  description-length constraints -- those stay in the consumers (6.5).
- Raises :class:`~generation.provider.GenerationError` -- naming the failed item
  and the affected :attr:`ContentIdea.idea_id`, with **no** partial artifact --
  when the request fails, does not complete within the configured timeout,
  returns zero items, or returns only empty/whitespace content (6.6). A
  ``count < 1`` for titles/thumbnails raises ``GenerationError`` **without**
  issuing any request to the LLM (6.8). Every reason is a short, secret-free
  description (6.7).

The provider performs **no** retry or backoff of its own; the single request per
item is delegated to the injected client. Timeout enforcement is delegated to
the client via the ``timeout_seconds`` argument, so "does not complete within
the configured request timeout" surfaces as the client raising, which the
provider maps to a ``GenerationError`` (6.6).

Requirements traceability: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.models import ContentIdea
from generation.provider import (
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    OP_THUMBNAILS,
    OP_TITLES,
    GenerationError,
    ThumbnailDraft,
)

if TYPE_CHECKING:  # pragma: no cover - types only, no runtime dependency
    from generation.llm_client import LLMClient

__all__ = [
    "THUMBNAIL_FIELD_DELIMITER",
    "REASON_REQUEST_FAILED",
    "REASON_NO_CONTENT",
    "REASON_INVALID_COUNT",
    "LLMGenerationProvider",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Separator the provider uses to split a raw thumbnail completion into a visual
#: description and a text overlay. Content with no delimiter is treated as a
#: visual description with an empty overlay.
THUMBNAIL_FIELD_DELIMITER = "||"

#: Reason recorded when the underlying LLM request fails or times out (6.6).
REASON_REQUEST_FAILED = "llm-request-failed"
#: Reason recorded when the LLM returns zero items or only empty/whitespace (6.6).
REASON_NO_CONTENT = "llm-returned-no-usable-content"
#: Reason recorded when a title/thumbnail count below 1 is requested (6.8).
REASON_INVALID_COUNT = "count-must-be-at-least-1"


def _is_blank(text: str) -> bool:
    """True when ``text`` is empty or contains only whitespace characters."""
    return not text.strip()


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LLMGenerationProvider:
    """A :class:`~generation.provider.GenerationProvider` backed by an LLM.

    Structurally satisfies the existing ``GenerationProvider`` protocol (6.1).
    Constructed with an injected :class:`~generation.llm_client.LLMClient` and a
    request timeout; both are forwarded to the client per call so the provider
    owns no network or retry policy of its own.
    """

    def __init__(self, client: "LLMClient", *, request_timeout_seconds: float) -> None:
        self._client = client
        self._request_timeout_seconds = request_timeout_seconds

    # -- title concepts (6.2) ----------------------------------------------

    def generate_titles(self, idea: ContentIdea, count: int) -> tuple[str, ...]:
        """Return one raw title string per candidate the LLM produces (6.2).

        A ``count < 1`` raises ``GenerationError`` without issuing a request
        (6.8). A failed/timed-out request, zero items, or only empty/whitespace
        content raises ``GenerationError`` with no partial artifact (6.6).
        """
        self._require_positive_count(OP_TITLES, idea, count)
        items = self._request(OP_TITLES, idea, self._title_prompt(idea), n=count)
        return items

    # -- thumbnail concepts (6.3) ------------------------------------------

    def generate_thumbnails(
        self, idea: ContentIdea, count: int
    ) -> tuple[ThumbnailDraft, ...]:
        """Return one :class:`ThumbnailDraft` per concept the LLM produces (6.3).

        Each draft carries a non-empty visual description and a text overlay.
        Count and failure handling mirror :meth:`generate_titles` (6.6, 6.8).
        """
        self._require_positive_count(OP_THUMBNAILS, idea, count)
        items = self._request(OP_THUMBNAILS, idea, self._thumbnail_prompt(idea), n=count)
        return tuple(self._to_thumbnail(content) for content in items)

    # -- script artifacts (6.4) --------------------------------------------

    def generate_outline(self, idea: ContentIdea) -> str:
        """Return the raw outline artifact produced by the LLM (6.4)."""
        return self._single(OP_OUTLINE, idea, self._outline_prompt(idea))

    def generate_script(self, idea: ContentIdea) -> str:
        """Return the raw script artifact produced by the LLM (6.4)."""
        return self._single(OP_SCRIPT, idea, self._script_prompt(idea))

    def generate_description(self, idea: ContentIdea) -> str:
        """Return the raw description artifact produced by the LLM (6.4)."""
        return self._single(OP_DESCRIPTION, idea, self._description_prompt(idea))

    # -- request orchestration ---------------------------------------------

    def _require_positive_count(
        self, item: str, idea: ContentIdea, count: int
    ) -> None:
        """Raise ``GenerationError`` without issuing a request when ``count<1`` (6.8)."""
        if count < 1:
            raise GenerationError(
                item=item, idea_id=idea.idea_id, reason=REASON_INVALID_COUNT
            )

    def _request(
        self, item: str, idea: ContentIdea, prompt: str, *, n: int
    ) -> tuple[str, ...]:
        """Issue one LLM request and return its raw, non-empty content (6.2, 6.6).

        Maps a failed/timed-out request, a zero-item response, or a response
        whose every item is empty/whitespace to a ``GenerationError`` that names
        the item and idea, returning no partial artifact (6.6).
        """
        items = self._complete(item, idea, prompt, n)
        if not items or all(_is_blank(content) for content in items):
            raise GenerationError(
                item=item, idea_id=idea.idea_id, reason=REASON_NO_CONTENT
            )
        return items

    def _single(self, item: str, idea: ContentIdea, prompt: str) -> str:
        """Issue a one-item request and return its raw, non-empty artifact (6.4, 6.6)."""
        items = self._complete(item, idea, prompt, 1)
        if not items or _is_blank(items[0]):
            raise GenerationError(
                item=item, idea_id=idea.idea_id, reason=REASON_NO_CONTENT
            )
        return items[0]

    def _complete(
        self, item: str, idea: ContentIdea, prompt: str, n: int
    ) -> tuple[str, ...]:
        """Call the injected client once, mapping any failure to ``GenerationError``.

        A failed or timed-out request surfaces as the client raising; the
        provider maps it to a ``GenerationError`` naming the item and idea, with
        no partial artifact (6.6).
        """
        try:
            produced = self._client.complete(
                prompt, n=n, timeout_seconds=self._request_timeout_seconds
            )
        except Exception as exc:  # request failure or timeout (6.6)
            raise GenerationError(
                item=item, idea_id=idea.idea_id, reason=REASON_REQUEST_FAILED
            ) from exc
        return tuple(produced)

    # -- thumbnail parsing --------------------------------------------------

    @staticmethod
    def _to_thumbnail(content: str) -> ThumbnailDraft:
        """Split one raw completion into a non-empty visual description + overlay.

        Content of the form ``"visual || overlay"`` is split at the first
        delimiter; content with no delimiter becomes the visual description with
        an empty overlay. The visual description falls back to the whole content
        when the split would leave it blank, so it stays non-empty (6.3). The
        overlay is passed through raw (no length enforcement, 6.5).
        """
        visual_part, _, overlay_part = content.partition(THUMBNAIL_FIELD_DELIMITER)
        visual = visual_part if not _is_blank(visual_part) else content
        return ThumbnailDraft(visual_description=visual, text_overlay=overlay_part)

    # -- prompt construction ------------------------------------------------

    @staticmethod
    def _idea_brief(idea: ContentIdea) -> str:
        category = idea.category.value if idea.category is not None else "general"
        return (
            f"Idea {idea.idea_id!r} (category={category}, "
            f"window={idea.time_window.value}): {idea.title_concept}"
        )

    def _title_prompt(self, idea: ContentIdea) -> str:
        return f"Write viral video title candidates for the following. {self._idea_brief(idea)}"

    def _thumbnail_prompt(self, idea: ContentIdea) -> str:
        return (
            "Write thumbnail concepts as 'visual description "
            f"{THUMBNAIL_FIELD_DELIMITER} text overlay' for the following. "
            f"{self._idea_brief(idea)}"
        )

    def _outline_prompt(self, idea: ContentIdea) -> str:
        return f"Write a video outline for the following. {self._idea_brief(idea)}"

    def _script_prompt(self, idea: ContentIdea) -> str:
        return f"Write a full video script for the following. {self._idea_brief(idea)}"

    def _description_prompt(self, idea: ContentIdea) -> str:
        return f"Write a video description for the following. {self._idea_brief(idea)}"
