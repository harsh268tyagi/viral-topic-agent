"""Hypothesis property tests for the Category_Filter (task 7.5).

Example-based and edge-case tests live in ``test_category_filter.py``. This
module validates Property 7 for :class:`~viral_topic_agent.category_filter.CategoryFilter`.

Property 7 (design.md): *For any* mixed-category set of ideas and templates and
a selected category (or, when none is selected, the detected category), every
returned idea and template SHALL match the applied category and no matching item
SHALL be dropped; when no category is selected and none was detected, the result
SHALL carry a ``category-unavailable`` indicator and apply no filtering (outputs
equal inputs).

Validates: Requirements 4.1, 4.2, 4.7.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.analysis.category_filter import CategoryFilter
from viral_topic_agent.domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the input space Property 7 is scoped to:
# - ``selected`` is drawn from ``ChannelCategory | None``. The enum only carries
#   supported members, so the unsupported-category branch (4.6, Property not in
#   scope) is never reached.
# - ideas carry a category that may be any ``ChannelCategory`` OR ``None`` (the
#   model allows an idea without a category), so the generated set is genuinely
#   mixed-category.
# - each idea carries the 1..5 templates the model invariant requires; those
#   nested templates are irrelevant to filtering (which inspects ``idea.category``
#   and the top-level templates' ``category``) so their categories are arbitrary.
# ---------------------------------------------------------------------------

_categories = st.sampled_from(list(ChannelCategory))
_optional_category = st.one_of(st.none(), _categories)


@st.composite
def _template(draw: st.DrawFn, *, idx: int) -> ViralTemplate:
    return ViralTemplate(
        template_id=f"t{idx}",
        name=f"template-{idx}",
        category=draw(_categories),
        observed_performance=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )


@st.composite
def _idea(draw: st.DrawFn, *, idx: int) -> ContentIdea:
    # 1..5 nested templates satisfy the ContentIdea model invariant; their
    # categories do not affect filtering of the top-level idea.
    nested = tuple(
        draw(_template(idx=idx * 10 + j))
        for j in range(draw(st.integers(min_value=1, max_value=5)))
    )
    return ContentIdea(
        idea_id=f"i{idx}",
        title_concept=f"title-{idx}",
        rationale="metric value observed within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=draw(_optional_category),
        templates=nested,
        observed_metric_value=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )


@st.composite
def _ideas(draw: st.DrawFn) -> list[ContentIdea]:
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(_idea(idx=i)) for i in range(n)]


@st.composite
def _templates(draw: st.DrawFn) -> list[ViralTemplate]:
    n = draw(st.integers(min_value=0, max_value=8))
    return [draw(_template(idx=1000 + i)) for i in range(n)]


# ---------------------------------------------------------------------------
# Property 7
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 7: Category filtering returns only matching items or the correct indicator
@settings(max_examples=200)
@given(
    ideas=_ideas(),
    templates=_templates(),
    selected=_optional_category,
    detected=_optional_category,
)
def test_applied_category_returns_only_matching_and_drops_nothing(
    ideas: list[ContentIdea],
    templates: list[ViralTemplate],
    selected: ChannelCategory | None,
    detected: ChannelCategory | None,
):
    """When an applied category exists (selected, else detected), the result
    contains exactly the input items matching that category - every returned
    item matches it (only matching) and no matching input item is dropped
    (4.1, 4.2)."""
    applied = selected if selected is not None else detected
    if applied is None:
        # Scoped to the branch where a category is applied; the no-category
        # branch is exercised by the dedicated test below.
        return

    result = CategoryFilter().filter(
        ideas=ideas, templates=templates, selected=selected, detected=detected
    )

    # No unsupported-category error and no category-unavailable indicator when a
    # supported category is applied.
    assert result.error is None
    assert result.category_unavailable is False
    assert result.applied_category == applied

    expected_ideas = tuple(i for i in ideas if i.category == applied)
    expected_templates = tuple(t for t in templates if t.category == applied)

    # Only matching items are returned AND no matching item is dropped (the
    # returned items are exactly the matching subset, order preserved).
    assert result.ideas == expected_ideas
    assert result.templates == expected_templates

    # Defensive restatement of "only matching": nothing non-matching leaks.
    assert all(i.category == applied for i in result.ideas)
    assert all(t.category == applied for t in result.templates)


# Feature: viral-topic-agent, Property 7: Category filtering returns only matching items or the correct indicator
@settings(max_examples=200)
@given(ideas=_ideas(), templates=_templates())
def test_no_category_available_applies_no_filtering(
    ideas: list[ContentIdea],
    templates: list[ViralTemplate],
):
    """When no category is selected and none was detected, the result carries a
    category-unavailable indicator and applies no filtering: the outputs equal
    the inputs (4.7)."""
    result = CategoryFilter().filter(
        ideas=ideas, templates=templates, selected=None, detected=None
    )

    assert result.category_unavailable is True
    assert result.applied_category is None
    assert result.no_matches is False
    assert result.error is None
    # No filtering: outputs equal inputs (order preserved).
    assert result.ideas == tuple(ideas)
    assert result.templates == tuple(templates)
