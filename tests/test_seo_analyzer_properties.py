"""Property-based tests for the SEO_Analyzer keyword-gap classification.

This module hosts two Hypothesis properties for :func:`classify_keyword_gaps`:

- Property 20 (task 13.2): the percentile-rule classification is an exact
  "if and only if" over the analyzed keywords (Requirement 11.2).
- Property 21 (task 13.3): the returned gaps are exactly the qualifying
  keywords, ordered by descending demand then ascending competition
  (Requirement 11.3).

The 50th percentile is defined as the median of the analyzed values
(``statistics.median``: the middle value for odd samples, the mean of the two
middle values for even samples), matching the implementation and the design's
"50th percentile" language. Both thresholds are inclusive ("at or above" /
"at or below").

# Feature: viral-topic-agent, Property 20: Keyword-gap classification follows the percentile rule
# Feature: viral-topic-agent, Property 21: Keyword gaps are ordered by demand then competition
"""

from __future__ import annotations

from collections import Counter
from statistics import median

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.domain.models import KeywordMetric
from viral_topic_agent.analysis.seo_analyzer import MIN_ANALYZED_KEYWORDS, classify_keyword_gaps


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Demand and competition are drawn from a small, collision-prone value space so
# that ties on the median boundary (the inclusive "at or above"/"at or below"
# edge) and tie-breaks in ordering are exercised frequently rather than rarely.
_metric_value = st.floats(min_value=0.0, max_value=20.0, allow_nan=False, allow_infinity=False)


@st.composite
def _keywords(draw, min_size: int = MIN_ANALYZED_KEYWORDS, max_size: int = 30):
    """Build a list of KeywordMetric with distinct keyword ids.

    Distinct ids make the qualifying set a clean multiset of
    ``(keyword, demand, competition)`` triples to compare against, while demand
    and competition values are free to collide (the interesting boundary cases).
    """
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [
        KeywordMetric(
            keyword=f"k{i}",
            demand=draw(_metric_value),
            competition=draw(_metric_value),
        )
        for i in range(n)
    ]


def _expected_qualifying(keywords):
    """The keywords that satisfy the inclusive percentile rule (11.2).

    Independent of the implementation's filter+sort: a straightforward median
    threshold check used as the oracle for both properties.
    """
    demand_threshold = median(k.demand for k in keywords)
    competition_threshold = median(k.competition for k in keywords)
    return [
        k
        for k in keywords
        if k.demand >= demand_threshold and k.competition <= competition_threshold
    ]


# ---------------------------------------------------------------------------
# Property 20: classification follows the percentile rule (iff)
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(keywords=_keywords())
def test_keyword_gap_classification_follows_percentile_rule(keywords):
    """Property 20: with >= 4 analyzed keywords, a keyword is classified as a gap
    if and only if its demand is at or above the median demand and its
    competition is at or below the median competition.

    Validates: Requirements 11.2
    """
    result = classify_keyword_gaps(keywords)

    # With >= 4 keywords we never fall into the insufficient-data branch.
    assert result.insufficient_data is False

    expected = _expected_qualifying(keywords)

    # The gaps are exactly the qualifying keywords (same multiset of triples) -
    # nothing qualifying is dropped and nothing non-qualifying is included.
    produced_triples = Counter((g.keyword, g.demand, g.competition) for g in result.gaps)
    expected_triples = Counter((k.keyword, k.demand, k.competition) for k in expected)
    assert produced_triples == expected_triples

    # The no-gap indicator is set exactly when there are no qualifying keywords.
    assert result.no_gap == (len(expected) == 0)


# ---------------------------------------------------------------------------
# Property 21: gaps ordered by descending demand then ascending competition
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(keywords=_keywords())
def test_keyword_gaps_are_ordered_by_demand_then_competition(keywords):
    """Property 21: the returned gaps are exactly the qualifying keywords, ordered
    by descending search demand, ties broken by ascending competition.

    Validates: Requirements 11.3
    """
    result = classify_keyword_gaps(keywords)
    gaps = result.gaps

    # (a) Membership: the gaps are exactly the qualifying keywords (multiset).
    expected = _expected_qualifying(keywords)
    produced_triples = Counter((g.keyword, g.demand, g.competition) for g in gaps)
    expected_triples = Counter((k.keyword, k.demand, k.competition) for k in expected)
    assert produced_triples == expected_triples

    # (b) Ordering: every adjacent pair obeys descending demand, then ascending
    # competition on a demand tie.
    for current, nxt in zip(gaps, gaps[1:]):
        assert (current.demand, -current.competition) >= (nxt.demand, -nxt.competition)
        if current.demand == nxt.demand:
            assert current.competition <= nxt.competition
