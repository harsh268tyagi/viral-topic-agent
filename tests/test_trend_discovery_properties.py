"""Hypothesis property tests for the Trend_Discovery_Engine (tasks 7.2, 7.3).

Example-based and edge-case tests live in ``test_trend_discovery.py``. This
module validates the two universal properties of
:class:`~viral_topic_agent.trend_discovery.TrendDiscoveryEngine`:

- **Property 5** (design.md): *For any* per-window data availability, the
  discovery result SHALL contain between 1 and 20 content ideas for each
  requested window that has data, an empty result for each window with no data
  or that was not requested, and, for any window that times out or is
  unavailable, an empty result plus an error indication identifying that window
  - while every other requested window with data still produces 1-20 ideas.
  Validates: Requirements 3.1, 3.4, 3.5, 3.6, 3.7.

- **Property 6** (design.md): *For any* produced content idea, it SHALL be
  associated with between 1 and 5 viral templates, record the time window it was
  derived from, and include a rationale that references at least one observed
  performance metric value recorded within that same window.
  Validates: Requirements 3.2, 3.3.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from infrastructure.datasource import (
    DataRequest,
    DataSourceFailure,
    FailureClassification,
)
from domain.models import (
    ChannelCategory,
    TimeWindow,
    ViralTemplate,
)
from infrastructure.result import Err, Ok, Result
from analysis.trend_discovery import TrendDiscoveryEngine

# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------

_WINDOWS: tuple[TimeWindow, ...] = (
    TimeWindow.WEEKLY,
    TimeWindow.MONTHLY,
    TimeWindow.ALL_TIME,
)

_MAX_IDEAS_PER_WINDOW = 20
_MAX_TEMPLATES_PER_IDEA = 5


class StubSource:
    """A ``DiscoverySource`` driven by a per-window scripted response.

    ``responses`` maps a :class:`TimeWindow` to either a list of
    ``ViralTemplate`` (returned as an ``Ok`` payload) or a ``DataSourceFailure``
    (returned as an ``Err``). Every call is recorded so tests can assert which
    windows were (and were not) queried.
    """

    def __init__(
        self,
        responses: dict[TimeWindow, list[ViralTemplate] | DataSourceFailure],
    ) -> None:
        self._responses = responses
        self.calls: list[DataRequest] = []

    def call(self, request: DataRequest) -> Result[object, DataSourceFailure]:
        self.calls.append(request)
        window = TimeWindow(request.params["window"])
        response = self._responses.get(window, [])
        if isinstance(response, DataSourceFailure):
            return Err(response)
        return Ok(response)


def _window_from_request(request: DataRequest) -> TimeWindow:
    return TimeWindow(request.params["window"])


# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the input space the properties are scoped to:
# - templates carry a distinct id (so the engine's deterministic ranking has a
#   stable tie-break) and a finite, non-negative observed performance.
# - a window's "data availability" is one of: a non-empty template list (data),
#   an empty list (no data), or a DataSourceFailure (timed out / unavailable).
# - the requested set is any subset of the three windows (including empty), so
#   the non-requested branch (3.6) is exercised alongside the rest.
# ---------------------------------------------------------------------------

_categories = st.sampled_from(list(ChannelCategory))
_perf = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


@st.composite
def _template(draw: st.DrawFn, *, idx: int) -> ViralTemplate:
    return ViralTemplate(
        template_id=f"t{idx}",
        name=f"template-{idx}",
        category=draw(_categories),
        observed_performance=draw(_perf),
    )


@st.composite
def _template_list(
    draw: st.DrawFn, *, min_size: int, max_size: int, base: int
) -> list[ViralTemplate]:
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    return [draw(_template(idx=base + j)) for j in range(n)]


_failure = st.builds(
    DataSourceFailure,
    target=st.just("trend-discovery"),
    reason=st.sampled_from(["timeout", "unavailable", "rate-limit-timeout"]),
    classification=st.sampled_from(list(FailureClassification)),
)


@st.composite
def _availability_scenario(
    draw: st.DrawFn,
) -> tuple[dict[TimeWindow, list[ViralTemplate] | DataSourceFailure], set[TimeWindow]]:
    """A mixed scenario: each window is data / no-data / error; request a subset.

    Uses up to 25 templates for a data window so the 20-idea cap (3.1) is
    genuinely exercised.
    """
    responses: dict[TimeWindow, list[ViralTemplate] | DataSourceFailure] = {}
    for index, window in enumerate(_WINDOWS):
        kind = draw(st.sampled_from(["data", "empty", "error"]))
        if kind == "data":
            responses[window] = draw(
                _template_list(min_size=1, max_size=25, base=index * 100)
            )
        elif kind == "empty":
            responses[window] = []
        else:
            responses[window] = draw(_failure)
    requested = draw(st.sets(st.sampled_from(_WINDOWS)))
    return responses, requested


@st.composite
def _data_scenario(
    draw: st.DrawFn,
) -> dict[TimeWindow, list[ViralTemplate]]:
    """A scenario guaranteed to produce at least one idea (weekly has data).

    All three windows are requested; monthly/all-time may be empty. This keeps
    Property 6 meaningful (it quantifies over *produced* ideas).
    """
    return {
        TimeWindow.WEEKLY: draw(_template_list(min_size=1, max_size=25, base=0)),
        TimeWindow.MONTHLY: draw(_template_list(min_size=0, max_size=25, base=100)),
        TimeWindow.ALL_TIME: draw(_template_list(min_size=0, max_size=25, base=200)),
    }


# ---------------------------------------------------------------------------
# Property 5
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 5: Discovery cardinality and per-window isolation
@settings(max_examples=200)
@given(scenario=_availability_scenario())
def test_discovery_cardinality_and_per_window_isolation(
    scenario: tuple[
        dict[TimeWindow, list[ViralTemplate] | DataSourceFailure], set[TimeWindow]
    ],
) -> None:
    """Each requested window with data yields 1-20 ideas; windows with no data
    or that were not requested yield an empty result; a requested window that
    errored yields an empty result plus an identifying error - and one window's
    state never affects another (3.1, 3.4, 3.5, 3.6, 3.7)."""
    responses, requested = scenario
    source = StubSource(responses)

    result = TrendDiscoveryEngine().discover(source, windows=requested)

    # The result always carries all three canonical windows as keys.
    assert set(result.ideas_by_window) == set(_WINDOWS)

    for window in _WINDOWS:
        ideas = result.ideas_by_window[window]
        response = responses[window]

        if window not in requested:
            # 3.6: a non-requested window yields an empty result and no error.
            assert ideas == ()
            assert window not in result.window_errors
        elif isinstance(response, DataSourceFailure):
            # 3.7: a requested window that errored yields an empty result plus
            # an error indication that identifies the window.
            assert ideas == ()
            assert window in result.window_errors
            assert window.value in result.window_errors[window]
        elif len(response) == 0:
            # 3.4 / 3.5: a requested window with no data yields an empty result
            # and is NOT an error.
            assert ideas == ()
            assert window not in result.window_errors
        else:
            # 3.1: a requested window with data yields between 1 and 20 ideas.
            assert 1 <= len(ideas) <= _MAX_IDEAS_PER_WINDOW
            assert window not in result.window_errors

    # Errors are recorded only for requested windows that actually errored
    # (per-window isolation: a healthy or empty window never gains an error).
    expected_errors = {
        window
        for window in requested
        if isinstance(responses[window], DataSourceFailure)
    }
    assert set(result.window_errors) == expected_errors

    # 3.6: only the requested windows are ever queried.
    queried = {_window_from_request(call) for call in source.calls}
    assert queried == requested


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 6: Every content idea carries valid templates, window, and metric-backed rationale
@settings(max_examples=200)
@given(responses=_data_scenario())
def test_every_idea_carries_valid_templates_window_and_metric_rationale(
    responses: dict[TimeWindow, list[ViralTemplate]],
) -> None:
    """Every produced content idea is associated with 1-5 viral templates,
    records the window it was derived from, and carries a rationale that
    references an observed metric value recorded within that same window
    (3.2, 3.3)."""
    source = StubSource(responses)

    result = TrendDiscoveryEngine().discover(source, windows=set(_WINDOWS))

    produced_any = False
    for window, ideas in result.ideas_by_window.items():
        supplied_performances = {
            template.observed_performance for template in responses.get(window, [])
        }
        for idea in ideas:
            produced_any = True

            # 3.2: associated with between 1 and 5 viral templates.
            assert 1 <= len(idea.templates) <= _MAX_TEMPLATES_PER_IDEA

            # 3.3: records the time window it was derived from.
            assert idea.time_window == window

            # 3.3: the referenced metric value is one actually observed within
            # that same window's data...
            assert idea.observed_metric_value in supplied_performances
            # ...and the rationale references that metric value and the window.
            assert str(idea.observed_metric_value) in idea.rationale
            assert window.value in idea.rationale

            # The associated templates are themselves drawn from that window.
            for template in idea.templates:
                assert template.observed_performance in supplied_performances

    # The weekly window always has >= 1 template, so ideas are always produced;
    # this keeps the property from passing vacuously.
    assert produced_any
