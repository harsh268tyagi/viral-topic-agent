"""Hypothesis property test for competitor monitoring isolation (task 9.4).

Validates Property 13 for
:meth:`~viral_topic_agent.competitor_tracker.CompetitorTracker.monitor`.

Property 13 (design.md -> Correctness Properties): *For any* set of monitored
competitors, a competitor with fewer than 5 retrieved videos SHALL be marked
``insufficient-data`` with no spike detection, an unavailable competitor SHALL
be marked ``unavailable``, and in both cases every other competitor SHALL still
be monitored.

The test builds a configuration whose competitors each fall into one of three
generated kinds - ``unavailable`` (the Data_Source raises), ``insufficient``
(fewer than 5 videos), or ``ok`` (>= 5 videos) - and asserts that every
competitor receives exactly the report its kind dictates, regardless of the
other competitors. This exercises the isolation guarantee: a degraded
competitor never suppresses or corrupts the others' reports.

# Feature: viral-topic-agent, Property 13: Competitor monitoring isolates insufficient or unavailable channels

Validates: Requirements 6.6, 6.7
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.infrastructure.clock import FakeClock
from viral_topic_agent.analysis.competitor_tracker import CompetitorTracker
from viral_topic_agent.infrastructure.datasource import DataSource, NonTransientError
from viral_topic_agent.domain.models import Configuration, VideoStats
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy

_BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)
_MIN_RETRIEVED_VIDEOS = 5


# ---------------------------------------------------------------------------
# Test double: a multi-channel source driven by a per-channel script.
# ---------------------------------------------------------------------------


class _ScriptedSource:
    """Serves a per-channel video list, or raises for unavailable channels.

    ``videos_by_channel`` maps a channel id to the videos it should return.
    ``unavailable`` is the set of channel ids for which ``get_videos`` raises a
    :class:`NonTransientError`, modelling a competitor the Data_Source cannot
    serve (6.7).
    """

    def __init__(
        self,
        videos_by_channel: dict[str, list[VideoStats]],
        unavailable: set[str],
    ) -> None:
        self._videos_by_channel = videos_by_channel
        self._unavailable = unavailable

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        if channel_id in self._unavailable:
            raise NonTransientError(f"channel {channel_id} not found")
        return list(self._videos_by_channel.get(channel_id, []))

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _videos(channel_id: str, count: int) -> list[VideoStats]:
    """``count`` distinct-date videos for ``channel_id`` with healthy views."""
    return [
        VideoStats(
            video_id=f"{channel_id}-v{i}",
            view_count=100,
            published_at=(_BASE_DATE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            format=None,
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# Generator: a list of (channel_id, kind) where kind in {ok, insufficient,
# unavailable}. Channel ids are unique so each maps to one kind.
# ---------------------------------------------------------------------------

_kinds = st.sampled_from(["ok", "insufficient", "unavailable"])


@st.composite
def _competitor_plan(draw: st.DrawFn) -> list[tuple[str, str]]:
    ids = draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=2, max_size=5),
            unique=True,
            min_size=1,
            max_size=8,
        )
    )
    return [(channel_id, draw(_kinds)) for channel_id in ids]


# Feature: viral-topic-agent, Property 13: Competitor monitoring isolates insufficient or unavailable channels
@settings(max_examples=200)
@given(plan=_competitor_plan())
def test_monitoring_isolates_degraded_competitors(
    plan: list[tuple[str, str]],
) -> None:
    """Each competitor gets the report its kind dictates, independent of others.

    Validates: Requirements 6.6, 6.7
    """
    videos_by_channel: dict[str, list[VideoStats]] = {}
    unavailable: set[str] = set()
    kind_by_channel: dict[str, str] = {}

    for index, (channel_id, kind) in enumerate(plan):
        kind_by_channel[channel_id] = kind
        if kind == "unavailable":
            unavailable.add(channel_id)
        elif kind == "insufficient":
            # 0..4 videos (below the 5-video threshold). Vary the count by index
            # so both the empty and the 1-4 sub-cases are exercised.
            videos_by_channel[channel_id] = _videos(
                channel_id, index % _MIN_RETRIEVED_VIDEOS
            )
        else:  # ok
            videos_by_channel[channel_id] = _videos(channel_id, _MIN_RETRIEVED_VIDEOS)

    config = Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=tuple(channel_id for channel_id, _ in plan),
        schedule=None,
        delivery_destinations=(),
    )
    source = ResilientDataSource(
        _ScriptedSource(videos_by_channel, unavailable), RetryPolicy(), FakeClock()
    )

    reports = CompetitorTracker().monitor(config, source)

    # Every monitored competitor is reported, in order: none is dropped because
    # of another's degraded state (isolation).
    assert [r.channel_id for r in reports] == [cid for cid, _ in plan]

    for report in reports:
        kind = kind_by_channel[report.channel_id]
        if kind == "unavailable":
            # 6.7: unavailable status, no baseline, no spikes.
            assert report.status == "unavailable"
            assert report.baseline is None
            assert report.spikes == ()
        elif kind == "insufficient":
            # 6.6: insufficient-data status, spike detection skipped.
            assert report.status == "insufficient-data"
            assert report.spikes == ()
        else:  # ok
            assert report.status == "ok"
            assert report.baseline is not None
