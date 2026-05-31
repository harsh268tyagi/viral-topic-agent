"""Unit and edge-case tests for the Trend_Discovery_Engine (task 7.1).

Covers the concrete branches of Requirement 3:

- 3.1  Each requested window with data produces 1-20 Content_Ideas.
- 3.2  Each Content_Idea is associated with 1-5 Viral_Templates.
- 3.3  Each Content_Idea records its window and a rationale referencing at least
       one observed metric value recorded within that window.
- 3.4  No data for all requested windows -> empty result for every window.
- 3.5  No data for one window -> empty for that window, others still produce.
- 3.6  Only requested windows produce ideas; non-requested windows are empty.
- 3.7  A window that errors (timeout/unavailable) -> empty for that window plus
       an identifying error indication, while other windows still produce.

Properties 5 and 6 (Hypothesis) live in tasks 7.2 / 7.3 and are intentionally
not included here.
"""

from __future__ import annotations

import pytest

from infrastructure.datasource import (
    DataRequest,
    DataSourceFailure,
    FailureClassification,
)
from domain.models import (
    ChannelCategory,
    ContentIdea,
    DiscoveryResult,
    TimeWindow,
    ViralTemplate,
)
from infrastructure.result import Err, Ok, Result
from analysis.trend_discovery import (
    ALL_WINDOWS,
    TrendDiscoveryEngine,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class StubSource:
    """A ``DiscoverySource`` driven by a per-window scripted response.

    ``responses`` maps a :class:`TimeWindow` to either:
      - a list of ``ViralTemplate`` (an ``Ok`` payload), or
      - a ``DataSourceFailure`` (returned as an ``Err``).

    A window absent from ``responses`` returns an empty ``Ok`` payload (i.e.
    "no data for this window"). Every call is recorded in ``calls`` so tests can
    assert which windows were (and were not) queried.
    """

    def __init__(
        self, responses: dict[TimeWindow, list[ViralTemplate] | DataSourceFailure]
    ) -> None:
        self._responses = responses
        self.calls: list[DataRequest] = []

    def call(self, request: DataRequest) -> Result[object, DataSourceFailure]:
        self.calls.append(request)
        window = _window_from_request(request)
        response = self._responses.get(window, [])
        if isinstance(response, DataSourceFailure):
            return Err(response)
        return Ok(response)


def _window_from_request(request: DataRequest) -> TimeWindow:
    return TimeWindow(request.params["window"])


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _template(
    template_id: str,
    performance: float,
    category: ChannelCategory = ChannelCategory.GAMING,
) -> ViralTemplate:
    return ViralTemplate(
        template_id=template_id,
        name=f"template-{template_id}",
        category=category,
        observed_performance=performance,
    )


def _templates(n: int, category: ChannelCategory = ChannelCategory.GAMING) -> list[ViralTemplate]:
    """``n`` templates with distinct descending performances."""
    return [
        _template(f"t{i}", performance=float(1000 - i), category=category)
        for i in range(n)
    ]


@pytest.fixture
def engine() -> TrendDiscoveryEngine:
    return TrendDiscoveryEngine()


# ---------------------------------------------------------------------------
# 3.1 - 1..20 ideas per requested window with data
# ---------------------------------------------------------------------------


def test_each_requested_window_with_data_produces_between_1_and_20_ideas(engine):
    source = StubSource(
        {
            TimeWindow.WEEKLY: _templates(3),
            TimeWindow.MONTHLY: _templates(10),
            TimeWindow.ALL_TIME: _templates(50),  # more than 20 templates
        }
    )

    result = engine.discover(source)

    assert isinstance(result, DiscoveryResult)
    for window in (TimeWindow.WEEKLY, TimeWindow.MONTHLY, TimeWindow.ALL_TIME):
        ideas = result.ideas_by_window[window]
        assert 1 <= len(ideas) <= 20, window
    # The all-time window had 50 templates but is capped at 20 ideas (3.1).
    assert len(result.ideas_by_window[TimeWindow.ALL_TIME]) == 20
    assert result.window_errors == {}


def test_single_template_yields_exactly_one_idea(engine):
    source = StubSource({TimeWindow.WEEKLY: _templates(1)})
    result = engine.discover(source, windows={TimeWindow.WEEKLY})
    assert len(result.ideas_by_window[TimeWindow.WEEKLY]) == 1


def test_ideas_are_capped_at_twenty(engine):
    source = StubSource({TimeWindow.WEEKLY: _templates(100)})
    result = engine.discover(source, windows={TimeWindow.WEEKLY})
    assert len(result.ideas_by_window[TimeWindow.WEEKLY]) == 20


# ---------------------------------------------------------------------------
# 3.2 - each idea associated with 1..5 templates
# ---------------------------------------------------------------------------


def test_each_idea_has_between_one_and_five_templates(engine):
    source = StubSource({TimeWindow.MONTHLY: _templates(12)})
    result = engine.discover(source, windows={TimeWindow.MONTHLY})

    ideas = result.ideas_by_window[TimeWindow.MONTHLY]
    assert ideas
    for idea in ideas:
        assert 1 <= len(idea.templates) <= 5, idea.idea_id


def test_tail_ideas_have_at_least_one_template(engine):
    # With exactly 3 templates the last idea's 5-wide window has only 1 left.
    source = StubSource({TimeWindow.WEEKLY: _templates(3)})
    result = engine.discover(source, windows={TimeWindow.WEEKLY})
    ideas = result.ideas_by_window[TimeWindow.WEEKLY]
    assert [len(i.templates) for i in ideas] == [3, 2, 1]


# ---------------------------------------------------------------------------
# 3.3 - window recorded + rationale references an observed metric value
# ---------------------------------------------------------------------------


def test_each_idea_records_its_window_and_metric_backed_rationale(engine):
    templates = [_template("hot", performance=987.0)]
    source = StubSource({TimeWindow.ALL_TIME: templates})

    result = engine.discover(source, windows={TimeWindow.ALL_TIME})
    idea = result.ideas_by_window[TimeWindow.ALL_TIME][0]

    assert isinstance(idea, ContentIdea)
    # Window is recorded on the idea (3.3).
    assert idea.time_window == TimeWindow.ALL_TIME
    # The observed metric value comes from a template within the window.
    assert idea.observed_metric_value == 987.0
    # The rationale references that observed metric value and the window.
    assert "987.0" in idea.rationale
    assert TimeWindow.ALL_TIME.value in idea.rationale


def test_rationale_metric_value_matches_an_associated_template(engine):
    source = StubSource({TimeWindow.WEEKLY: _templates(6)})
    result = engine.discover(source, windows={TimeWindow.WEEKLY})

    for idea in result.ideas_by_window[TimeWindow.WEEKLY]:
        observed_values = {t.observed_performance for t in idea.templates}
        # The referenced metric value is one actually observed for an
        # associated template within the window.
        assert idea.observed_metric_value in observed_values
        assert str(idea.observed_metric_value) in idea.rationale


# ---------------------------------------------------------------------------
# 3.4 - no data for all requested windows -> empty per window
# ---------------------------------------------------------------------------


def test_no_data_for_all_windows_returns_empty_results(engine):
    source = StubSource({})  # every window returns an empty Ok payload

    result = engine.discover(source)

    assert set(result.ideas_by_window) == {
        TimeWindow.WEEKLY,
        TimeWindow.MONTHLY,
        TimeWindow.ALL_TIME,
    }
    assert all(ideas == () for ideas in result.ideas_by_window.values())
    # No data is not an error condition (3.4) - errors are reserved for 3.7.
    assert result.window_errors == {}


# ---------------------------------------------------------------------------
# 3.5 - no data for one window -> empty there, others still produce
# ---------------------------------------------------------------------------


def test_one_empty_window_does_not_block_others(engine):
    source = StubSource(
        {
            TimeWindow.WEEKLY: [],  # explicitly empty -> no data
            TimeWindow.MONTHLY: _templates(4),
            TimeWindow.ALL_TIME: _templates(7),
        }
    )

    result = engine.discover(source)

    assert result.ideas_by_window[TimeWindow.WEEKLY] == ()
    assert 1 <= len(result.ideas_by_window[TimeWindow.MONTHLY]) <= 20
    assert 1 <= len(result.ideas_by_window[TimeWindow.ALL_TIME]) <= 20
    assert result.window_errors == {}


# ---------------------------------------------------------------------------
# 3.6 - only requested windows produce; non-requested are empty
# ---------------------------------------------------------------------------


def test_only_requested_windows_are_queried_and_produced(engine):
    source = StubSource(
        {
            TimeWindow.WEEKLY: _templates(5),
            TimeWindow.MONTHLY: _templates(5),
            TimeWindow.ALL_TIME: _templates(5),
        }
    )

    result = engine.discover(source, windows={TimeWindow.MONTHLY})

    # Requested window has ideas...
    assert len(result.ideas_by_window[TimeWindow.MONTHLY]) >= 1
    # ...non-requested windows are empty even though the source had data.
    assert result.ideas_by_window[TimeWindow.WEEKLY] == ()
    assert result.ideas_by_window[TimeWindow.ALL_TIME] == ()
    # Only the requested window was actually queried.
    queried = {_window_from_request(c) for c in source.calls}
    assert queried == {TimeWindow.MONTHLY}


def test_empty_requested_set_produces_all_empty_and_no_calls(engine):
    source = StubSource({TimeWindow.WEEKLY: _templates(5)})
    result = engine.discover(source, windows=set())

    assert all(ideas == () for ideas in result.ideas_by_window.values())
    assert source.calls == []


# ---------------------------------------------------------------------------
# 3.7 - errored window -> empty + identifying error; others still produce
# ---------------------------------------------------------------------------


def test_errored_window_is_isolated_with_identifying_error(engine):
    failure = DataSourceFailure(
        target="trend-discovery:weekly",
        reason="no complete response within 5s",
        classification=FailureClassification.TRANSIENT,
        attempts=3,
    )
    source = StubSource(
        {
            TimeWindow.WEEKLY: failure,
            TimeWindow.MONTHLY: _templates(6),
            TimeWindow.ALL_TIME: _templates(6),
        }
    )

    result = engine.discover(source)

    # Errored window: empty result + an error indication identifying it (3.7).
    assert result.ideas_by_window[TimeWindow.WEEKLY] == ()
    assert TimeWindow.WEEKLY in result.window_errors
    assert TimeWindow.WEEKLY.value in result.window_errors[TimeWindow.WEEKLY]

    # Other requested windows still produce results.
    assert len(result.ideas_by_window[TimeWindow.MONTHLY]) >= 1
    assert len(result.ideas_by_window[TimeWindow.ALL_TIME]) >= 1
    # Only the errored window is recorded as an error.
    assert set(result.window_errors) == {TimeWindow.WEEKLY}


def test_all_windows_erroring_records_all_errors_and_no_ideas(engine):
    def _fail(window: TimeWindow) -> DataSourceFailure:
        return DataSourceFailure(
            target=f"trend-discovery:{window.value}",
            reason="unavailable",
            classification=FailureClassification.NON_TRANSIENT,
        )

    source = StubSource(
        {
            TimeWindow.WEEKLY: _fail(TimeWindow.WEEKLY),
            TimeWindow.MONTHLY: _fail(TimeWindow.MONTHLY),
            TimeWindow.ALL_TIME: _fail(TimeWindow.ALL_TIME),
        }
    )

    result = engine.discover(source)

    assert all(ideas == () for ideas in result.ideas_by_window.values())
    assert set(result.window_errors) == {
        TimeWindow.WEEKLY,
        TimeWindow.MONTHLY,
        TimeWindow.ALL_TIME,
    }


def test_error_in_one_window_does_not_appear_for_healthy_windows(engine):
    failure = DataSourceFailure(
        target="trend-discovery:all_time",
        reason="rate-limit-timeout",
        classification=FailureClassification.RATE_LIMIT_TIMEOUT,
    )
    source = StubSource(
        {
            TimeWindow.WEEKLY: _templates(2),
            TimeWindow.ALL_TIME: failure,
        }
    )

    result = engine.discover(source)

    assert TimeWindow.WEEKLY not in result.window_errors
    assert TimeWindow.MONTHLY not in result.window_errors  # empty, not errored
    assert TimeWindow.ALL_TIME in result.window_errors


# ---------------------------------------------------------------------------
# Defaults & request construction
# ---------------------------------------------------------------------------


def test_default_windows_cover_all_three(engine):
    assert ALL_WINDOWS == {
        TimeWindow.WEEKLY,
        TimeWindow.MONTHLY,
        TimeWindow.ALL_TIME,
    }
    source = StubSource({w: _templates(2) for w in ALL_WINDOWS})
    result = engine.discover(source)  # no windows argument -> all three
    queried = {_window_from_request(c) for c in source.calls}
    assert queried == ALL_WINDOWS


def test_request_encodes_window_and_trailing_days(engine):
    source = StubSource({w: [] for w in ALL_WINDOWS})
    engine.discover(source)

    by_window = {_window_from_request(c): c for c in source.calls}
    assert by_window[TimeWindow.WEEKLY].params["published_within_days"] == 7
    assert by_window[TimeWindow.MONTHLY].params["published_within_days"] == 30
    assert by_window[TimeWindow.ALL_TIME].params["published_within_days"] is None
    # Each request target identifies its window for failure reporting (16.6).
    for window, req in by_window.items():
        assert window.value in req.target


def test_ideas_have_distinct_ids_within_a_window(engine):
    source = StubSource({TimeWindow.MONTHLY: _templates(15)})
    result = engine.discover(source, windows={TimeWindow.MONTHLY})
    ids = [i.idea_id for i in result.ideas_by_window[TimeWindow.MONTHLY]]
    assert len(ids) == len(set(ids))


def test_idea_category_matches_its_anchor_template(engine):
    source = StubSource(
        {TimeWindow.WEEKLY: _templates(4, category=ChannelCategory.MUSIC)}
    )
    result = engine.discover(source, windows={TimeWindow.WEEKLY})
    for idea in result.ideas_by_window[TimeWindow.WEEKLY]:
        assert idea.category == ChannelCategory.MUSIC
