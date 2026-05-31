"""Generation provider interface and a deterministic in-memory stub.

The creative components -- :class:`Concept_Generator` (Requirement 8) and
:class:`Script_Generator` (Requirement 10) -- do not talk to an LLM directly.
They depend on the :class:`GenerationProvider` abstraction defined here, so the
concrete provider (an LLM, a hosted service, etc.) can change without touching
domain logic, and tests can inject a deterministic stub.

Design references (``.kiro/specs/viral-topic-agent/design.md``):

- ``ConceptGenerator.generate(idea, gen: GenerationProvider) -> Result[ConceptSet, ...]``
  needs raw title concepts and thumbnail concepts (8.1, 8.2).
- ``ScriptGenerator.generate(idea, seo_keywords, gen: GenerationProvider) -> Result[ScriptBundle, ...]``
  needs the script artifacts outline, script draft, and description (10.1).

The interface therefore exposes five operations:

- :meth:`generate_titles`      -> raw title strings (Concept_Generator, 8.1)
- :meth:`generate_thumbnails`  -> raw thumbnail drafts (Concept_Generator, 8.2)
- :meth:`generate_outline`     -> a video outline (Script_Generator, 10.1)
- :meth:`generate_script`      -> a script draft (Script_Generator, 10.1)
- :meth:`generate_description` -> a video description (Script_Generator, 10.1)

The provider returns *raw* artifacts (plain strings and lightweight
:class:`ThumbnailDraft` values). Enforcement of the domain invariants -- title
distinctness and the 1..100 char bound (8.1, 8.3), the <=30 char overlay (8.2),
the 100..5000 char description (10.3), SEO-tag bounds (10.2) -- is the
responsibility of the Concept_Generator and Script_Generator that consume this
provider, not of the provider itself. The script artifacts are split into three
separate operations so a consumer can identify exactly which item failed (10.5).

Failure is signalled by raising :class:`GenerationError`, which carries the
failed item name and the offending idea id; consumers translate it into the
appropriate ``Result`` error (8.5, 10.5).

Requirements traceability: 8.1, 10.1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from viral_topic_agent.domain.models import ContentIdea

__all__ = [
    "OP_TITLES",
    "OP_THUMBNAILS",
    "OP_OUTLINE",
    "OP_SCRIPT",
    "OP_DESCRIPTION",
    "GENERATION_OPERATIONS",
    "ThumbnailDraft",
    "GenerationError",
    "GenerationProvider",
    "InMemoryGenerationProvider",
]


# ---------------------------------------------------------------------------
# Operation identifiers
# ---------------------------------------------------------------------------

#: Generate raw title concepts (Concept_Generator, 8.1).
OP_TITLES = "titles"
#: Generate raw thumbnail drafts (Concept_Generator, 8.2).
OP_THUMBNAILS = "thumbnails"
#: Generate a video outline (Script_Generator, 10.1).
OP_OUTLINE = "outline"
#: Generate a script draft (Script_Generator, 10.1).
OP_SCRIPT = "script"
#: Generate a video description (Script_Generator, 10.1).
OP_DESCRIPTION = "description"

#: All recognised operation identifiers, useful for validation and for
#: configuring failure injection in the stub.
GENERATION_OPERATIONS: frozenset[str] = frozenset(
    {OP_TITLES, OP_THUMBNAILS, OP_OUTLINE, OP_SCRIPT, OP_DESCRIPTION}
)


# ---------------------------------------------------------------------------
# Raw artifact carriers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThumbnailDraft:
    """A raw thumbnail concept produced by a :class:`GenerationProvider`.

    Mirrors the fields of :class:`~viral_topic_agent.models.ThumbnailConcept`
    but carries *unvalidated* provider output: the Concept_Generator is
    responsible for enforcing the <=30 char overlay bound (8.2) before packaging
    it into a domain ``ThumbnailConcept``.
    """

    visual_description: str
    text_overlay: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GenerationError(Exception):
    """Raised by a provider when it cannot produce a requested artifact.

    Consumers catch this and translate it into a ``Result`` error that names the
    failed item and the affected idea (8.5 for concepts, 10.5 for scripts).
    """

    def __init__(self, item: str, idea_id: str, reason: str = "generation-failed"):
        self.item = item
        self.idea_id = idea_id
        self.reason = reason
        super().__init__(
            f"{reason}: could not generate {item!r} for idea {idea_id!r}"
        )


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------


@runtime_checkable
class GenerationProvider(Protocol):
    """Abstraction over the creative-generation backend (e.g. an LLM).

    Implementations may be non-deterministic in production, but they must be
    pure functions of their arguments in tests. All methods raise
    :class:`GenerationError` when the requested artifact cannot be produced.
    """

    def generate_titles(self, idea: ContentIdea, count: int) -> tuple[str, ...]:
        """Return ``count`` candidate title strings for ``idea`` (8.1)."""
        ...

    def generate_thumbnails(
        self, idea: ContentIdea, count: int
    ) -> tuple[ThumbnailDraft, ...]:
        """Return ``count`` candidate thumbnail drafts for ``idea`` (8.2)."""
        ...

    def generate_outline(self, idea: ContentIdea) -> str:
        """Return a video outline for ``idea`` (10.1)."""
        ...

    def generate_script(self, idea: ContentIdea) -> str:
        """Return a script draft for ``idea`` (10.1)."""
        ...

    def generate_description(self, idea: ContentIdea) -> str:
        """Return a video description for ``idea`` (10.1)."""
        ...


# ---------------------------------------------------------------------------
# Deterministic in-memory stub
# ---------------------------------------------------------------------------

# Fixed, content-free building blocks so output is a pure function of inputs.
_TITLE_ANGLES: tuple[str, ...] = (
    "The Truth About",
    "Why Everyone Is Talking About",
    "Inside",
    "The Rise Of",
    "What Nobody Tells You About",
    "Breaking Down",
    "The Ultimate Guide To",
    "Reacting To",
)

_OVERLAY_WORDS: tuple[str, ...] = (
    "SHOCKING",
    "INSANE",
    "MUST SEE",
    "#1",
    "REVEALED",
    "GONE VIRAL",
)

_DESCRIPTION_FILLER = (
    "Subscribe for more data-driven content ideas tailored to your channel. "
)

# A domain ThumbnailConcept caps the text overlay at 30 characters (8.2); the
# stub keeps its raw overlays within that bound so they remain usable as-is.
_MAX_OVERLAY_CHARS = 30
# A domain TitleConcept is bounded to 1..100 characters (8.3); the stub keeps
# its raw titles within that bound.
_MAX_TITLE_CHARS = 100
# A ScriptBundle description is bounded to 100..5000 characters (10.3); the stub
# produces descriptions within that range.
_MIN_DESCRIPTION_CHARS = 100
_MAX_DESCRIPTION_CHARS = 5000


@dataclass(frozen=True)
class InMemoryGenerationProvider:
    """A deterministic, dependency-free :class:`GenerationProvider` for tests.

    Properties:

    - **Deterministic.** Every method is a pure function of its arguments and
      the instance configuration: the same input always yields the same output,
      with no randomness, clock, or external state. Two instances built with the
      same configuration are interchangeable.
    - **Configurable cardinality.** ``generate_titles``/``generate_thumbnails``
      honour the requested ``count``. ``max_titles`` / ``max_thumbnails`` impose
      an optional supply ceiling so a test can force *under*-production (fewer
      than requested) to exercise a consumer's "cannot produce the required
      set" path (8.5).
    - **Controllable failure injection.** Any operation listed in
      ``failing_operations`` raises :class:`GenerationError` identifying the item
      and the idea, exercising the consumers' failure branches (8.5, 10.5).

    The produced artifacts satisfy the structural bounds the domain models
    require (distinct titles within 1..100 chars, overlays <=30 chars,
    descriptions >=100 chars), so the stub is usable end-to-end without a real
    backend.
    """

    #: Operation identifiers (see ``GENERATION_OPERATIONS``) that should raise.
    failing_operations: frozenset[str] = field(default_factory=frozenset)
    #: Optional ceiling on titles produced regardless of the requested count.
    max_titles: int | None = None
    #: Optional ceiling on thumbnails produced regardless of the requested count.
    max_thumbnails: int | None = None

    # -- helpers ------------------------------------------------------------

    def _maybe_fail(self, operation: str, idea: ContentIdea) -> None:
        """Raise :class:`GenerationError` if ``operation`` is configured to fail."""
        if operation in self.failing_operations:
            raise GenerationError(item=operation, idea_id=idea.idea_id)

    @staticmethod
    def _base_text(idea: ContentIdea) -> str:
        """A stable, non-empty text seed derived from the idea."""
        base = idea.title_concept.strip()
        return base if base else idea.idea_id

    @staticmethod
    def _effective_count(requested: int, ceiling: int | None) -> int:
        """Clamp a requested count to ``[0, ceiling]`` (no ceiling -> as requested)."""
        if requested < 0:
            raise ValueError(f"count must be non-negative, got {requested}")
        if ceiling is None:
            return requested
        return min(requested, max(0, ceiling))

    # -- title concepts (8.1, 8.3) -----------------------------------------

    def generate_titles(self, idea: ContentIdea, count: int) -> tuple[str, ...]:
        """Return up to ``count`` distinct titles, each within 1..100 chars.

        Distinctness is guaranteed by a leading ``"{i+1}. "`` ordinal: it varies
        per title and sits at the front of the string, so it survives the
        100-char truncation that keeps every title within bound (8.3).
        """
        self._maybe_fail(OP_TITLES, idea)
        n = self._effective_count(count, self.max_titles)
        base = self._base_text(idea)
        titles: list[str] = []
        for i in range(n):
            angle = _TITLE_ANGLES[i % len(_TITLE_ANGLES)]
            title = f"{i + 1}. {angle} {base}"
            titles.append(title[:_MAX_TITLE_CHARS])
        return tuple(titles)

    # -- thumbnail concepts (8.2) ------------------------------------------

    def generate_thumbnails(
        self, idea: ContentIdea, count: int
    ) -> tuple[ThumbnailDraft, ...]:
        """Return up to ``count`` thumbnail drafts with overlays <=30 chars."""
        self._maybe_fail(OP_THUMBNAILS, idea)
        n = self._effective_count(count, self.max_thumbnails)
        base = self._base_text(idea)
        category = idea.category.value if idea.category is not None else "general"
        thumbnails: list[ThumbnailDraft] = []
        for i in range(n):
            overlay = _OVERLAY_WORDS[i % len(_OVERLAY_WORDS)][:_MAX_OVERLAY_CHARS]
            visual = (
                f"Thumbnail {i + 1}: a {category} scene illustrating "
                f"'{base}' with bold framing and high contrast."
            )
            thumbnails.append(
                ThumbnailDraft(visual_description=visual, text_overlay=overlay)
            )
        return tuple(thumbnails)

    # -- script artifacts (10.1) -------------------------------------------

    def generate_outline(self, idea: ContentIdea) -> str:
        """Return a deterministic outline for ``idea`` (10.1)."""
        self._maybe_fail(OP_OUTLINE, idea)
        base = self._base_text(idea)
        return (
            f"Outline for '{base}':\n"
            "1. Hook\n"
            "2. Context\n"
            "3. Main points\n"
            "4. Payoff\n"
            "5. Call to action"
        )

    def generate_script(self, idea: ContentIdea) -> str:
        """Return a deterministic script draft for ``idea`` (10.1)."""
        self._maybe_fail(OP_SCRIPT, idea)
        base = self._base_text(idea)
        return (
            f"[INTRO] Welcome back. Today we dig into {base}.\n"
            f"[BODY] Here is why {base} is taking off right now, "
            f"and what it means for your channel.\n"
            "[OUTRO] Like and subscribe for more."
        )

    def generate_description(self, idea: ContentIdea) -> str:
        """Return a deterministic description, padded to >=100 chars (10.3)."""
        self._maybe_fail(OP_DESCRIPTION, idea)
        base = self._base_text(idea)
        text = (
            f"In this video we explore {base}. "
            f"Derived from {idea.time_window.value} trends with an observed "
            f"metric value of {idea.observed_metric_value}. "
        )
        # Pad deterministically until the description meets the lower bound (10.3).
        while len(text) < _MIN_DESCRIPTION_CHARS:
            text += _DESCRIPTION_FILLER
        return text[:_MAX_DESCRIPTION_CHARS]
