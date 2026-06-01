"""Hypothesis property test for YouTube keyword-metrics retrieval (task 6.5).

This module validates a single universal property of
``YouTubeDataSource.get_keyword_metrics`` (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 9):

- Property 9 (Requirements 4.1, 4.2): for any list of keyword metrics a
  configured ``KeywordMetricsProvider`` returns and any requested
  ``max_keywords``, ``get_keyword_metrics`` returns the one-to-one mapping
  capped at the requested maximum -- the result is the provider's list truncated
  to ``min(len(provided), max_keywords)`` (for a positive maximum) or empty (for
  a non-positive maximum), with the surviving elements preserved in order and
  identity (4.1, 4.2).

The data source is driven through an injected :class:`FakeKeywordMetricsProvider`
(no real network access, 16.3): ``get_keyword_metrics`` delegates to the
provider and applies the cap, so no HTTP request is performed. The
:class:`AuthManager` is a real one built over a :class:`FakeHttpTransport` +
:class:`FakeClock` and an :class:`AuthSettings`; it is never consulted on this
path but is required by the constructor.

The generator produces an arbitrary list of distinct ``KeywordMetric`` values
and an arbitrary ``max_keywords`` (including ``0`` and negatives), and the test
asserts only the universal cap-and-mapping invariant the property states; the
unconfigured-degradation branch (4.3) and the classified-failure branch (4.4)
are this property's example-test companions (tasks 6.7 and within 6.4), not this
property.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import ChannelCategory, KeywordMetric
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport, FakeKeywordMetricsProvider

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 30.0

# Any supported channel category; the cap-and-mapping behaviour is independent
# of which one is requested.
_categories = st.sampled_from(list(ChannelCategory))
# A requested maximum spanning negatives, zero, and well past any list length so
# both the "cap binds" and "cap does not bind" cases are exercised.
_max_keywords = st.integers(min_value=-5, max_value=60)
# Non-negative demand/competition values (the metrics are never negative).
_metric_values = st.floats(
    min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False
)


@st.composite
def _keyword_metrics(draw: st.DrawFn) -> list[KeywordMetric]:
    """Draw a list of distinct ``KeywordMetric`` values the provider returns.

    Keywords are unique so a positional assertion is unambiguous; demand and
    competition are arbitrary non-negative floats. The list length spans empty
    through well past a typical ``max_keywords`` so both the capped and uncapped
    cases occur.
    """
    keywords = draw(
        st.lists(
            st.text(st.characters(codec="utf-8"), min_size=1, max_size=30),
            min_size=0,
            max_size=40,
            unique=True,
        )
    )
    metrics: list[KeywordMetric] = []
    for keyword in keywords:
        metrics.append(
            KeywordMetric(
                keyword=keyword,
                demand=draw(_metric_values),
                competition=draw(_metric_values),
            )
        )
    return metrics


def _build_data_source(
    provider: FakeKeywordMetricsProvider,
) -> YouTubeDataSource:
    """Build a YouTubeDataSource with the given keyword provider configured.

    The transport is never touched on the keyword-metrics path (delegation is to
    the provider), but a real :class:`AuthManager` over a fake transport/clock is
    supplied because the constructor requires one.
    """
    transport = FakeHttpTransport()
    clock = FakeClock()
    auth_settings = AuthSettings(
        youtube_api_key=Secret(
            "api-key-value", CredentialReference("youtube_api_key")
        ),
        oauth=None,
    )
    auth = AuthManager(auth_settings, transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url=API_BASE_URL,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        keyword_provider=provider,
    )


# Feature: real-provider-integration, Property 9: Keyword metrics map one-to-one within the requested maximum
# Validates: Requirements 4.1, 4.2
@settings(max_examples=200)
@given(
    metrics=_keyword_metrics(),
    category=_categories,
    max_keywords=_max_keywords,
)
def test_keyword_metrics_map_one_to_one_within_the_requested_maximum(
    metrics, category, max_keywords
):
    """For any provider-returned keyword metrics and any ``max_keywords``,
    ``get_keyword_metrics`` returns the one-to-one mapping capped at the
    requested maximum: ``min(len(provided), max_keywords)`` elements (or none
    when the maximum is non-positive), preserved in order and identity
    (Requirements 4.1, 4.2)."""
    provider = FakeKeywordMetricsProvider(metrics=metrics)
    data_source = _build_data_source(provider)

    result = data_source.get_keyword_metrics(category, max_keywords)

    # The expected one-to-one mapping capped at the requested maximum (4.1, 4.2):
    # a non-positive maximum requests no keyword, otherwise the provider's list
    # truncated to max_keywords with order preserved.
    expected = metrics[:max_keywords] if max_keywords > 0 else []

    # Length is exactly min(provided, max_keywords) for a positive maximum, else 0 (4.2).
    assert len(result) == len(expected)
    # Elements are the same KeywordMetric values, in the same order (4.1).
    assert result == expected
    # No element is fabricated or dropped out of order: each survivor is the
    # provider's element at the same position.
    for i, metric in enumerate(result):
        assert metric is metrics[i]

    # The cap never exceeds the requested maximum (4.2).
    if max_keywords > 0:
        assert len(result) <= max_keywords
    else:
        assert result == []
