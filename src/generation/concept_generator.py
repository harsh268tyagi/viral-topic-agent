"""Title and thumbnail concept generation (Requirement 8).

The :class:`ConceptGenerator` turns a
:class:`~viral_topic_agent.models.ContentIdea` into a
:class:`~viral_topic_agent.models.ConceptSet` of click-worthy assets, using a
:class:`~viral_topic_agent.generation.GenerationProvider` as the creative
backend (so the component stays pure and property-testable, and a test can
inject a deterministic stub).

Design references (``.kiro/specs/viral-topic-agent/design.md`` -> Concept_Generator):

- Produce **at least 3 distinct** title concepts (8.1), each **1..100 chars**
  inclusive (8.3).
- Produce **at least 1** thumbnail concept that carries a visual description and
  a text overlay of **at most 30 characters** (8.2).
- When the idea is associated with a Channel_Category, the produced concepts
  belong to that category (8.4); the resulting ``ConceptSet`` is tagged with the
  idea's category.
- If the required set cannot be produced, return **no partial concepts** and an
  error that identifies the idea (8.5).

The provider returns *raw* artifacts (plain strings and
:class:`~viral_topic_agent.generation.ThumbnailDraft` values). Enforcement of
the domain invariants is the responsibility of this component:

- titles are stripped, truncated to 100 chars, de-duplicated, and dropped if
  empty, so the surviving set is distinct and every title is within 1..100
  chars (8.1, 8.3);
- thumbnail overlays are stripped and truncated to 30 chars, and a draft is
  dropped unless it carries both a non-empty visual description and a non-empty
  overlay (8.2).

Failure (the provider raising
:class:`~viral_topic_agent.generation.GenerationError`, too few distinct valid
titles, or no valid thumbnail) yields ``Err(ConceptError)`` with no partial
``ConceptSet`` (8.5).

Requirements traceability: 8.1, 8.2, 8.3, 8.4, 8.5.
"""

from __future__ import annotations

from dataclasses import dataclass

from generation.provider import GenerationError, GenerationProvider, ThumbnailDraft
from domain.models import ConceptSet, ContentIdea, ThumbnailConcept, TitleConcept
from infrastructure.result import Err, Ok, Result

__all__ = [
    "MIN_TITLES",
    "MIN_THUMBNAILS",
    "MAX_TITLE_CHARS",
    "MAX_OVERLAY_CHARS",
    "TITLE_REQUEST_COUNT",
    "THUMBNAIL_REQUEST_COUNT",
    "REASON_INSUFFICIENT_TITLES",
    "REASON_NO_THUMBNAIL",
    "REASON_GENERATION_FAILED",
    "ConceptError",
    "ConceptGenerator",
]


# ---------------------------------------------------------------------------
# Structural bounds (Requirement 8)
# ---------------------------------------------------------------------------

#: Minimum number of distinct title concepts required (8.1).
MIN_TITLES = 3
#: Minimum number of thumbnail concepts required (8.2).
MIN_THUMBNAILS = 1
#: Upper bound on title length, inclusive (8.3).
MAX_TITLE_CHARS = 100
#: Upper bound on thumbnail text-overlay length, inclusive (8.2).
MAX_OVERLAY_CHARS = 30

#: How many raw titles to request. Asking for more than ``MIN_TITLES`` leaves
#: headroom so de-duplication and dropping invalid candidates does not force a
#: spurious failure when the provider can supply enough distinct titles.
TITLE_REQUEST_COUNT = MIN_TITLES + 2
#: How many raw thumbnails to request (headroom above ``MIN_THUMBNAILS``).
THUMBNAIL_REQUEST_COUNT = MIN_THUMBNAILS + 2


# ---------------------------------------------------------------------------
# Error reasons (8.5)
# ---------------------------------------------------------------------------

#: Fewer than ``MIN_TITLES`` distinct, in-bound titles could be produced.
REASON_INSUFFICIENT_TITLES = "insufficient-titles"
#: No valid thumbnail (visual description + <=30 char overlay) could be produced.
REASON_NO_THUMBNAIL = "no-thumbnail"
#: The provider raised :class:`GenerationError` for an artifact.
REASON_GENERATION_FAILED = "generation-failed"


