"""Unit tests for the ``DataSource`` abstraction and failure model (task 2.1).

These cover the infrastructure types defined in ``datasource.py``:

- the :class:`DataSource` ``Protocol`` (structural conformance);
- the :class:`DataSourceError` hierarchy and classification semantics of each
  subtype (rate-limit / transient / non-transient / timeout);
- :class:`DataRequest` reification and dispatch via :meth:`DataRequest.invoke`;
- :class:`DataSourceFailure` carrying ``target``, ``reason``, and
  ``classification`` (16.6), with value-based equality.

The end-to-end resilience behavior (retry/backoff/pause) is covered by
``test_resilient_data_source.py``; here we exercise the building blocks in
isolation.
"""

from __future__ import annotations

import pytest

from viral_topic_agent.infrastructure.datasource import (
    DataOperation,
    DataRequest,
    DataSource,
    DataSourceError,
    DataSourceFailure,
    FailureClassification,
    NonTransientError,
    RateLimitError,
    TimeoutError,
    TransientError,
)
from viral_topic_agent.domain.models import (
    AudienceActivity,
    ChannelCategory,
    ChannelMetadata,
    KeywordMetric,
    TemplatePerformance,
    VideoStats,
)


# ---------------------------------------------------------------------------
# A minimal in-memory DataSource used for protocol / dispatch tests.
# ---------------------------------------------------------------------------


