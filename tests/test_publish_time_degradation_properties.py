"""Property-based test for publish-time degradation (task 16.3).

The recommendation-shape Property 16 lives in
``test_publish_time_predictor_properties.py``. This module hosts Property 17 for
:class:`~viral_topic_agent.publish_time_predictor.PublishTimePredictor`, kept in
its own file (mirroring the per-property separation used by the scoring tests).

Property 17 (design.md -> Publish_Time_Predictor): *For any* request where the
owned-channel audience activity is unavailable but a usable Channel_Category
aggregate activity is supplied, the predictor SHALL return a recommendation
derived from the category aggregate and marked ``LOW`` confidence (9.5).

"Owned-channel activity unavailable" is modelled by the source returning a
series that does not cover 7 days (here, an empty series with
``days_covered=0``), which the predictor treats as unavailable. The category
aggregate is always usable (>= 7 days, non-empty buckets), so the fallback path
(9.5) is exercised on every example.

# Feature: viral-topic-agent, Property 17: Publish-time recommendation degrades to category aggregate with low confidence

Validates: Requirements 9.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from infrastructure.clock import FakeClock
from infrastructure.datasource import DataSource
from domain.models import AudienceActivity, Confidence, HourlyActivity
from analysis.publish_time_predictor import (
    _pick_peak_window,
    PublishTimePredictor,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


class _UnavailableOwnedSource:
    """A :class:`DataSource` whose owned-channel activity is *unavailable*.

    The retrieval *succeeds* (so this is not a 9.4 retrieval failure) but the
    returned series covers 0 days with no buckets, which the predictor treats as
    unavailable -> the 9.5 category-aggregate fallback path.
    """

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity:
        return AudienceActivity(channel_id=channel_id, days_covered=0, buckets=())

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):  # pragma: no cover
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient() -> ResilientDataSource:
    source: DataSource = _UnavailableOwnedSource()
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


# ---------------------------------------------------------------------------
# Generators -- a usable category aggregate (>= 7 days, non-empty buckets).
# ---------------------------------------------------------------------------

_activity_value = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


@st.composite
def _category_activity(draw: st.DrawFn) -> AudienceActivity:
    n_buckets = draw(st.integers(min_value=1, max_value=40))
    buckets = tuple(
        HourlyActivity(
            day_of_week=draw(st.integers(min_value=0, max_value=6)),
            hour=draw(st.integers(min_value=0, max_value=23)),
            activity=draw(_activity_value),
        )
        for _ in range(n_buckets)
    )
    return AudienceActivity(
        channel_id=None,  # None marks a category aggregate
        days_covered=draw(st.integers(min_value=7, max_value=90)),
        buckets=buckets,
    )


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 17: Publish-time recommendation degrades to category aggregate with low confidence
@settings(max_examples=200)
@given(category=_category_activity())
def test_degrades_to_category_aggregate_with_low_confidence(
    category: AudienceActivity,
) -> None:
    """Unavailable owned activity + usable aggregate -> LOW-confidence rec (9.5).

    Validates: Requirements 9.5
    """
    result = PublishTimePredictor().predict(
        channel_id="chan-degrade",
        source=_resilient(),
        tz=None,
        category_activity=category,
    )

    assert result.is_ok()
    rec = result.unwrap()

    # 9.5: the recommendation is derived from the category aggregate and marked
    # low-confidence.
    assert rec.confidence is Confidence.LOW

    # It is still a well-shaped recommendation, and it is the one derived from
    # the *category* buckets (not the unavailable owned-channel data).
    expected_day, expected_start, expected_duration = _pick_peak_window(category.buckets)
    assert rec.day_of_week == expected_day
    assert rec.window_start_hour == expected_start
    assert rec.window_duration_hours == expected_duration
    assert 0 <= rec.day_of_week <= 6
    assert 1 <= rec.window_duration_hours <= 3
