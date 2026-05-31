"""Trend-to-script generation (Requirement 10).

The :class:`ScriptGenerator` converts a selected
:class:`~viral_topic_agent.models.ContentIdea` into a production-ready
:class:`~viral_topic_agent.models.ScriptBundle`: a video outline, a script
draft, a set of SEO tags, and a video description.

Design references (``.kiro/specs/viral-topic-agent/design.md`` -> Script_Generator):

- Produce outline, script draft, SEO tags, and description (10.1). The creative
  artifacts (outline, script, description) come from a
  :class:`~viral_topic_agent.generation.GenerationProvider` so the backend
  (an LLM, a hosted service, etc.) can change without touching this logic.
- SEO tags include **every** keyword supplied by the ``SEO_Analyzer`` for the
  idea and total **between 5 and 30** (10.2). When fewer than 5 keywords are
  supplied (but at least one), the set is augmented with deterministic derived
  tags so the lower bound is met; analyzer keywords are never dropped.
- The video description is bounded to **100..5000 characters** (10.3); shorter
  output is padded deterministically and longer output is truncated.
- When the ``SEO_Analyzer`` supplies **no** keywords, the outline, script, and
  description are still produced and the bundle's ``seo_tags_unavailable`` flag
  is set with an empty tag set (10.4).
- If the outline, script, or description cannot be produced, no partial bundle
  is returned: instead an :class:`Err` carrying a :class:`ScriptError` names the
  failed item and **retains the selected idea** so the Creator can retry (10.5).

This component returns a ``Result`` rather than raising: the
:class:`~viral_topic_agent.generation.GenerationError` raised by the provider is
caught and translated into ``Err(ScriptError(...))``.

Requirements traceability: 10.1, 10.2, 10.3, 10.4, 10.5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from generation.provider import (
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    GenerationError,
    GenerationProvider,
)
from domain.models import ContentIdea, ScriptBundle
from infrastructure.result import Err, Ok, Result

__all__ = [
    "MIN_SEO_TAGS",
    "MAX_SEO_TAGS",
    "MIN_DESCRIPTION_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "ScriptError",
    "ScriptGenerator",
]


# SEO tag count bounds (10.2): every analyzer keyword is included and the total
# is kept within these bounds (augmenting with derived tags when too few are
# supplied).
MIN_SEO_TAGS = 5
MAX_SEO_TAGS = 30

# Description length bounds (10.3): padded up to the minimum, truncated at the
# maximum.
MIN_DESCRIPTION_CHARS = 100
MAX_DESCRIPTION_CHARS = 5000

# Deterministic filler used to pad a too-short description up to the lower bound
# (10.3). Content-free so the output remains a pure function of the inputs.
_DESCRIPTION_FILLER = (
    "Subscribe and turn on notifications for more data-driven video ideas "
    "tailored to your channel. "
)

# Generic fallback tags used (in order) to augment a too-small tag set up to the
# minimum when the idea itself does not yield enough distinct derived tags.
_FALLBACK_TAGS: tuple[str, ...] = (
    "youtube",
    "video",
    "trending",
    "viral",
    "creator",
    "howto",
    "guide",
    "tips",
)

# Token splitter for deriving tags from free-text fields (title concept, etc.).
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class ScriptError:
    """Error returned when a :class:`ScriptBundle` cannot be produced (10.5).

    Carries the ``failed_item`` (one of the generation operation identifiers
    ``"outline"``, ``"script"``, or ``"description"``) so the Creator sees which
    artifact failed, and **retains** the selected ``idea`` so generation can be
    retried without re-selecting it.
    """

    failed_item: str
    idea: ContentIdea
    reason: str = "generation-failed"


class ScriptGenerator:
    """Turns a selected Content_Idea into a Script_Bundle (Requirement 10)."""

    def generate(
        self,
        idea: ContentIdea,
        seo_keywords: list[str],
        gen: GenerationProvider,
    ) -> Result[ScriptBundle, ScriptError]:
        """Produce a :class:`ScriptBundle` for ``idea`` or an identifying error.

        On success returns ``Ok(ScriptBundle)`` whose outline, script, SEO tags,
        and description satisfy the Requirement 10 bounds. On a generation
        failure for the outline, script, or description, returns
        ``Err(ScriptError)`` naming the failed item and retaining ``idea`` (10.5);
        no partial bundle is produced.

        ``seo_keywords`` are the keywords supplied by the ``SEO_Analyzer`` for
        the idea. An empty list triggers the SEO-tags-unavailable path (10.4).
        """
        # -- creative artifacts via the provider (10.1) --------------------
        # A failure of any item short-circuits with an identifying error that
        # retains the idea for retry (10.5); no partial bundle is built.
        try:
            outline = gen.generate_outline(idea)
        except GenerationError as exc:
            return Err(self._script_error(OP_OUTLINE, idea, exc))

        try:
            script = gen.generate_script(idea)
        except GenerationError as exc:
            return Err(self._script_error(OP_SCRIPT, idea, exc))

        try:
            raw_description = gen.generate_description(idea)
        except GenerationError as exc:
            return Err(self._script_error(OP_DESCRIPTION, idea, exc))

        description = self._bound_description(raw_description, idea)

        # -- SEO tags (10.2, 10.4) -----------------------------------------
        seo_tags, seo_tags_unavailable = self._build_seo_tags(idea, seo_keywords)

        return Ok(
            ScriptBundle(
                idea_id=idea.idea_id,
                outline=outline,
                script=script,
                seo_tags=seo_tags,
                description=description,
                seo_tags_unavailable=seo_tags_unavailable,
            )
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _script_error(
        item: str, idea: ContentIdea, exc: GenerationError
    ) -> ScriptError:
        """Build a :class:`ScriptError` from a provider failure (10.5)."""
        return ScriptError(failed_item=item, idea=idea, reason=exc.reason)

    @staticmethod
    def _bound_description(text: str, idea: ContentIdea) -> str:
        """Coerce ``text`` to the 100..5000 char bound (10.3).

        Pads a too-short description with deterministic filler and truncates a
        too-long one. The result is a pure function of the inputs.
        """
        if len(text) > MAX_DESCRIPTION_CHARS:
            return text[:MAX_DESCRIPTION_CHARS]
        if len(text) >= MIN_DESCRIPTION_CHARS:
            return text
        # Pad deterministically until the lower bound is met, then clamp in case
        # the final filler chunk overshot the upper bound (it cannot here, but
        # the clamp keeps the function total).
        padded = text
        while len(padded) < MIN_DESCRIPTION_CHARS:
            padded += _DESCRIPTION_FILLER
        return padded[:MAX_DESCRIPTION_CHARS]

    def _build_seo_tags(
        self, idea: ContentIdea, seo_keywords: list[str]
    ) -> tuple[tuple[str, ...], bool]:
        """Build the SEO tag set and the ``seo_tags_unavailable`` flag.

        - No keywords supplied -> empty tag set + ``True`` (10.4).
        - Otherwise every supplied keyword is included (deduplicated, order
          preserved) and, when fewer than :data:`MIN_SEO_TAGS` distinct keywords
          were supplied, the set is augmented with deterministic derived tags up
          to the minimum (10.2). Analyzer keywords are never dropped.
        """
        deduped = self._dedupe_keywords(seo_keywords)

        # No usable keywords from the analyzer -> tags unavailable (10.4).
        if not deduped:
            return (), True

        tags = list(deduped)
        if len(tags) < MIN_SEO_TAGS:
            self._augment_tags(tags, idea)

        return tuple(tags), False

    @staticmethod
    def _dedupe_keywords(seo_keywords: list[str]) -> list[str]:
        """Return supplied keywords with blanks dropped and duplicates removed.

        Duplicates are detected case-insensitively (after stripping) so a
        keyword is included exactly once, while the first-seen original spelling
        is preserved.
        """
        seen: set[str] = set()
        result: list[str] = []
        for raw in seo_keywords:
            keyword = raw.strip()
            if not keyword:
                continue
            key = keyword.casefold()
            if key in seen:
                continue
            seen.add(key)
            result.append(keyword)
        return result

    def _augment_tags(self, tags: list[str], idea: ContentIdea) -> None:
        """Append deterministic derived tags in place until the minimum is met.

        Only invoked when at least one (but fewer than five) analyzer keyword was
        supplied, so this never drops or reorders the analyzer keywords already
        present; it only appends. Candidates are drawn from the idea (category,
        time window, title words, template names) and then generic fallbacks,
        which together always provide enough distinct tags to reach the minimum.
        """
        present = {t.casefold() for t in tags}
        for candidate in self._derived_candidates(idea):
            if len(tags) >= MIN_SEO_TAGS:
                return
            key = candidate.casefold()
            if key in present:
                continue
            present.add(key)
            tags.append(candidate)

    @staticmethod
    def _derived_candidates(idea: ContentIdea) -> list[str]:
        """Deterministic, idea-derived tag candidates followed by fallbacks."""
        candidates: list[str] = []

        if idea.category is not None:
            candidates.append(idea.category.value)
        candidates.append(idea.time_window.value)

        # Words from the title concept (lower-cased, alphanumeric runs).
        for word in _WORD_RE.findall(idea.title_concept.lower()):
            candidates.append(word)

        # Template names contribute topical tags.
        for template in idea.templates:
            for word in _WORD_RE.findall(template.name.lower()):
                candidates.append(word)

        # Generic fallbacks guarantee enough distinct candidates to reach the
        # minimum even for a sparse idea.
        candidates.extend(_FALLBACK_TAGS)
        return candidates
