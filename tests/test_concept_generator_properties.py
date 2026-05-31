"""Hypothesis property test for the Concept_Generator (task 12.3).

This module hosts Property 15 for :class:`ConceptGenerator.generate`. Concrete,
hand-checked examples and the normalization/failure branches live in
``tests/test_concept_generator.py``; this module is the universal layer that
asserts the *structural* contract holds across arbitrary content ideas, using
the deterministic :class:`InMemoryGenerationProvider` stub (no mocking).

Property 15 (design.md -> Concept_Generator): *for any* successfully generated
concept set it SHALL contain

- at least 3 *distinct* title concepts, each of length 1..100 characters
  inclusive (8.1, 8.3),
- at least one thumbnail concept with a (non-empty) visual description and a
  text overlay of at most 30 characters (8.2), and
- when the source idea has a category, the concept set SHALL belong to that
  category, i.e. it is tagged with the idea's category (8.4).

Validates: Requirements 8.1, 8.2, 8.3, 8.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from generation.concept_generator import (
    MAX_OVERLAY_CHARS,
    MAX_TITLE_CHARS,
    MIN_THUMBNAILS,
    MIN_TITLES,
    ConceptGenerator,
)
from generation import InMemoryGenerationProvider
from domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategy constrained to the property's scope: arbitrary, valid content
# ideas. The fields that actually drive the structural constraints are exercised
# broadly:
#   * `title_concept` is free-form text including empty/whitespace, very long,
#     and unicode strings, so title seeding, stripping, truncation, and the
#     1..100-char bound are all stressed;
#   * `category` ranges over every ChannelCategory *and* None, so the tagging
#     constraint (8.4) is checked in both the present and absent cases;
#   * `templates` carries the model invariant of 1..5 associated templates.
# The default InMemoryGenerationProvider is used (no failure injection, no supply
# ceiling), so a concept set is always produced; the assertions still guard on
# success to match the "for any successfully generated concept set" phrasing.
# ---------------------------------------------------------------------------

_categories = st.one_of(st.none(), st.sampled_from(list(ChannelCategory)))
# Free-form text including the empty string and whitespace-only values so the
# generator's strip/fallback/truncation paths are all exercised.
_title_concepts = st.text(min_size=0, max_size=300)
_finite_floats = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


@st.composite
def _templates(draw: st.DrawFn) -> tuple[ViralTemplate, ...]:
    """Between 1 and 5 associated viral templates (the model invariant)."""
    count = draw(st.integers(min_value=1, max_value=5))
    category = draw(st.sampled_from(list(ChannelCategory)))
    return tuple(
        ViralTemplate(
            template_id=f"t{i}",
            name="tier-list ranking",
            category=category,
            observed_performance=draw(_finite_floats),
        )
        for i in range(count)
    )


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """An arbitrary, valid ContentIdea."""
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=24)),
        title_concept=draw(_title_concepts),
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=draw(_categories),
        templates=draw(_templates()),
        observed_metric_value=draw(_finite_floats),
    )


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 15: Generated concept sets satisfy all structural constraints
@settings(max_examples=200)
@given(idea=_ideas())
def test_generated_concept_sets_satisfy_structural_constraints(
    idea: ContentIdea,
) -> None:
    """Every produced concept set meets the title/thumbnail/category contract.

    Validates: Requirements 8.1, 8.2, 8.3, 8.4
    """
    result = ConceptGenerator().generate(idea, InMemoryGenerationProvider())

    # The deterministic stub always supplies enough distinct, in-bound artifacts,
    # so generation succeeds; the property only constrains the success case.
    assert result.is_ok()
    concepts = result.unwrap()

    # (8.1) at least 3 title concepts, and (8.1) they are distinct.
    assert len(concepts.titles) >= MIN_TITLES
    texts = [t.text for t in concepts.titles]
    assert len(set(texts)) == len(texts)

    # (8.3) every title is within 1..100 characters inclusive.
    assert all(1 <= len(t.text) <= MAX_TITLE_CHARS for t in concepts.titles)

    # (8.2) at least one thumbnail, each with a non-empty visual description and
    # a text overlay of at most 30 characters.
    assert len(concepts.thumbnails) >= MIN_THUMBNAILS
    for thumb in concepts.thumbnails:
        assert thumb.visual_description != ""
        assert 1 <= len(thumb.text_overlay) <= MAX_OVERLAY_CHARS

    # (8.4) when the idea carries a category, the concept set belongs to it;
    # otherwise it carries no category.
    assert concepts.category is idea.category
