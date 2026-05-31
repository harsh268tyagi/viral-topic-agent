"""Hypothesis property tests for idea scoring (task 8.2).

This module validates Property 8 for the :class:`~viral_topic_agent.scoring`
component. Example-based and edge-case tests live in ``test_scoring.py``;
Properties 9 and 10 are covered by their own tasks.

Property 8 (design.md): *For any* content idea and channel profile with an
available non-zero baseline, the idea score SHALL be an integer in [0, 100]
(bounded), SHALL be identical for identical inputs (deterministic), and SHALL
not decrease when an associated template's observed performance increases, all
else equal (monotonic non-decreasing).

Validates: Requirements 5.1, 5.2.
"""

from __future__ import annotations

import dataclasses

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.domain.models import (
    BaselineResult,
    ChannelCategory,
    Confidence,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from viral_topic_agent.analysis.scoring import compute_idea_score

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the valid input space for Property 8: ideas
# carry 1..5 templates (the model invariant) with finite, non-negative observed
# performance, and the baseline is available and strictly positive (the branch
# the property is scoped to).
# ---------------------------------------------------------------------------

# Non-negative, finite performance values. Capped well below float overflow so
# means and ratios stay representable.
_performance = st.floats(
    min_value=0.0,
    max_value=1e12,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _templates(draw: st.DrawFn) -> tuple[ViralTemplate, ...]:
    """1..5 viral templates with non-negative observed performance."""
    perfs = draw(st.lists(_performance, min_size=1, max_size=5))
    category = draw(st.sampled_from(list(ChannelCategory)))
    return tuple(
        ViralTemplate(
            template_id=f"t{i}",
            name=f"template-{i}",
            category=category,
            observed_performance=p,
        )
        for i, p in enumerate(perfs)
    )


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """A valid ContentIdea with 1..5 associated templates."""
    templates = draw(_templates())
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=20)),
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=draw(st.sampled_from(list(ChannelCategory))),
        templates=templates,
        observed_metric_value=draw(_performance),
    )


# Available, strictly-positive baseline -> the NORMAL-confidence scoring branch.
_available_baselines = st.builds(
    BaselineResult,
    value=st.floats(
        min_value=1e-3,
        max_value=1e12,
        allow_nan=False,
        allow_infinity=False,
    ),
    confidence=st.just(Confidence.NORMAL),
    sample_size=st.integers(min_value=5, max_value=10_000),
)


# ---------------------------------------------------------------------------
# Property 8
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 8: Idea score is a bounded, deterministic, monotonic integer
@settings(max_examples=200)
@given(idea=_ideas(), baseline=_available_baselines)
def test_idea_score_is_bounded_integer(idea: ContentIdea, baseline: BaselineResult):
    """Bounded: the score is an integer in [0, 100] (5.1, 5.2)."""
    outcome = compute_idea_score(idea, baseline, None)

    assert isinstance(outcome.score, int)
    assert not isinstance(outcome.score, bool)  # bool is an int subclass
    assert 0 <= outcome.score <= 100


# Feature: viral-topic-agent, Property 8: Idea score is a bounded, deterministic, monotonic integer
@settings(max_examples=200)
@given(idea=_ideas(), baseline=_available_baselines)
def test_idea_score_is_deterministic(idea: ContentIdea, baseline: BaselineResult):
    """Deterministic: identical inputs always yield identical output (5.1, 5.2)."""
    first = compute_idea_score(idea, baseline, None)
    second = compute_idea_score(idea, baseline, None)

    assert first == second


# Feature: viral-topic-agent, Property 8: Idea score is a bounded, deterministic, monotonic integer
@settings(max_examples=200)
@given(
    idea=_ideas(),
    baseline=_available_baselines,
    index=st.integers(min_value=0, max_value=4),
    increase=st.floats(
        min_value=0.0, max_value=1e12, allow_nan=False, allow_infinity=False
    ),
)
def test_idea_score_is_monotonic_in_template_performance(
    idea: ContentIdea,
    baseline: BaselineResult,
    index: int,
    increase: float,
):
    """Monotonic: raising one template's observed performance never lowers the
    score, all else equal (5.1, 5.2)."""
    target = index % len(idea.templates)
    original = idea.templates[target]
    bumped = dataclasses.replace(
        original, observed_performance=original.observed_performance + increase
    )
    bumped_idea = dataclasses.replace(
        idea,
        templates=idea.templates[:target] + (bumped,) + idea.templates[target + 1 :],
    )

    before = compute_idea_score(idea, baseline, None).score
    after = compute_idea_score(bumped_idea, baseline, None).score

    assert before is not None and after is not None
    assert after >= before
