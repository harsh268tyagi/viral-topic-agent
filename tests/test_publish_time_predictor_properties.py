"""Hypothesis property test for the Publish_Time_Predictor (task 16.2).

Example-based and edge-case tests live in ``test_publish_time_predictor.py``.
This module validates Property 16 for
:class:`~viral_topic_agent.publish_time_predictor.PublishTimePredictor`.

Property 16 (design.md -> Correctness Properties): *For any* owned-channel
audience activity covering at least 7 days, the produced recommendation SHALL
carry a day-of-week in ``0..6``, a contiguous window whose duration is in
``1..3`` hours, and a time-zone label equal to the Creator's configured time
zone, or ``"UTC"`` when none is configured.

The generator produces a genuine :class:`AudienceActivity` (>= 7 days covered,
with hourly buckets) and drives it through the real
:class:`ResilientDataSource` over a stub source, so the property exercises the
true retrieval + recommendation path with no mocking.

# Feature: viral-topic-agent, Property 16: Publish-time recommendation shape and time-zone selection

Validates: Requirements 9.2, 9.3
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from infrastructure.clock import FakeClock
from infrastructure.datasource import DataSource
from domain.models import AudienceActivity, HourlyActivity
from analysis.publish_time_predictor import PublishTimePredictor
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class _ActivitySource:
    """A :class:`DataSource` returning pre-canned audience activity.

    Only ``get_audience_activity`` is meaningful; the rest exist so the stub
    structurally satisfies the protocol. Wrapping it in a real
    ``ResilientDataSource`` keeps the property test free of mocks.
    """

    def __init__(self, activity: AudienceActivity) -> None:
        self._activity = activity

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity:
        return self._activity

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):  # pragma: no cover
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(activity: AudienceActivity) -> ResilientDataSource:
    source: DataSource = _ActivitySource(activity)
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


# ---------------------------------------------------------------------------
# Generators
#
# A usable activity series: >= 7 days covered and a non-empty set of hourly
# buckets with valid (day, hour) coordinates and non-negative activity.
# ---------------------------------------------------------------------------

_activity_value = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


@st.composite
def _audience_activity(draw: st.DrawFn) -> AudienceActivity:
    n_buckets = draw(st.integers(min_value=1, max_value=40))
    buckets = tuple(
        HourlyActivity(
            day_of_week=draw(st.integers(min_value=0, max_value=6)),
            hour=draw(st.integers(min_value=0, max_value=23)),
            activity=draw(_activity_value),
        )
        for _ in range(n_buckets)
    )
    days_covered = draw(st.integers(min_value=7, max_value=90))
    return AudienceActivity(
        channel_id="chan-prop",
        days_covered=days_covered,
        buckets=buckets,
    )


# Either no configured time zone (-> "UTC") or an IANA-style name string.
_tz = st.one_of(
    st.none(),
    st.sampled_from(
        [
            "UTC",
            "America/New_York",
            "Europe/London",
            "Asia/Tokyo",
            "Australia/Sydney",
            "America/Los_Angeles",
        ]
    ),
)


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 16: Publish-time recommendation shape and time-zone selection
@settings(max_examples=200)
@given(activity=_audience_activity(), tz=_tz)
def test_recommendation_shape_and_timezone(
    activity: AudienceActivity, tz: str | None
) -> None:
    """A >=7-day activity series yields a well-shaped, correctly-labelled rec.

    Validates: Requirements 9.2, 9.3
    """
    result = PublishTimePredictor().predict(
        channel_id="chan-prop",
        source=_resilient(activity),
        tz=tz,
        category_activity=None,
    )

    assert result.is_ok()
    rec = result.unwrap()

    # 9.2: exactly one day-of-week in 0..6.
    assert isinstance(rec.day_of_week, int)
    assert 0 <= rec.day_of_week <= 6

    # 9.2: exactly one contiguous window 1..3 hours in duration, starting within
    # the day and not wrapping past midnight (so the day is unambiguous).
    assert isinstance(rec.window_duration_hours, int)
    assert 1 <= rec.window_duration_hours <= 3
    assert 0 <= rec.window_start_hour <= 23
    assert rec.window_start_hour + rec.window_duration_hours <= 24

    # 9.2 / 9.3: the time-zone label equals the configured tz, or "UTC" when none.
    expected_tz = "UTC" if tz is None else tz
    assert rec.timezone == expected_tz
