"""Property-based test for idea-scoring degradation (task 8.4).

This module hosts Property 10 for the ``Idea_Scorer`` / ``compute_idea_score``
(Requirements 5.4, 5.5). It is kept in its own file -- mirroring the separation
between Property 8 (``tests/test_scoring_properties.py``) and Property 9
(``tests/test_scoring_ordering_properties.py``) -- so each scoring task owns a
distinct test module.

Property 10 (design.md -> Idea_Scorer):

- When the channel ``Baseline_View_Count`` is unavailable or zero *and* a usable
  Channel_Category aggregate is available, the score SHALL be computed from the
  aggregate and marked ``LOW`` confidence (5.4).
- When the baseline is unavailable or zero *and* the category aggregate is also
  unavailable, the score SHALL be withheld and an ``insufficient-data``
  indicator that identifies the idea SHALL be returned (5.5).

# Feature: viral-topic-agent, Property 10: Scoring degrades correctly when the baseline is missing
"""

from __future__ import annotations

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
from viral_topic_agent.analysis.scoring import CategoryAggregate, ScoreOutcome, compute_idea_score

# ---------------------------------------------------------------------------
# Strategies
#
# Smart generators constrained to the input space the degradation branches are
# defined over: ideas carry 1..5 templates (model invariant) with finite,
# non-negative observed performance, and the baseline is always *missing* (None
# value or value 0) so the baseline-available branch is never sampled here.
# ---------------------------------------------------------------------------

_performance = st.floats(
    min_value=0.0,
    max_value=1e12,
    allow_nan=False,
    allow_infinity=False,
)


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """A valid ContentIdea with 1..5 associated templates."""
    perfs = draw(st.lists(_performance, min_size=1, max_size=5))
    category = draw(st.sampled_from(list(ChannelCategory)))
    templates = tuple(
        ViralTemplate(
            template_id=f"t{i}",
            name=f"template-{i}",
            category=category,
            observed_performance=p,
        )
        for i, p in enumerate(perfs)
    )
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=20)),
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=draw(st.sampled_from(list(ChannelCategory))),
        templates=templates,
        observed_metric_value=draw(_performance),
    )


# A baseline that is *missing*: either no value at all, or exactly zero. Both
# trigger the degradation path (5.4 / 5.5) per the design.
_missing_baselines = st.builds(
    BaselineResult,
    value=st.sampled_from([None, 0.0]),
    confidence=st.sampled_from([Confidence.UNAVAILABLE, Confidence.LOW]),
    sample_size=st.integers(min_value=0, max_value=4),
)

# A *usable* category aggregate: strictly-positive aggregate performance.
_usable_aggregate = st.builds(
    CategoryAggregate,
    category=st.sampled_from(list(ChannelCategory)),
    aggregate_performance=st.floats(
        min_value=1e-3,
        max_value=1e12,
        allow_nan=False,
        allow_infinity=False,
    ),
)

# An *unusable* aggregate basis: either None or a non-positive aggregate (which
# the scorer treats as indistinguishable from having no reference at all).
_unusable_aggregate = st.one_of(
    st.none(),
    st.builds(
        CategoryAggregate,
        category=st.sampled_from(list(ChannelCategory)),
        aggregate_performance=st.floats(
            min_value=-1e6, max_value=0.0, allow_nan=False, allow_infinity=False
        ),
    ),
)


# ---------------------------------------------------------------------------
# Property 10
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 10: Scoring degrades correctly when the baseline is missing
@settings(max_examples=200)
@given(
    idea=_ideas(),
    baseline=_missing_baselines,
    aggregate=st.one_of(_usable_aggregate, _unusable_aggregate),
)
def test_scoring_degrades_when_baseline_missing(
    idea: ContentIdea,
    baseline: BaselineResult,
    aggregate: CategoryAggregate | None,
):
    """Property 10: with a missing/zero baseline, scoring falls back to a usable
    category aggregate at LOW confidence, or withholds the score with an
    insufficient-data indicator identifying the idea when no usable aggregate
    exists.

    Validates: Requirements 5.4, 5.5
    """
    outcome = compute_idea_score(idea, baseline, aggregate)

    assert isinstance(outcome, ScoreOutcome)
    # The outcome always identifies the idea, even when the score is withheld (5.5).
    assert outcome.idea_id == idea.idea_id

    aggregate_usable = aggregate is not None and aggregate.aggregate_performance > 0

    if aggregate_usable:
        # 5.4: scored from the category aggregate, marked low-confidence.
        assert outcome.score is not None
        assert isinstance(outcome.score, int)
        assert not isinstance(outcome.score, bool)  # bool is an int subclass
        assert 0 <= outcome.score <= 100
        assert outcome.confidence is Confidence.LOW
        assert outcome.insufficient_data is False
    else:
        # 5.5: no usable basis at all -> withhold score + insufficient-data.
        assert outcome.score is None
        assert outcome.insufficient_data is True
        assert outcome.confidence is Confidence.UNAVAILABLE
