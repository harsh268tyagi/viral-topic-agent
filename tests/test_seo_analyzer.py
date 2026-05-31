"""Example / branch tests for the SEO_Analyzer (task 13.4).

The universal properties for the percentile rule (Property 20) and the ordering
(Property 21) live in ``test_seo_analyzer_properties.py``. This module covers the
concrete boundary branches of :class:`SEOAnalyzer.analyze` and the pure
:func:`classify_keyword_gaps`:

- no candidate keyword meets the gap criteria -> empty result + ``no_gap`` (11.4);
- a Data_Source error -> error indication, previous results retained (11.5);
- fewer than 4 candidate keywords -> empty result + ``insufficient_data`` (11.6).

Where retrieval is involved, the tests drive the genuine
:class:`ResilientDataSource` over a scripted stub with a :class:`FakeClock`, so
retries/timeouts are exercised instantly and without mocking the resilience
layer.

Requirements exercised: 11.4, 11.5, 11.6.
"""

from __future__ import annotations

from viral_topic_agent.infrastructure.clock import FakeClock
from viral_topic_agent.infrastructure.datasource import (
    DataSource,
    NonTransientError,
)
from viral_topic_agent.domain.models import ChannelCategory, KeywordMetric
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy
from viral_topic_agent.analysis.seo_analyzer import (
    MIN_ANALYZED_KEYWORDS,
    SEOAnalyzer,
    classify_keyword_gaps,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _KeywordSource:
    """A :class:`DataSource` stub for ``get_keyword_metrics``.

    Either returns a scripted list of :class:`KeywordMetric` or raises a
    supplied error. Each call appends to ``calls`` for retrieval assertions.
    Only the keyword-metrics method is used by the SEO analyzer; the rest raise
    to make accidental use obvious.
    """

    def __init__(
        self,
        *,
        keywords: list[KeywordMetric] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._keywords = keywords
        self._error = error
        self.calls: list[dict] = []

    def get_keyword_metrics(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]:
        self.calls.append({"category": category, "max_keywords": max_keywords})
        if self._error is not None:
            raise self._error
        assert self._keywords is not None
        return list(self._keywords)

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):  # pragma: no cover
        raise NotImplementedError

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(source: DataSource) -> ResilientDataSource:
    # A FakeClock makes any retry backoff instant.
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


def _km(keyword: str, demand: float, competition: float) -> KeywordMetric:
    return KeywordMetric(keyword=keyword, demand=demand, competition=competition)


# ---------------------------------------------------------------------------
# 11.6: fewer than 4 candidate keywords -> insufficient-data
# ---------------------------------------------------------------------------


def test_pure_classify_fewer_than_four_is_insufficient_data():
    for n in range(0, MIN_ANALYZED_KEYWORDS):
        keywords = [_km(f"k{i}", demand=10.0, competition=1.0) for i in range(n)]
        result = classify_keyword_gaps(keywords)
        assert result.insufficient_data is True
        assert result.gaps == ()
        assert result.no_gap is False
        assert result.error is None


def test_analyze_fewer_than_four_candidates_is_insufficient_data():
    # Three candidates retrieved -> insufficient data (11.6).
    keywords = [
        _km("a", demand=90.0, competition=1.0),
        _km("b", demand=80.0, competition=2.0),
        _km("c", demand=70.0, competition=3.0),
    ]
    source = _KeywordSource(keywords=keywords)
    analyzer = SEOAnalyzer()

    result = analyzer.analyze(ChannelCategory.GAMING, _resilient(source))

    assert result.insufficient_data is True
    assert result.gaps == ()
    assert result.no_gap is False
    assert result.error is None
    # Retrieval requested up to 1,000 keywords for the selected category (11.1).
    assert source.calls == [{"category": ChannelCategory.GAMING, "max_keywords": 1000}]


# ---------------------------------------------------------------------------
# 11.4: no candidate meets the gap criteria -> no-gap indicator
# ---------------------------------------------------------------------------


def test_no_gap_when_demand_and_competition_are_inversely_related():
    # Constructed so that no single keyword is simultaneously high-demand AND
    # low-competition: demand and competition rise together, so the high-demand
    # keywords (>= median demand) all have high competition (> median), and the
    # low-competition keywords all have low demand (< median).
    keywords = [
        _km("a", demand=10.0, competition=10.0),
        _km("b", demand=20.0, competition=20.0),
        _km("c", demand=30.0, competition=30.0),
        _km("d", demand=40.0, competition=40.0),
    ]
    # median demand = 25, median competition = 25.
    # demand >= 25 -> {c(30,30), d(40,40)} but their competition (30,40) > 25.
    # competition <= 25 -> {a,b} but their demand (10,20) < 25. No overlap.
    result = classify_keyword_gaps(keywords)
    assert result.no_gap is True
    assert result.gaps == ()
    assert result.insufficient_data is False


def test_analyze_no_gap_indicator_via_source():
    keywords = [
        _km("a", demand=10.0, competition=10.0),
        _km("b", demand=20.0, competition=20.0),
        _km("c", demand=30.0, competition=30.0),
        _km("d", demand=40.0, competition=40.0),
    ]
    source = _KeywordSource(keywords=keywords)
    analyzer = SEOAnalyzer()

    result = analyzer.analyze(ChannelCategory.MUSIC, _resilient(source))

    assert result.no_gap is True
    assert result.gaps == ()
    assert result.error is None


# ---------------------------------------------------------------------------
# 11.5: source error -> error indication, previous results retained
# ---------------------------------------------------------------------------


def test_source_error_returns_error_and_retains_previous_results():
    # First, a successful analysis that produces gaps and is retained.
    good_keywords = [
        _km("alpha", demand=100.0, competition=1.0),
        _km("beta", demand=90.0, competition=2.0),
        _km("gamma", demand=10.0, competition=50.0),
        _km("delta", demand=5.0, competition=60.0),
    ]
    good_source = _KeywordSource(keywords=good_keywords)
    analyzer = SEOAnalyzer()

    first = analyzer.analyze(ChannelCategory.SPORTS, _resilient(good_source))
    assert first.error is None
    assert len(first.gaps) >= 1
    previous_gaps = first.gaps
    assert analyzer.last_result == first

    # Now a retrieval failure: error indication returned, previous gaps surfaced,
    # and the retained ``last_result`` is left unchanged (11.5).
    bad_source = _KeywordSource(error=NonTransientError("keyword service down"))
    second = analyzer.analyze(ChannelCategory.SPORTS, _resilient(bad_source))

    assert second.error is not None
    assert "keyword service down" in second.error
    assert second.gaps == previous_gaps
    # The stored "last good result" is not overwritten by the failure.
    assert analyzer.last_result == first


def test_source_error_with_no_previous_results_returns_empty_gaps():
    # A failure on the very first analysis -> error indication, empty gaps, and
    # no retained result.
    bad_source = _KeywordSource(error=NonTransientError("unavailable"))
    analyzer = SEOAnalyzer()

    result = analyzer.analyze(ChannelCategory.ENTERTAINMENT, _resilient(bad_source))

    assert result.error is not None
    assert "unavailable" in result.error
    assert result.gaps == ()
    assert analyzer.last_result is None


# ---------------------------------------------------------------------------
# Happy-path sanity for analyze (classification + ordering wired correctly)
# ---------------------------------------------------------------------------


def test_analyze_returns_ordered_gaps_on_success():
    # Five keywords so the median is the middle value (not an average of two).
    keywords = [
        _km("high-a", demand=90.0, competition=2.0),
        _km("high-b", demand=90.0, competition=1.0),
        _km("mid", demand=50.0, competition=5.0),
        _km("low-d", demand=20.0, competition=10.0),
        _km("low-e", demand=10.0, competition=10.0),
    ]
    source = _KeywordSource(keywords=keywords)
    analyzer = SEOAnalyzer()

    result = analyzer.analyze(ChannelCategory.GAMING, _resilient(source))

    assert result.error is None
    assert result.insufficient_data is False
    assert result.no_gap is False
    # median demand = 50, median competition = 5.
    # gaps: demand >= 50 AND competition <= 5 -> high-a(90,2), high-b(90,1), mid(50,5).
    # Ordered by descending demand then ascending competition: the two 90s tie on
    # demand, so high-b (competition 1) precedes high-a (competition 2), then mid.
    assert [g.keyword for g in result.gaps] == ["high-b", "high-a", "mid"]
