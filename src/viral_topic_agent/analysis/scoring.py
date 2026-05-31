"""Idea scoring (Requirement 5).

The :class:`IdeaScorer` assigns each :class:`~viral_topic_agent.models.ContentIdea`
an integer ``Idea_Score`` in ``[0, 100]`` representing predicted view potential
for the Owned_Channel, then orders the scored ideas for presentation.

Design references (``.kiro/specs/viral-topic-agent/design.md`` -> Idea_Scorer):

- The score is computed from the channel ``Baseline_View_Count`` and the observed
  performance of each Viral_Template associated with the idea (5.1, 5.2).
- Scored ideas are ordered by descending score, breaking ties by descending
  associated template observed performance (5.3).
- When the baseline is unavailable or zero, the Channel_Category aggregate
  performance is used as the basis and the score is marked ``LOW`` confidence
  (5.4). When both are unavailable, the score is withheld and an
  ``insufficient-data`` indicator identifying the idea is returned (5.5).

Scoring model
-------------
Predicted view potential is modelled as how the idea's associated template
performance compares to a *reference* view level (the channel baseline, or the
category aggregate when the baseline is missing):

    ``ratio = mean(template.observed_performance) / reference``
    ``score = round(100 * ratio / (ratio + 1))`` clamped to ``[0, 100]``

This map is:

- **Bounded** in ``[0, 100]`` for every finite, non-negative input (5.1).
- **Deterministic** -- identical inputs always produce identical output.
- **Monotonic non-decreasing** in each associated template's observed
  performance (increasing any template's performance can only raise the mean,
  hence the ratio, hence the score). This satisfies Property 8.

A saturating ratio map is used rather than a hard linear clamp so the score
retains granularity across the full range of inputs while still respecting the
bounds.

Requirements traceability: 5.1, 5.2, 5.3, 5.4, 5.5.
"""

from __future__ import annotations

from dataclasses import dataclass

from viral_topic_agent.domain.models import (
    BaselineResult,
    ChannelCategory,
    ChannelProfile,
    Confidence,
    ContentIdea,
    ScoredIdea,
)

__all__ = [
    "CategoryAggregate",
    "ScoreOutcome",
    "compute_idea_score",
    "IdeaScorer",
]


@dataclass(frozen=True)
class CategoryAggregate:
    """Aggregate view performance for a Channel_Category.

    Used as a fallback reference level when the Owned_Channel baseline is
    unavailable or zero (5.4). It is treated as *available* only when
    ``aggregate_performance`` is greater than zero; a non-positive aggregate is
    indistinguishable from having no usable reference and triggers the
    insufficient-data path (5.5).
    """

    category: ChannelCategory
    aggregate_performance: float  # substitute reference view level, > 0 when usable


@dataclass(frozen=True)
class ScoreOutcome:
    """The outcome of scoring a single idea.

    Either carries an integer ``score`` in ``[0, 100]`` with a confidence
    marker, or withholds the score (``score is None``) with
    ``insufficient_data=True``. ``idea_id`` always identifies the scored idea so
    a withheld outcome still names the affected idea (5.5).
    """

    idea_id: str
    score: int | None  # 0..100, or None when withheld
    confidence: Confidence
    insufficient_data: bool = False


def _mean_template_performance(idea: ContentIdea) -> float:
    """Mean observed performance across the idea's associated templates.

    Using the mean (rather than the max) makes the score monotonic in *each*
    template's observed performance, which is what Property 8 requires. An idea
    always has 1..5 templates per the model invariant; the empty guard keeps the
    function total and division-safe.
    """
    templates = idea.templates
    if not templates:
        return 0.0
    return sum(t.observed_performance for t in templates) / len(templates)


def _score_from_ratio(performance: float, reference: float) -> int:
    """Map performance relative to a reference level onto an integer ``[0, 100]``.

    ``reference`` is assumed positive (callers only invoke this with a usable
    baseline or category aggregate). The saturating map is strictly increasing
    in ``performance`` and bounded, then rounded and clamped for safety.
    """
    if performance <= 0.0:
        return 0
    ratio = performance / reference
    raw = 100.0 * ratio / (ratio + 1.0)
    return max(0, min(100, round(raw)))


def compute_idea_score(
    idea: ContentIdea,
    baseline: BaselineResult,
    category_aggregate: CategoryAggregate | None,
) -> ScoreOutcome:
    """Compute the :class:`ScoreOutcome` for a single idea.

    - When the baseline value is available and greater than zero, score against
      it with ``NORMAL`` confidence (5.1, 5.2).
    - Otherwise, when a usable category aggregate is available, score against it
      with ``LOW`` confidence (5.4).
    - Otherwise, withhold the score and flag insufficient data, identifying the
      idea (5.5).
    """
    performance = _mean_template_performance(idea)

    baseline_available = baseline.value is not None and baseline.value > 0
    if baseline_available:
        score = _score_from_ratio(performance, float(baseline.value))
        return ScoreOutcome(
            idea_id=idea.idea_id,
            score=score,
            confidence=Confidence.NORMAL,
            insufficient_data=False,
        )

    aggregate_available = (
        category_aggregate is not None and category_aggregate.aggregate_performance > 0
    )
    if aggregate_available:
        assert category_aggregate is not None  # narrowed by aggregate_available
        score = _score_from_ratio(
            performance, float(category_aggregate.aggregate_performance)
        )
        return ScoreOutcome(
            idea_id=idea.idea_id,
            score=score,
            confidence=Confidence.LOW,
            insufficient_data=False,
        )

    # Neither the baseline nor the category aggregate provides a usable basis.
    return ScoreOutcome(
        idea_id=idea.idea_id,
        score=None,
        confidence=Confidence.UNAVAILABLE,
        insufficient_data=True,
    )


class IdeaScorer:
    """Scores and orders content ideas for the Owned_Channel (Requirement 5)."""

    def score(
        self,
        ideas: list[ContentIdea],
        profile: ChannelProfile,
        category_aggregate: CategoryAggregate | None,
    ) -> list[ScoredIdea]:
        """Score every idea and return them ordered for presentation.

        Ordering (5.3): descending ``Idea_Score`` first, then -- for ideas that
        share an equal score -- descending associated template observed
        performance. Ideas whose score is withheld (insufficient data, 5.5) are
        placed after all scored ideas, ordered among themselves by descending
        template performance. The output is a permutation of the input set
        (Property 9).
        """
        scored: list[ScoredIdea] = []
        for idea in ideas:
            outcome = compute_idea_score(idea, profile.baseline, category_aggregate)
            scored.append(
                ScoredIdea(
                    idea=idea,
                    score=outcome.score,
                    confidence=outcome.confidence,
                    insufficient_data=outcome.insufficient_data,
                )
            )

        def sort_key(item: ScoredIdea) -> tuple[int, float, float]:
            performance = _mean_template_performance(item.idea)
            if item.score is None:
                # Withheld scores sort after every scored idea (first element 1
                # outranks 0); ties among them broken by descending performance.
                return (1, 0.0, -performance)
            # Scored ideas: descending score, then descending template performance.
            return (0, float(-item.score), -performance)

        scored.sort(key=sort_key)
        return scored
