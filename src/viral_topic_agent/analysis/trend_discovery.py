"""Trend_Discovery_Engine: discover trending content ideas per Time_Window.

This component implements Requirement 3 (Trend and Viral Idea Discovery). It is
an orchestration-plus-pure-logic unit: it retrieves observed performance data
for each requested :class:`~viral_topic_agent.models.TimeWindow` through a
``ResilientDataSource`` handle and turns that data into between 1 and 20
:class:`~viral_topic_agent.models.ContentIdea` objects per window, each
associated with 1-5 :class:`~viral_topic_agent.models.ViralTemplate` objects and
a rationale that references an observed metric value recorded within that
window.

Design reference: ``.kiro/specs/viral-topic-agent/design.md`` ->
*Trend_Discovery_Engine*::

    class TrendDiscoveryEngine:
        def discover(self, source: ResilientDataSource,
                     windows: set[TimeWindow] = ALL_WINDOWS) -> DiscoveryResult: ...

Behavior (Requirement 3 / design):

- 3.1  For each *requested* window that has data, produce 1-20 Content_Ideas.
- 3.2  Each Content_Idea is associated with between 1 and 5 Viral_Templates.
- 3.3  Each Content_Idea records the Time_Window it was derived from and a
       rationale that references at least one observed performance metric value
       recorded within that same window.
- 3.4  No data for *all* requested windows -> empty result for each window.
- 3.5  No data for a *single* requested window -> empty result for that window
       while every remaining requested window still produces 1-20 ideas.
- 3.6  Only the requested windows produce ideas; every non-requested window
       receives an empty result.
- 3.7  A window whose retrieval times out or is unavailable (the
       ``ResilientDataSource`` returns an ``Err``) receives an empty result plus
       an error indication identifying that window, while every other requested
       window still produces results.

Dependency boundary
--------------------
Per the design, every external ``Data_Source`` access funnels through
``ResilientDataSource``. To avoid a hard import dependency on that infrastructure
module (which may be implemented in parallel), this engine depends only on the
documented ``call(request) -> Result[Any, DataSourceFailure]`` interface,
expressed here as the :class:`DiscoverySource` ``Protocol``. Any object exposing
that method - including the real ``ResilientDataSource`` - satisfies it.

Latency criteria (3.8, and the 5 s per-window response bound in 3.7) are owned
by the resilience layer's timeout/retry policy and verified by integration
tests (task 20.1); from this engine's perspective a slow or unavailable window
simply surfaces as an ``Err`` from ``call`` and is handled as a per-window error.

Requirements traceability: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Protocol, runtime_checkable

from viral_topic_agent.infrastructure.datasource import DataOperation, DataRequest, DataSourceFailure
from viral_topic_agent.domain.models import ContentIdea, DiscoveryResult, TimeWindow, ViralTemplate
from viral_topic_agent.infrastructure.result import Result

__all__ = ["TrendDiscoveryEngine", "DiscoverySource", "ALL_WINDOWS"]


#: Every Time_Window the engine can discover for. Used as the default request
#: set so an unqualified ``discover`` call covers weekly, monthly, and all-time.
ALL_WINDOWS: frozenset[TimeWindow] = frozenset(
    {TimeWindow.WEEKLY, TimeWindow.MONTHLY, TimeWindow.ALL_TIME}
)

#: The canonical window order used when building the result so the
#: ``ideas_by_window`` mapping always carries the same, predictable key set.
_WINDOW_ORDER: tuple[TimeWindow, ...] = (
    TimeWindow.WEEKLY,
    TimeWindow.MONTHLY,
    TimeWindow.ALL_TIME,
)

#: Trailing-day span associated with each window. ``ALL_TIME`` maps to ``None``
#: meaning "the full available history" (no published-within filter).
_WINDOW_DAYS: dict[TimeWindow, int | None] = {
    TimeWindow.WEEKLY: 7,  # trailing 7 days
    TimeWindow.MONTHLY: 30,  # trailing 30 days
    TimeWindow.ALL_TIME: None,  # full history
}

#: Cardinality bounds mandated by Requirement 3.
_MAX_IDEAS_PER_WINDOW = 20  # (3.1)
_MAX_TEMPLATES_PER_IDEA = 5  # (3.2)

#: A title concept is later constrained to 1-100 chars by the Concept_Generator
#: (8.3); discovery keeps its working titles within that bound from the start.
_MAX_TITLE_CHARS = 100


@runtime_checkable
class DiscoverySource(Protocol):
    """The minimal data-source interface the engine depends on.

    This mirrors the documented ``ResilientDataSource.call`` contract so the
    engine can be wired to the real resilience layer without importing it. The
    ``Ok`` payload for a discovery request is a sequence of
    :class:`~viral_topic_agent.models.ViralTemplate` observed as trending within
    the requested window (each carrying the observed performance metric for that
    window); an ``Err`` carries a :class:`DataSourceFailure`.
    """

    def call(self, request: DataRequest) -> Result[Any, DataSourceFailure]: ...


class TrendDiscoveryEngine:
    """Discovers trending Content_Ideas per Time_Window (Requirement 3)."""

    def discover(
        self,
        source: DiscoverySource,
        windows: Collection[TimeWindow] = ALL_WINDOWS,
    ) -> DiscoveryResult:
        """Discover Content_Ideas for each requested ``Time_Window``.

        For each requested window the engine issues a single ``source.call``.
        On success it derives 1-20 ideas from the returned templates; on an
        empty payload it returns an empty result for that window; on an ``Err``
        it returns an empty result for that window and records an error
        indication identifying the window. Every other window is processed
        independently, so one window's failure never affects the rest (3.5,
        3.7). Non-requested windows always receive an empty result (3.6).

        Args:
            source: Anything satisfying :class:`DiscoverySource` (the real
                ``ResilientDataSource`` or a test double).
            windows: The windows to discover for. Defaults to all three.

        Returns:
            A :class:`~viral_topic_agent.models.DiscoveryResult` whose
            ``ideas_by_window`` always contains all three canonical windows
            (empty tuples for windows with no data, not requested, or errored)
            and whose ``window_errors`` maps each errored *requested* window to
            an identifying error indication.
        """
        requested = {w for w in windows if isinstance(w, TimeWindow)}

        ideas_by_window: dict[TimeWindow, tuple[ContentIdea, ...]] = {}
        window_errors: dict[TimeWindow, str] = {}

        for window in _WINDOW_ORDER:
            # 3.6: windows that were not requested receive an empty result and
            # are never queried.
            if window not in requested:
                ideas_by_window[window] = ()
                continue

            result = source.call(self._build_request(window))

            if result.is_err():
                # 3.7: a timed-out/unavailable window yields an empty result
                # plus an error indication that identifies the window. The other
                # requested windows continue to be processed by this loop.
                failure = result.unwrap_err()
                ideas_by_window[window] = ()
                window_errors[window] = self._error_indication(window, failure)
                continue

            templates = self._coerce_templates(result.unwrap())

            # 3.4 / 3.5: no data for this window -> empty result (no error).
            # 3.1: data present -> 1-20 ideas.
            ideas_by_window[window] = self._ideas_for_window(window, templates)

        return DiscoveryResult(
            ideas_by_window=ideas_by_window,
            window_errors=window_errors,
        )

    # -- Request construction ----------------------------------------------

    @staticmethod
    def _build_request(window: TimeWindow) -> DataRequest:
        """Build the reified ``DataRequest`` for a single window's discovery.

        The window is encoded both in the human-readable ``target`` (so a
        recorded failure identifies the window, per 16.6) and in ``params`` via
        its trailing-day span.
        """
        return DataRequest(
            operation=DataOperation.TEMPLATE_PERFORMANCE,
            target=f"trend-discovery:{window.value}",
            params={
                "window": window.value,
                "published_within_days": _WINDOW_DAYS[window],
            },
        )

    # -- Payload handling ---------------------------------------------------

    @staticmethod
    def _coerce_templates(payload: Any) -> list[ViralTemplate]:
        """Normalize an ``Ok`` payload to a list of :class:`ViralTemplate`.

        Accepts any sequence of templates (the documented contract) and returns
        a list. A ``None`` or empty payload normalizes to an empty list, which
        the caller treats as "no data for this window".
        """
        if payload is None:
            return []
        if isinstance(payload, Sequence):
            return [t for t in payload if isinstance(t, ViralTemplate)]
        # An unexpected payload shape is treated as no data rather than raising,
        # keeping a single malformed window from aborting the whole discovery.
        return []

    # -- Idea derivation ----------------------------------------------------

    def _ideas_for_window(
        self, window: TimeWindow, templates: list[ViralTemplate]
    ) -> tuple[ContentIdea, ...]:
        """Derive 1-20 Content_Ideas for ``window`` from its templates.

        Templates are ranked by descending observed performance (ties broken by
        ``template_id`` for determinism). One idea is produced per top template,
        up to 20. Each idea is anchored on a template and associated with a
        contiguous slice of 1-5 ranked templates beginning at the anchor.
        """
        if not templates:
            return ()

        ranked = sorted(
            templates,
            key=lambda t: (-t.observed_performance, t.template_id),
        )

        num_ideas = min(len(ranked), _MAX_IDEAS_PER_WINDOW)  # (3.1) 1..20
        ideas: list[ContentIdea] = []
        for index in range(num_ideas):
            anchor = ranked[index]
            # 3.2: associate 1-5 templates. Take up to five ranked templates
            # starting at the anchor; near the tail this naturally yields fewer
            # (but always at least the anchor itself).
            associated = tuple(
                ranked[index : index + _MAX_TEMPLATES_PER_IDEA]
            )
            ideas.append(self._build_idea(window, index, anchor, associated))

        return tuple(ideas)

    @staticmethod
    def _build_idea(
        window: TimeWindow,
        index: int,
        anchor: ViralTemplate,
        associated: tuple[ViralTemplate, ...],
    ) -> ContentIdea:
        """Construct a single Content_Idea (3.2, 3.3).

        ``observed_metric_value`` is the anchor template's observed performance -
        a real metric recorded within ``window`` - and the same value is
        embedded verbatim in the rationale so the rationale references an
        observed metric value from that window (3.3).
        """
        metric_value = float(anchor.observed_performance)

        title = f"{anchor.name} ({window.value} trend)"
        if len(title) > _MAX_TITLE_CHARS:
            title = title[:_MAX_TITLE_CHARS]

        rationale = (
            f"Derived from the {window.value} window: the viral template "
            f"'{anchor.name}' recorded an observed performance metric of "
            f"{metric_value} within this window."
        )

        return ContentIdea(
            idea_id=f"{window.value}-idea-{index + 1}",
            title_concept=title,
            rationale=rationale,
            time_window=window,
            category=anchor.category,
            templates=associated,
            observed_metric_value=metric_value,
        )

    # -- Error reporting ----------------------------------------------------

    @staticmethod
    def _error_indication(window: TimeWindow, failure: DataSourceFailure) -> str:
        """Build an error indication that identifies the affected window (3.7).

        The window is also the key in ``DiscoveryResult.window_errors``; the
        value adds the failure classification and reason so the indication is
        self-describing when rendered on its own.
        """
        return (
            f"{window.value}: {failure.classification.value} - {failure.reason} "
            f"(target={failure.target})"
        )
