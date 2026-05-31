"""Property-based tests for idea-scoring ordering and set preservation (task 8.3).

This module hosts Property 9 for the ``Idea_Scorer`` (Requirement 5.3). It is kept
separate from ``tests/test_scoring_properties.py`` (Properties 8 / 10, tasks 8.2 /
8.4) so the two tasks can be developed without colliding on the same file.

# Feature: viral-topic-agent, Property 9: Scored ideas are ordered by score then template performance and preserve the input set
"""

from __future__ import annotations

from collections import Counter

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.domain.models import (
    BaselineResult,
    ChannelCategory,
    ChannelProfile,
    Confidence,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from viral_topic_agent.analysis.scoring import CategoryAggregate, IdeaScorer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mean_template_performance(idea: ContentIdea) -> float:
    """Mean observed performance across an idea's templates.

    Mirrors the tie-break basis used by ``IdeaScorer.score`` (descending
    associated template observed performance). Ideas always carry 1..5
    templates, so the collection is non-empty.
    """
    templates = idea.templates
    if not templates:
        return 0.0
    return sum(t.observed_performance for t in templates) / len(templates)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Finite, non-negative performance values -- the input space the scorer is
# defined over (see design "Idea_Scorer" scoring model).
_perf = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)

_template = st.builds(
    ViralTemplate,
    template_id=st.text(min_size=1, max_size=8),
    name=st.just("tmpl"),
    category=st.just(ChannelCategory.GAMING),
    observed_performance=_perf,
)

# A Content_Idea with 1..5 associated templates (model invariant).
_idea = st.builds(
    ContentIdea,
    idea_id=st.text(min_size=1, max_size=8),
    title_concept=st.just("title"),
    rationale=st.just("observed metric value 1 within the window"),
    time_window=st.just(TimeWindow.WEEKLY),
    category=st.just(ChannelCategory.GAMING),
    templates=st.lists(_template, min_size=1, max_size=5).map(tuple),
    observed_metric_value=st.just(1.0),
)


@st.composite
def _profile_and_aggregate(draw):
    """Build a (ChannelProfile, CategoryAggregate|None) pair across all branches.

    Three scenarios are sampled so the ordering logic is exercised over scored
    ideas (NORMAL via baseline, LOW via aggregate) as well as the all-withheld
    branch:

    - ``baseline``: positive baseline -> every idea scored (NORMAL).
    - ``aggregate``: missing/zero baseline + usable aggregate -> scored (LOW).
    - ``withheld``: missing/zero baseline + no usable aggregate -> all withheld.
    """
    scenario = draw(st.sampled_from(["baseline", "aggregate", "withheld"]))

    if scenario == "baseline":
        baseline = BaselineResult(
            value=draw(st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)),
            confidence=Confidence.NORMAL,
            sample_size=30,
        )
        aggregate = draw(
            st.one_of(
                st.none(),
                st.builds(
                    CategoryAggregate,
                    category=st.just(ChannelCategory.GAMING),
                    aggregate_performance=st.floats(
                        min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False
                    ),
                ),
            )
        )
        return baseline, aggregate

    if scenario == "aggregate":
        baseline = BaselineResult(
            value=draw(st.one_of(st.none(), st.just(0.0))),
            confidence=Confidence.UNAVAILABLE,
            sample_size=0,
        )
        aggregate = CategoryAggregate(
            category=ChannelCategory.GAMING,
            aggregate_performance=draw(
                st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False)
            ),
        )
        return baseline, aggregate

    # withheld: neither a usable baseline nor a usable aggregate.
    baseline = BaselineResult(
        value=draw(st.one_of(st.none(), st.just(0.0))),
        confidence=Confidence.UNAVAILABLE,
        sample_size=0,
    )
    aggregate = draw(
        st.one_of(
            st.none(),
            st.builds(
                CategoryAggregate,
                category=st.just(ChannelCategory.GAMING),
                aggregate_performance=st.just(0.0),
            ),
        )
    )
    return baseline, aggregate


def _make_profile(baseline: BaselineResult) -> ChannelProfile:
    return ChannelProfile(
        channel_id="owned-1",
        detected_category=ChannelCategory.GAMING,
        subscriber_count=0,
        video_count=0,
        baseline=baseline,
    )


# ---------------------------------------------------------------------------
# Property 9
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(ideas=st.lists(_idea, max_size=12), profile_aggregate=_profile_and_aggregate())
def test_scored_ideas_preserve_set_and_are_correctly_ordered(ideas, profile_aggregate):
    """Property 9: the scored output is a permutation of the input ideas, ordered
    by descending score with ties broken by descending template performance, and
    any withheld ideas sort after every scored idea.

    Validates: Requirements 5.3
    """
    baseline, aggregate = profile_aggregate
    profile = _make_profile(baseline)

    result = IdeaScorer().score(ideas, profile, aggregate)

    # (a) Set preservation: exactly the same multiset of ideas, none added/dropped.
    assert Counter(scored.idea for scored in result) == Counter(ideas)

    # (b1) No scored idea may appear after a withheld idea.
    seen_withheld = False
    for scored in result:
        if scored.score is None:
            seen_withheld = True
        else:
            assert not seen_withheld, "a scored idea was placed after a withheld idea"

    # (b2) Scored ideas: non-increasing score, ties broken by non-increasing
    # associated template performance.
    scored_items = [s for s in result if s.score is not None]
    for current, nxt in zip(scored_items, scored_items[1:]):
        assert current.score >= nxt.score
        if current.score == nxt.score:
            assert _mean_template_performance(current.idea) >= _mean_template_performance(
                nxt.idea
            )

    # (b3) Withheld ideas are themselves ordered by non-increasing template
    # performance (deterministic tail ordering).
    withheld_items = [s for s in result if s.score is None]
    for current, nxt in zip(withheld_items, withheld_items[1:]):
        assert _mean_template_performance(current.idea) >= _mean_template_performance(
            nxt.idea
        )
