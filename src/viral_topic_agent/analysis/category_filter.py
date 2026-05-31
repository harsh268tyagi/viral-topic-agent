"""Category_Filter: restrict Content_Ideas and Viral_Templates to a category.

This component implements Requirement 4 (Category-Based Filtering). It is a pure
transformation: it consumes already-discovered ideas and templates plus the
Creator's selected category and the channel's detected category, and returns a
``FilterResult`` carrying either the filtered items or an explicit status
indicator. It performs no I/O and raises no exceptions for expected degraded
states (see design.md -> "Status as data, not exceptions").

Behavior (design.md -> Category_Filter):

- 4.1  Selected supported category -> return only the ideas and templates whose
       category matches the selected category.
- 4.2  No selection but a detected category exists -> apply the detected
       category as if it had been selected.
- 4.3  The supported categories are exactly gaming, music, entertainment, and
       sports (``CategoryFilter.SUPPORTED``).
- 4.4  Selected category matches no ideas but some templates -> return the
       matching templates (and the empty idea set).
- 4.5  Selected category matches neither ideas nor templates -> return an empty
       result with ``no_matches=True`` for the applied category.
- 4.6  Selected category outside the supported set -> reject with an
       ``unsupported-category`` error that identifies the offending category and
       apply no filtering.
- 4.7  No selection and no detected category -> return ``category_unavailable``
       and apply no filtering (the returned ideas/templates equal the inputs).

Note on 4.6: ``selected`` is typed as ``ChannelCategory | None`` and the
``ChannelCategory`` enum only contains supported values, so in normal use an
unsupported value cannot arise. We still validate defensively against
``SUPPORTED`` so that any out-of-set value a caller passes (for example a value
from a future or external category enum) is rejected with an identifying error
rather than silently filtering everything away.

Requirements traceability: 4.1, 4.2, 4.4, 4.5, 4.6, 4.7.
"""

from __future__ import annotations

from collections.abc import Sequence

from viral_topic_agent.domain.models import ChannelCategory, ContentIdea, FilterResult, ViralTemplate

__all__ = ["CategoryFilter"]


class CategoryFilter:
    """Restricts ideas and templates to a single Channel_Category (Requirement 4)."""

    #: The supported Channel_Categories (4.3). Exactly gaming, music,
    #: entertainment, and sports.
    SUPPORTED: frozenset[ChannelCategory] = frozenset(
        {
            ChannelCategory.GAMING,
            ChannelCategory.MUSIC,
            ChannelCategory.ENTERTAINMENT,
            ChannelCategory.SPORTS,
        }
    )

    def filter(
        self,
        ideas: Sequence[ContentIdea],
        templates: Sequence[ViralTemplate],
        selected: ChannelCategory | None,
        detected: ChannelCategory | None,
    ) -> FilterResult:
        """Filter ``ideas`` and ``templates`` by the applied Channel_Category.

        The applied category is the selected category when present, otherwise the
        detected category. See the module docstring for the full branch behavior.
        """
        # 4.6: a selected category outside the supported set is rejected with an
        # error that identifies it; no filtering is applied.
        if selected is not None and selected not in self.SUPPORTED:
            return FilterResult(
                ideas=(),
                templates=(),
                error=f"unsupported-category: {self._identify(selected)}",
            )

        # 4.1 / 4.2: the selected category takes precedence; otherwise fall back
        # to the category detected during channel analysis.
        applied = selected if selected is not None else detected

        # 4.7: nothing selected and nothing detected -> no filtering at all. The
        # returned items equal the inputs and the category-unavailable indicator
        # is set.
        if applied is None:
            return FilterResult(
                ideas=tuple(ideas),
                templates=tuple(templates),
                category_unavailable=True,
                applied_category=None,
            )

        # 4.1 / 4.2 / 4.4: keep only items matching the applied category.
        matching_ideas = tuple(idea for idea in ideas if idea.category == applied)
        matching_templates = tuple(
            template for template in templates if template.category == applied
        )

        # 4.5: no matching ideas AND no matching templates -> empty + no-matches.
        if not matching_ideas and not matching_templates:
            return FilterResult(
                ideas=(),
                templates=(),
                no_matches=True,
                applied_category=applied,
            )

        # 4.1 (matching ideas + templates), 4.2 (detected applied), and 4.4
        # (only templates match) all return the matching items.
        return FilterResult(
            ideas=matching_ideas,
            templates=matching_templates,
            applied_category=applied,
        )

    @staticmethod
    def _identify(category: object) -> str:
        """Return a human-readable identifier for an (unsupported) category.

        Prefers the enum ``value`` (e.g. "cooking"), then ``name``, then falls
        back to ``str`` so the 4.6 error can always identify the offending input.
        """
        value = getattr(category, "value", None)
        if value is not None:
            return str(value)
        name = getattr(category, "name", None)
        if name is not None:
            return str(name)
        return str(category)
