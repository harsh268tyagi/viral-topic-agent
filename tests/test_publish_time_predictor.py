"""Example / edge-case tests for the Publish_Time_Predictor (task 16.4).

The universal Properties 16 and 17 live in
``test_publish_time_predictor_properties.py`` and
``test_publish_time_degradation_properties.py``. This module covers the concrete
edge branches of
:class:`~viral_topic_agent.publish_time_predictor.PublishTimePredictor`:

- audience-activity retrieval that fails on every attempt is retried up to the
  bounded 3 total attempts and then surfaces an ``AudienceDataRetrievalError``
  identifying the channel (9.4);
- when both the owned-channel activity and the Channel_Category aggregate are
  unavailable, a ``NoDataError`` identifying the channel is returned and no
  recommendation is produced (9.6).

A couple of supporting happy-path / fallback examples are included to anchor the
edge behavior, but the focus is on 9.4 and 9.6.

Tests drive the genuine :class:`ResilientDataSource` over scripted stubs with a
:class:`FakeClock`, so retries/backoff are exercised instantly and without
mocking the resilience layer.

Requirements exercised: 9.4, 9.6.
"""

from __future__ import annotations

from infrastructure.clock import FakeClock
from infrastructure.datasource import (
    DataSource,
    NonTransientError,
    TransientError,
)
from domain.models import AudienceActivity, Confidence, HourlyActivity
from analysis.publish_time_predictor import (
    AudienceDataRetrievalError,
    NoDataError,
    PublishTimePredictor,
)
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _RaisingSource:
    """A :class:`DataSource` whose ``get_audience_activity`` always raises.

    Records every call in ``calls`` so a test can assert how many attempts the
    resilience layer made. The supplied ``error`` decides whether the failure is
    retried (transient) or recorded once (non-transient).
    """

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.calls = 0

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity:
        self.calls += 1
        raise self._error

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_videos(self, channel_id, published_within_days=None):  # pragma: no cover
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


class _ReturningSource:
    """A :class:`DataSource` returning a pre-canned audience-activity series."""

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


def _resilient(source: DataSource) -> ResilientDataSource:
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


def _usable_activity(channel_id: str | None = "chan") -> AudienceActivity:
    return AudienceActivity(
        channel_id=channel_id,
        days_covered=7,
        buckets=(
            HourlyActivity(day_of_week=2, hour=18, activity=5.0),
            HourlyActivity(day_of_week=2, hour=19, activity=9.0),
            HourlyActivity(day_of_week=2, hour=20, activity=4.0),
        ),
    )


# ---------------------------------------------------------------------------
# 9.4: retrieval exhausts 3 attempts -> audience-data-retrieval error
# ---------------------------------------------------------------------------


def test_transient_retrieval_exhausts_three_attempts_then_audience_data_retrieval_error():
    """Every attempt fails transiently -> 3 attempts then 9.4 error naming channel."""
    source = _RaisingSource(TransientError("audience activity temporarily unavailable"))

    result = PublishTimePredictor().predict(
        channel_id="chan-9-4",
        source=_resilient(source),
        tz=None,
        category_activity=None,
    )

    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, AudienceDataRetrievalError)
    assert error.channel_id == "chan-9-4"
    # The resilience layer made exactly the bounded 3 total attempts (9.4).
    assert source.calls == 3
    # The underlying reason is carried through.
    assert "temporarily unavailable" in error.reason


def test_non_transient_retrieval_failure_yields_audience_data_retrieval_error():
    """A non-transient retrieval failure also surfaces a 9.4 error (recorded once)."""
    source = _RaisingSource(NonTransientError("audience activity endpoint not found"))

    result = PublishTimePredictor().predict(
        channel_id="chan-9-4b",
        source=_resilient(source),
        tz=None,
        category_activity=None,
    )

    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, AudienceDataRetrievalError)
    assert error.channel_id == "chan-9-4b"
    # Non-transient failures are recorded once, without retry.
    assert source.calls == 1


# ---------------------------------------------------------------------------
# 9.6: both owned-channel and category activity unavailable -> no-data error
# ---------------------------------------------------------------------------


def test_both_unavailable_returns_no_data_error_and_no_recommendation():
    """Owned activity unavailable + no category aggregate -> NoDataError (9.6)."""
    # Retrieval succeeds but the series is unavailable (covers 0 days, no buckets).
    unavailable = AudienceActivity(channel_id="chan-9-6", days_covered=0, buckets=())
    source = _ReturningSource(unavailable)

    result = PublishTimePredictor().predict(
        channel_id="chan-9-6",
        source=_resilient(source),
        tz=None,
        category_activity=None,  # no aggregate fallback available
    )

    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, NoDataError)
    assert error.channel_id == "chan-9-6"


def test_owned_unavailable_but_category_present_short_history_is_no_data():
    """A category aggregate that itself covers < 7 days is not usable -> NoDataError (9.6)."""
    unavailable_owned = AudienceActivity(
        channel_id="chan-9-6c", days_covered=3, buckets=()
    )
    short_category = AudienceActivity(
        channel_id=None,
        days_covered=4,  # < 7 days -> not usable
        buckets=(HourlyActivity(day_of_week=0, hour=0, activity=1.0),),
    )
    source = _ReturningSource(unavailable_owned)

    result = PublishTimePredictor().predict(
        channel_id="chan-9-6c",
        source=_resilient(source),
        tz=None,
        category_activity=short_category,
    )

    assert result.is_err()
    assert isinstance(result.unwrap_err(), NoDataError)


# ---------------------------------------------------------------------------
# Supporting anchors: happy path (9.2/9.3) and category fallback (9.5)
# ---------------------------------------------------------------------------


def test_available_owned_activity_yields_normal_confidence_peak_window():
    """Available owned activity -> NORMAL-confidence rec at the peak window (9.2)."""
    source = _ReturningSource(_usable_activity("chan-ok"))

    result = PublishTimePredictor().predict(
        channel_id="chan-ok",
        source=_resilient(source),
        tz="America/New_York",
        category_activity=None,
    )

    assert result.is_ok()
    rec = result.unwrap()
    assert rec.confidence is Confidence.NORMAL
    assert rec.timezone == "America/New_York"
    assert rec.day_of_week == 2
    # The peak contiguous window is hours 18-20 (5+9+4=18 activity, the densest
    # run that fits the 1-3 hour bound), starting at 18 with a 3-hour duration.
    assert rec.window_start_hour == 18
    assert rec.window_duration_hours == 3


def test_unconfigured_timezone_defaults_to_utc():
    """No configured time zone -> the recommendation is expressed in UTC (9.3)."""
    source = _ReturningSource(_usable_activity("chan-utc"))

    result = PublishTimePredictor().predict(
        channel_id="chan-utc",
        source=_resilient(source),
        tz=None,
        category_activity=None,
    )

    assert result.is_ok()
    assert result.unwrap().timezone == "UTC"