@dataclass(frozen=True)
class ConceptError:
    """Error returned when the required concept set cannot be produced (8.5).

    Always identifies the offending idea via ``idea_id``. ``reason`` is one of
    :data:`REASON_INSUFFICIENT_TITLES`, :data:`REASON_NO_THUMBNAIL`, or
    :data:`REASON_GENERATION_FAILED`. ``failed_item`` names the provider
    operation that raised when the failure originates from the provider.
    """

    idea_id: str
    reason: str
    failed_item: str | None = None


# ---------------------------------------------------------------------------
# Concept generator
# ---------------------------------------------------------------------------


class ConceptGenerator:
    """Generates title and thumbnail concepts for a Content_Idea (Requirement 8)."""

    def generate(
        self, idea: ContentIdea, gen: GenerationProvider
    ) -> Result[ConceptSet, ConceptError]:
        """Produce a :class:`ConceptSet` for ``idea`` or an error.

        Returns ``Ok(ConceptSet)`` with at least :data:`MIN_TITLES` distinct
        titles (each 1..100 chars) and at least :data:`MIN_THUMBNAILS` thumbnail
        (overlay <=30 chars), tagged with the idea's category when present
        (8.1-8.4). On any inability to produce the required set, returns
        ``Err(ConceptError)`` identifying the idea and produces **no** partial
        ``ConceptSet`` (8.5).
        """
        # --- titles (8.1, 8.3) -------------------------------------------
        try:
            raw_titles = gen.generate_titles(idea, TITLE_REQUEST_COUNT)
        except GenerationError as exc:
            return Err(
                ConceptError(
                    idea_id=idea.idea_id,
                    reason=REASON_GENERATION_FAILED,
                    failed_item=exc.item,
                )
            )

        titles = self._normalize_titles(raw_titles)
        if len(titles) < MIN_TITLES:
            # Too few distinct, in-bound titles -> no partial output (8.5).
            return Err(
                ConceptError(
                    idea_id=idea.idea_id, reason=REASON_INSUFFICIENT_TITLES
                )
            )

        # --- thumbnails (8.2) --------------------------------------------
        try:
            raw_thumbnails = gen.generate_thumbnails(idea, THUMBNAIL_REQUEST_COUNT)
        except GenerationError as exc:
            return Err(
                ConceptError(
                    idea_id=idea.idea_id,
                    reason=REASON_GENERATION_FAILED,
                    failed_item=exc.item,
                )
            )

        thumbnails = self._normalize_thumbnails(raw_thumbnails)
        if len(thumbnails) < MIN_THUMBNAILS:
            # No valid thumbnail -> no partial output (8.5).
            return Err(
                ConceptError(idea_id=idea.idea_id, reason=REASON_NO_THUMBNAIL)
            )

        # The set is tagged with the idea's category, so the concepts belong to
        # the associated category when one is present (8.4).
        return Ok(
            ConceptSet(
                idea_id=idea.idea_id,
                titles=titles,
                thumbnails=thumbnails,
                category=idea.category,
            )
        )

    # -- normalization helpers --------------------------------------------

    @staticmethod
    def _normalize_titles(raw_titles: tuple[str, ...]) -> tuple[TitleConcept, ...]:
        """Strip, truncate to 100 chars, drop empties, and de-duplicate (8.1, 8.3).

        Order is preserved and only the first occurrence of each distinct title
        is kept, so the surviving titles are distinct and every one is within
        1..100 characters.
        """
        seen: set[str] = set()
        titles: list[TitleConcept] = []
        for raw_text in raw_titles:
            text = raw_text.strip()[:MAX_TITLE_CHARS]
            if not (1 <= len(text) <= MAX_TITLE_CHARS):
                continue
            if text in seen:
                continue
            seen.add(text)
            titles.append(TitleConcept(text=text))
        return tuple(titles)

    @staticmethod
    def _normalize_thumbnails(
        raw_thumbnails: tuple[ThumbnailDraft, ...],
    ) -> tuple[ThumbnailConcept, ...]:
        """Strip and truncate overlays to 30 chars, dropping incomplete drafts (8.2).

        A draft survives only if it carries both a non-empty visual description
        and a non-empty text overlay; the overlay is truncated to at most 30
        characters.
        """
        thumbnails: list[ThumbnailConcept] = []
        for draft in raw_thumbnails:
            visual = draft.visual_description.strip()
            overlay = draft.text_overlay.strip()[:MAX_OVERLAY_CHARS]
            if not visual or not overlay:
                continue
            thumbnails.append(
                ThumbnailConcept(visual_description=visual, text_overlay=overlay)
            )
        return tuple(thumbnails)