class RecordingSource:
    """A structurally-conformant :class:`DataSource` that records its calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        self.calls.append(("get_channel_metadata", {"channel_id": channel_id}))
        return ChannelMetadata(
            channel_id=channel_id,
            title="Example",
            subscriber_count=10,
            video_count=5,
        )

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        self.calls.append(
            (
                "get_videos",
                {"channel_id": channel_id, "published_within_days": published_within_days},
            )
        )
        return [VideoStats(video_id="v1", view_count=100, published_at="2024-01-01")]

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity:
        self.calls.append(("get_audience_activity", {"channel_id": channel_id, "days": days}))
        return AudienceActivity(channel_id=channel_id, days_covered=days, buckets=())

    def get_keyword_metrics(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]:
        self.calls.append(
            ("get_keyword_metrics", {"category": category, "max_keywords": max_keywords})
        )
        return [KeywordMetric(keyword="kw", demand=1.0, competition=0.5)]

    def get_template_performance(
        self, category: ChannelCategory
    ) -> list[TemplatePerformance]:
        self.calls.append(("get_template_performance", {"category": category}))
        return [TemplatePerformance(template_id="t1", category=category, observed_performance=2.0)]


# ---------------------------------------------------------------------------
# DataSource protocol conformance
# ---------------------------------------------------------------------------


def test_recording_source_satisfies_datasource_protocol():
    assert isinstance(RecordingSource(), DataSource)


def test_incomplete_source_does_not_satisfy_protocol():
    class Partial:
        def get_channel_metadata(self, channel_id):  # missing the other methods
            return None

    assert not isinstance(Partial(), DataSource)


# ---------------------------------------------------------------------------
# Error hierarchy & classification semantics
# ---------------------------------------------------------------------------


def test_all_errors_share_the_datasource_error_base():
    for exc in (RateLimitError(), TransientError(), NonTransientError(), TimeoutError()):
        assert isinstance(exc, DataSourceError)


def test_timeout_error_is_a_transient_error():
    # A no-complete-response timeout must be retried like any transient (16.4).
    assert isinstance(TimeoutError(), TransientError)


def test_non_transient_is_not_transient():
    assert not isinstance(NonTransientError(), TransientError)
    assert not isinstance(NonTransientError(), RateLimitError)


def test_error_reason_defaults_to_class_name_when_blank():
    assert DataSourceError().reason == "DataSourceError"
    assert TransientError().reason == "TransientError"


def test_error_reason_preserved_when_provided():
    assert NonTransientError("authentication rejected").reason == "authentication rejected"


def test_rate_limit_error_carries_optional_retry_after():
    assert RateLimitError().retry_after_seconds is None
    assert RateLimitError(retry_after_seconds=42.0).retry_after_seconds == 42.0


def test_rate_limit_error_default_reason():
    assert RateLimitError().reason == "rate limit exceeded"


# ---------------------------------------------------------------------------
# DataRequest reification & dispatch
# ---------------------------------------------------------------------------


def test_data_request_is_frozen():
    req = DataRequest(operation=DataOperation.CHANNEL_METADATA, target="chan-1")
    with pytest.raises(Exception):
        req.target = "chan-2"  # type: ignore[misc]


def test_data_request_default_params_is_empty_mapping():
    req = DataRequest(operation=DataOperation.VIDEOS, target="chan-1")
    assert dict(req.params) == {}


@pytest.mark.parametrize(
    "operation, params, expected_method",
    [
        (DataOperation.CHANNEL_METADATA, {"channel_id": "c"}, "get_channel_metadata"),
        (DataOperation.VIDEOS, {"channel_id": "c", "published_within_days": 30}, "get_videos"),
        (DataOperation.AUDIENCE_ACTIVITY, {"channel_id": "c", "days": 7}, "get_audience_activity"),
        (
            DataOperation.KEYWORD_METRICS,
            {"category": ChannelCategory.GAMING, "max_keywords": 100},
            "get_keyword_metrics",
        ),
        (
            DataOperation.TEMPLATE_PERFORMANCE,
            {"category": ChannelCategory.MUSIC},
            "get_template_performance",
        ),
    ],
)
def test_data_request_invoke_dispatches_to_the_right_method(operation, params, expected_method):
    source = RecordingSource()
    req = DataRequest(operation=operation, target="t", params=params)

    result = req.invoke(source)

    assert result is not None
    assert source.calls == [(expected_method, params)]


def test_data_request_invoke_propagates_data_source_error():
    class FailingSource(RecordingSource):
        def get_channel_metadata(self, channel_id):
            raise NonTransientError("target not found")

    req = DataRequest(
        operation=DataOperation.CHANNEL_METADATA,
        target="missing",
        params={"channel_id": "missing"},
    )
    with pytest.raises(NonTransientError):
        req.invoke(FailingSource())


def test_data_operation_values_match_protocol_method_names():
    method_names = {
        "get_channel_metadata",
        "get_videos",
        "get_audience_activity",
        "get_keyword_metrics",
        "get_template_performance",
    }
    assert {op.value for op in DataOperation} == method_names


# ---------------------------------------------------------------------------
# DataSourceFailure model (16.6)
# ---------------------------------------------------------------------------


def test_failure_carries_target_reason_and_classification():
    failure = DataSourceFailure(
        target="chan-9",
        reason="authentication rejected",
        classification=FailureClassification.NON_TRANSIENT,
    )
    assert failure.target == "chan-9"
    assert failure.reason == "authentication rejected"
    assert failure.classification is FailureClassification.NON_TRANSIENT
    assert failure.attempts == 1  # default


def test_failure_has_value_equality():
    a = DataSourceFailure("t", "r", FailureClassification.TRANSIENT, attempts=3)
    b = DataSourceFailure("t", "r", FailureClassification.TRANSIENT, attempts=3)
    assert a == b
    assert a != DataSourceFailure("t", "r", FailureClassification.TRANSIENT, attempts=2)


def test_failure_is_frozen():
    failure = DataSourceFailure("t", "r", FailureClassification.RATE_LIMITED)
    with pytest.raises(Exception):
        failure.reason = "changed"  # type: ignore[misc]


def test_failure_classification_has_all_documented_members():
    assert {c.value for c in FailureClassification} == {
        "rate-limited",
        "transient",
        "non-transient",
        "rate-limit-timeout",
    }
