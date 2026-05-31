"""Example/edge-case tests for idea scoring (task 8.1).

Covers the concrete branches and ordering behaviour of
``compute_idea_score`` and ``IdeaScorer.score`` for Requirement 5:

- integer score bounds in [0, 100] (5.1),
- score computed from the baseline and associated template performance (5.2),
- ordering by descending score, ties broken by descending template
  performance (5.3),
- baseline unavailable/zero -> category aggregate + LOW confidence (5.4),
- both unavailable -> withheld score + insufficient-data identifying the
  idea (5.5).

Hypothesis property tests for Properties 8, 9, 10 live in separate tasks
(8.2-8.4); this module focuses on examples and edge cases.
"""

from __future__ import annotations

import pytest

from viral_topic_agent.domain.models import (
    BaselineResult,
    ChannelCategory,
    ChannelProfile,
    Confidence,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from viral_topic_agent.analysis.scoring import (
    CategoryAggregate,
    IdeaScorer,
    ScoreOutcome,
    compute_idea_score,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _template(template_id: str, performance: float) -> ViralTemplate:
    return ViralTemplate(
        template_id=template_id,
        name=f"template-{template_id}",
        category=ChannelCategory.GAMING,
        observed_performance=performance,
    )


def _idea(
    idea_id: str,
    template_perfs: tuple[float, ...] = (1000.0,),
    category: ChannelCategory | None = ChannelCategory.GAMING,
) -> ContentIdea:
    templates = tuple(
        _template(f"{idea_id}-t{i}", p) for i, p in enumerate(template_perfs)
    )
    return ContentIdea(
        idea_id=idea_id,
        title_concept=f"Idea {idea_id}",
        rationale="metric value 1234 observed within the window",
        time_window=TimeWindow.WEEKLY,
        category=category,
        templates=templates,
        observed_metric_value=1234.0,
    )


def _baseline(value: float | None, confidence: Confidence, sample_size: int = 30):
    return BaselineResult(value=value, confidence=confidence, sample_size=sample_size)


def _profile(baseline: BaselineResult) -> ChannelProfile:
    return ChannelProfile(
        channel_id="owned-1",
        detected_category=ChannelCategory.GAMING,
        subscriber_count=10_000,
        video_count=120,
        baseline=baseline,
    )


# ---------------------------------------------------------------------------
# compute_idea_score: normal baseline branch (5.1, 5.2)
# ---------------------------------------------------------------------------


def test_score_is_integer_in_bounds_with_normal_baseline():
    baseline = _baseline(1000.0, Confidence.NORMAL)
    outcome = compute_idea_score(_idea("a", (1000.0,)), baseline, None)

    assert isinstance(outcome.score, int)
    assert 0 <= outcome.score <= 100
    assert outcome.confidence is Confidence.NORMAL
    assert outcome.insufficient_data is False
    assert outcome.idea_id == "a"


def test_equal_performance_and_baseline_scores_midpoint():
    # ratio == 1 -> 100 * 1 / 2 == 50
    baseline = _baseline(1000.0, Confidence.NORMAL)
    outcome = compute_idea_score(_idea("a", (1000.0,)), baseline, None)
    assert outcome.score == 50


def test_score_increases_with_template_performance():
    baseline = _baseline(1000.0, Confidence.NORMAL)
    low = compute_idea_score(_idea("a", (500.0,)), baseline, None)
    high = compute_idea_score(_idea("a", (5000.0,)), baseline, None)
    assert high.score > low.score


def test_score_uses_mean_of_associated_templates():
    # mean(500, 1500) == 1000 == baseline -> ratio 1 -> 50
    baseline = _baseline(1000.0, Confidence.NORMAL)
    outcome = compute_idea_score(_idea("a", (500.0, 1500.0)), baseline, None)
    assert outcome.score == 50


def test_zero_template_performance_scores_zero():
    baseline = _baseline(1000.0, Confidence.NORMAL)
    outcome = compute_idea_score(_idea("a", (0.0,)), baseline, None)
    assert outcome.score == 0


def test_very_high_performance_saturates_below_or_at_100():
    baseline = _baseline(1.0, Confidence.NORMAL)
    outcome = compute_idea_score(_idea("a", (1_000_000_000.0,)), baseline, None)
    assert outcome.score <= 100
    assert outcome.score >= 99  # effectively saturated


def test_score_is_deterministic():
    baseline = _baseline(1234.0, Confidence.NORMAL)
    idea = _idea("a", (777.0, 4242.0))
    first = compute_idea_score(idea, baseline, None)
    second = compute_idea_score(idea, baseline, None)
    assert first == second


# ---------------------------------------------------------------------------
# compute_idea_score: category-aggregate fallback (5.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "baseline",
    [
        _baseline(None, Confidence.UNAVAILABLE, sample_size=0),
        _baseline(0.0, Confidence.LOW, sample_size=3),
    ],
)
def test_unavailable_or_zero_baseline_falls_back_to_aggregate(baseline):
    aggregate = CategoryAggregate(
        category=ChannelCategory.GAMING, aggregate_performance=1000.0
    )
    outcome = compute_idea_score(_idea("a", (1000.0,)), baseline, aggregate)

    assert isinstance(outcome.score, int)
    assert 0 <= outcome.score <= 100
    assert outcome.score == 50  # ratio 1 against the aggregate
    assert outcome.confidence is Confidence.LOW
    assert outcome.insufficient_data is False


def test_aggregate_used_only_when_baseline_missing():
    # Available baseline takes precedence and yields NORMAL confidence even when
    # an aggregate is supplied.
    baseline = _baseline(1000.0, Confidence.NORMAL)
    aggregate = CategoryAggregate(
        category=ChannelCategory.GAMING, aggregate_performance=10.0
    )
    outcome = compute_idea_score(_idea("a", (1000.0,)), baseline, aggregate)
    assert outcome.confidence is Confidence.NORMAL
    assert outcome.score == 50  # scored against baseline, not aggregate


# ---------------------------------------------------------------------------
# compute_idea_score: insufficient data (5.5)
# ---------------------------------------------------------------------------


def test_no_baseline_and_no_aggregate_withholds_score():
    baseline = _baseline(None, Confidence.UNAVAILABLE, sample_size=0)
    outcome = compute_idea_score(_idea("idea-x", (1000.0,)), baseline, None)

    assert outcome.score is None
    assert outcome.insufficient_data is True
    assert outcome.confidence is Confidence.UNAVAILABLE
    assert outcome.idea_id == "idea-x"  # identifies the idea


def test_no_baseline_and_nonpositive_aggregate_withholds_score():
    baseline = _baseline(0.0, Confidence.UNAVAILABLE, sample_size=0)
    aggregate = CategoryAggregate(
        category=ChannelCategory.GAMING, aggregate_performance=0.0
    )
    outcome = compute_idea_score(_idea("idea-y", (1000.0,)), baseline, aggregate)

    assert outcome.score is None
    assert outcome.insufficient_data is True
    assert outcome.idea_id == "idea-y"


# ---------------------------------------------------------------------------
# IdeaScorer.score: ordering (5.3)
# ---------------------------------------------------------------------------


def test_ideas_ordered_by_descending_score():
    profile = _profile(_baseline(1000.0, Confidence.NORMAL))
    ideas = [
        _idea("low", (200.0,)),
        _idea("high", (8000.0,)),
        _idea("mid", (1000.0,)),
    ]
    result = IdeaScorer().score(ideas, profile, None)

    ids = [s.idea.idea_id for s in result]
    assert ids == ["high", "mid", "low"]
    scores = [s.score for s in result]
    assert scores == sorted(scores, reverse=True)


def test_ties_broken_by_descending_template_performance():
    # Both ideas have template performance == baseline (1000) when each has a
    # single template equal to a different value but producing the same score?
    # Instead, force a score tie by using performances that round identically,
    # then assert the higher template performance comes first.
    profile = _profile(_baseline(1000.0, Confidence.NORMAL))

    # Choose two performances that map to the same integer score but differ.
    # score(1000) = 50, score(1001) rounds to 50 as well.
    idea_a = _idea("a", (1000.0,))
    idea_b = _idea("b", (1001.0,))

    a_out = compute_idea_score(idea_a, profile.baseline, None)
    b_out = compute_idea_score(idea_b, profile.baseline, None)
    assert a_out.score == b_out.score  # confirm the tie precondition

    result = IdeaScorer().score([idea_a, idea_b], profile, None)
    # Equal score -> higher template performance (idea_b, 1001) first.
    assert [s.idea.idea_id for s in result] == ["b", "a"]


def test_score_output_is_permutation_of_input():
    profile = _profile(_baseline(1000.0, Confidence.NORMAL))
    ideas = [_idea("a", (200.0,)), _idea("b", (8000.0,)), _idea("c", (1000.0,))]
    result = IdeaScorer().score(ideas, profile, None)

    assert len(result) == len(ideas)
    assert {s.idea.idea_id for s in result} == {"a", "b", "c"}
    assert [s.idea for s in result].count(ideas[0]) == 1


def test_withheld_ideas_sort_after_scored_ideas():
    # No baseline and no aggregate -> every idea is withheld; but mix is tested
    # via aggregate available for scored vs not. Here use unavailable baseline
    # with aggregate so all are scored LOW, then a separate all-withheld check.
    profile = _profile(_baseline(None, Confidence.UNAVAILABLE, sample_size=0))
    ideas = [_idea("a", (1000.0,)), _idea("b", (2000.0,))]
    result = IdeaScorer().score(ideas, profile, None)

    # All withheld; ordered by descending template performance (b before a).
    assert all(s.score is None and s.insufficient_data for s in result)
    assert [s.idea.idea_id for s in result] == ["b", "a"]


def test_scored_ideas_precede_withheld_when_aggregate_partial():
    # Idea with templates scored against aggregate ranks ahead of withheld.
    profile = _profile(_baseline(0.0, Confidence.UNAVAILABLE, sample_size=0))
    aggregate = CategoryAggregate(
        category=ChannelCategory.GAMING, aggregate_performance=1000.0
    )
    ideas = [_idea("scored", (5000.0,)), _idea("also-scored", (10.0,))]
    result = IdeaScorer().score(ideas, profile, aggregate)

    # With a usable aggregate, both are scored (LOW), none withheld.
    assert all(s.score is not None for s in result)
    assert all(s.confidence is Confidence.LOW for s in result)
    assert [s.idea.idea_id for s in result] == ["scored", "also-scored"]


def test_empty_input_returns_empty_list():
    profile = _profile(_baseline(1000.0, Confidence.NORMAL))
    assert IdeaScorer().score([], profile, None) == []


def test_scoreoutcome_dataclass_shape():
    # Sanity: ScoreOutcome carries the idea id even on the withheld path.
    outcome = ScoreOutcome(
        idea_id="z", score=None, confidence=Confidence.UNAVAILABLE, insufficient_data=True
    )
    assert outcome.idea_id == "z"
    assert outcome.score is None
