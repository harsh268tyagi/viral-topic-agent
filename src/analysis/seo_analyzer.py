"""SEO and keyword-gap analysis (Requirement 11).

The :class:`SEOAnalyzer` retrieves search demand and competition data for
candidate keywords (through :class:`ResilientDataSource`) and classifies the
high-demand, low-competition "gaps" the Creator can realistically rank for. The
classification itself is a pure function, :func:`classify_keyword_gaps`,
extracted for direct property testing.

Design reference (``.kiro/specs/viral-topic-agent/design.md`` -> SEO_Analyzer):

- Retrieve up to ``1,000`` candidate keywords through the resilience layer (11.1).
- With **at least 4** analyzed keywords, classify a keyword as a gap when its
  search demand is **at or above the 50th percentile** of analyzed demand and
  its competition is **at or below the 50th percentile** of analyzed competition
  (11.2).
- Order the gaps by **descending demand**, breaking ties by **ascending
  competition** (11.3).
- No qualifying keyword -> empty result with a ``no_gap`` indicator (11.4).
- A Data_Source error -> no fresh result, retain any previously stored result,
  and provide an error indication identifying the retrieval failure (11.5).
- Fewer than 4 candidate keywords -> empty result with an ``insufficient_data``
  indicator (11.6).

The percentile rule (11.2)
--------------------------
The "50th percentile" is the **median** of the analyzed values, using the
standard definition: the middle value for an odd-sized sample, and the mean of
the two middle values for an even-sized sample (``statistics.median``). Because
both boundaries are inclusive ("at or above" / "at or below"), a keyword whose
demand or competition sits exactly on the median still qualifies on that axis.
Concretely a keyword ``k`` is a gap iff::

    k.demand >= median(demand of all analyzed keywords)
    and k.competition <= median(competition of all analyzed keywords)

The same median is used for ordering decisions nowhere else; ordering (11.3) is
a pure ``(-demand, competition)`` sort over the qualifying keywords.

Requirements traceability: 11.1, 11.2, 11.3, 11.4, 11.5, 11.6.
"""

from __future__ import annotations

from collections.abc import Sequence
from statistics import median

from infrastructure.datasource import DataOperation, DataRequest
from domain.models import ChannelCategory, KeywordGap, KeywordGapResult, KeywordMetric
from infrastructure.resilient_data_source import ResilientDataSource

__all__ = [
    "SEOAnalyzer",
    "classify_keyword_gaps",
    "MAX_CANDIDATE_KEYWORDS",
    "MIN_ANALYZED_KEYWORDS",
]

# Up to 1,000 candidate keywords are retrieved/analyzed per category (11.1).
MAX_CANDIDATE_KEYWORDS = 1000

# At least 4 analyzed keywords are required to classify gaps (11.2, 11.6).
MIN_ANALYZED_KEYWORDS = 4


def classify_keyword_gaps(keywords: Sequence[KeywordMetric]) -> KeywordGapResult:
    """Classify and order keyword gaps from analyzed ``keywords`` (pure).

    Args:
        keywords: The analyzed candidate keyword metrics. Order is irrelevant:
            classification depends only on the median thresholds and the result
            is sorted deterministically.

    Returns:
        A :class:`KeywordGapResult`:

        - Fewer than :data:`MIN_ANALYZED_KEYWORDS` keywords -> empty gaps with
          ``insufficient_data=True`` (11.6).
        - Otherwise the gaps are the keywords with demand at or above the median
          demand and competition at or below the median competition (11.2),
          ordered by descending demand then ascending competition (11.3).
        - When no keyword qualifies -> empty gaps with ``no_gap=True`` (11.4).
    """
    analyzed = list(keywords)

    # 11.6: fewer than 4 analyzed keywords -> insufficient data, no gaps.
    if len(analyzed) < MIN_ANALYZED_KEYWORDS:
        return KeywordGapResult(gaps=(), insufficient_data=True)

    # 11.2: the 50th-percentile thresholds are the medians of the analyzed
    # demand and competition values. Both comparisons are inclusive.
    demand_threshold = median(k.demand for k in analyzed)
    competition_threshold = median(k.competition for k in analyzed)

    qualifying = [
        KeywordGap(keyword=k.keyword, demand=k.demand, competition=k.competition)
        for k in analyzed
        if k.demand >= demand_threshold and k.competition <= competition_threshold
    ]

    # 11.4: no qualifying keyword -> empty result with a no-gap indicator.
    if not qualifying:
        return KeywordGapResult(gaps=(), no_gap=True)

    # 11.3: order by descending demand, breaking ties by ascending competition.
    qualifying.sort(key=lambda g: (-g.demand, g.competition))
    return KeywordGapResult(gaps=tuple(qualifying))


class SEOAnalyzer:
    """Retrieves keyword metrics and classifies keyword gaps (Requirement 11).

    The analyzer is stateful only in that it retains the most recent
    *successful* :class:`KeywordGapResult`. On a Data_Source error it returns an
    error-bearing result while leaving that retained result intact (11.5), so a
    transient retrieval failure never discards the last good analysis.
    """

    def __init__(self) -> None:
        self._last_result: KeywordGapResult | None = None

    @property
    def last_result(self) -> KeywordGapResult | None:
        """The most recent successfully classified result, if any (11.5)."""
        return self._last_result

    def analyze(
        self, category: ChannelCategory, source: ResilientDataSource
    ) -> KeywordGapResult:
        """Retrieve candidate keywords for ``category`` and classify gaps.

        Retrieves up to :data:`MAX_CANDIDATE_KEYWORDS` candidate keywords for
        ``category`` through ``source`` (11.1), then delegates to
        :func:`classify_keyword_gaps` (11.2-11.4, 11.6). On a retrieval failure
        the previously stored result is retained and an error-bearing result is
        returned identifying the failure (11.5).

        Args:
            category: The Channel_Category to analyze keywords for.
            source: The resilient data-source handle. All retrieval flows
                through it, so Requirement 16 handling is already applied and
                surfaced as a ``Result``.

        Returns:
            A :class:`KeywordGapResult`. On success it is the freshly classified
            result (also stored as :attr:`last_result`). On a Data_Source error
            it carries an ``error`` indication and surfaces the retained previous
            gaps, and :attr:`last_result` is left unchanged.
        """
        result = source.call(
            DataRequest(
                operation=DataOperation.KEYWORD_METRICS,
                target=category.value,
                params={"category": category, "max_keywords": MAX_CANDIDATE_KEYWORDS},
            )
        )

        # 11.5: Data_Source error -> no fresh classification. Retain the previous
        # result (do not overwrite ``_last_result``) and return an error
        # indication that names the retrieval failure, surfacing any retained
        # gaps so prior results remain available to the caller.
        if result.is_err():
            failure = result.unwrap_err()
            error = (
                f"keyword retrieval failed for {failure.target}: {failure.reason}"
            )
            retained_gaps = (
                self._last_result.gaps if self._last_result is not None else ()
            )
            return KeywordGapResult(gaps=retained_gaps, error=error)

        keywords = list(result.unwrap())
        # 11.1: classify over up to 1,000 candidate keywords; cap defensively in
        # case the source returns more than requested.
        if len(keywords) > MAX_CANDIDATE_KEYWORDS:
            keywords = keywords[:MAX_CANDIDATE_KEYWORDS]

        gap_result = classify_keyword_gaps(keywords)
        self._last_result = gap_result
        return gap_result
