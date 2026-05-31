"""Hypothesis property test for Script_Generator SEO tags (task 14.2).

This module hosts Property 18 for :meth:`ScriptGenerator.generate`. Concrete,
hand-checked examples (every supplied keyword kept, augmentation to the minimum,
dedupe, blank handling) live in ``tests/test_script_generator.py``; this module
is the universal layer that asserts the superset-and-bounds contract across
arbitrary analyzer-keyword sets.

Property 18 (design.md -> Property 18 / Requirement 10.2): *for any* set of
analyzer-supplied keywords that admits a valid total, the generated SEO tag set
SHALL be a superset of those keywords AND SHALL contain between 5 and 30 tags in
total.

"Admits a valid total" scopes the property to the input space where both
clauses of 10.2 can hold simultaneously: at least one usable keyword (zero
keywords is the ``seo_tags_unavailable`` path of 10.4, a different requirement)
and no more than ``MAX_SEO_TAGS`` distinct keywords (supplying more than 30
distinct keywords cannot fit the upper bound while remaining a superset). The
generator below produces 1..30 distinct, non-blank keywords accordingly.

Tests exercise the real ``ScriptGenerator`` against the deterministic
``InMemoryGenerationProvider`` stub (no mocks).

# Feature: viral-topic-agent, Property 18: SEO tags include every analyzer keyword and stay within bounds
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from generation import InMemoryGenerationProvider
from domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from generation.script_generator import (
    MAX_SEO_TAGS,
    MIN_SEO_TAGS,
    ScriptGenerator,
)

# ---------------------------------------------------------------------------
# Strategies
#
# Keywords are distinct lower-case alphanumeric tokens (no whitespace), so each
# survives the implementation's strip + case-insensitive dedupe verbatim and the
# supplied set is already its own deduped form. The count is constrained to
# 1..MAX_SEO_TAGS: the lower bound stays out of the 10.4 (no-keyword) path and
# the upper bound keeps a valid total achievable (10.2's "admits a valid total").
# ---------------------------------------------------------------------------

_keyword = st.text(
    alphabet=st.characters(min_codepoint=ord("a"), max_codepoint=ord("z")),
    min_size=1,
    max_size=15,
)

_keyword_lists = st.lists(
    _keyword,
    min_size=1,
    max_size=MAX_SEO_TAGS,
    unique_by=lambda s: s.casefold(),
)


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """A valid ContentIdea with one associated template.

    Varied so the augmentation path (fewer than 5 keywords) draws its derived
    tag candidates from a range of titles, categories, and templates.
    """
    category = draw(st.one_of(st.none(), st.sampled_from(list(ChannelCategory))))
    template = ViralTemplate(
        template_id="t0",
        name=draw(st.text(min_size=1, max_size=20)),
        category=category if category is not None else ChannelCategory.GAMING,
        observed_performance=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=20)),
        title_concept=draw(st.text(min_size=1, max_size=40)),
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=category,
        templates=(template,),
        observed_metric_value=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )


# ---------------------------------------------------------------------------
# Property 18
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(idea=_ideas(), keywords=_keyword_lists)
def test_seo_tags_are_superset_within_bounds(
    idea: ContentIdea, keywords: list[str]
) -> None:
    """SEO tags are a superset of every supplied keyword and total 5..30.

    Validates: Requirements 10.2
    """
    result = ScriptGenerator().generate(idea, keywords, InMemoryGenerationProvider())

    assert result.is_ok()
    bundle = result.unwrap()

    # At least one usable keyword was supplied, so this is not the 10.4 path.
    assert bundle.seo_tags_unavailable is False

    tags = set(bundle.seo_tags)

    # (a) Superset: every analyzer-supplied keyword is present, none dropped.
    for keyword in keywords:
        assert keyword in tags

    # (b) Bounds: the total tag count is within [5, 30] inclusive (10.2).
    assert MIN_SEO_TAGS <= len(bundle.seo_tags) <= MAX_SEO_TAGS

    # The tag set carries no duplicates (case-insensitively).
    lowered = [t.casefold() for t in bundle.seo_tags]
    assert len(lowered) == len(set(lowered))
