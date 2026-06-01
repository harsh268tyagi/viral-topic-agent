"""Example/unit tests for the keyword/template degradation branches (task 6.7).

Covers the two documented graceful-degradation branches of
:class:`~infrastructure.youtube_data_source.YouTubeDataSource` (implemented by
task 6.4) against injected fakes with **no real network access** (Requirements
16.3, 16.4):

- **4.3 — unconfigured keyword provider.** When no
  :class:`~infrastructure.keyword_metrics_provider.KeywordMetricsProvider` is
  configured, :meth:`YouTubeDataSource.get_keyword_metrics` returns an empty
  list, preserving degradation to ``insufficient-data``. The data source must
  **not** consult any provider and must **not** issue any network request.
- **5.3 — unconfigured template strategy.** When no
  :class:`~infrastructure.template_performance_strategy.TemplatePerformanceStrategy`
  is configured, :meth:`YouTubeDataSource.get_template_performance` returns an
  empty list. The data source must **not** consult any strategy and must
  **not** issue any network request.

The data source is wired with a :class:`~tests.edge_fakes.FakeHttpTransport`, a
real :class:`~infrastructure.auth_manager.AuthManager` over that transport, and
a :class:`~infrastructure.clock.FakeClock`, mirroring the construction used by
``tests/test_youtube_datasource_conformance.py`` and
``tests/test_youtube_audience_activity.py``. The ``FakeHttpTransport`` is
constructed with **no** scripted outcomes, so any attempt to issue a request
would raise ``AssertionError`` — a second, stronger guard that the unconfigured
branches never reach the network (the ``call_count == 0`` assertions document
the same guarantee explicitly).
"""

from __future__ import annotations

import pytest

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import (
    ChannelCategory,
    KeywordMetric,
    TemplatePerformance,
    VideoStats,
)
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

_API_KEY_VALUE = "data-api-key-AAA"


# ---------------------------------------------------------------------------
# Builders (mirroring tests/test_youtube_audience_activity.py conventions)
# ---------------------------------------------------------------------------


def _secret(value: str, name: str) -> Secret:
    return Secret(value, CredentialReference(name))


def _auth_settings() -> AuthSettings:
    return AuthSettings(youtube_api_key=_secret(_API_KEY_VALUE, "youtube_api_key"))


def _data_source(
    transport: FakeHttpTransport,
    *,
    keyword_provider=None,
    template_strategy=None,
) -> YouTubeDataSource:
    """Build a ``YouTubeDataSource`` over injected fakes with optional seams.

    Defaults leave both seams unconfigured (``None``) so the degradation
    branches are exercised; a test may inject a spy seam to assert it is *not*
    consulted by the unconfigured-sibling branch.
    """
    clock = FakeClock()
    auth = AuthManager(_auth_settings(), transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url="https://youtube.example.com/v3",
        request_timeout_seconds=30.0,
        keyword_provider=keyword_provider,
        template_strategy=template_strategy,
    )


class _SpyKeywordProvider:
    """A :class:`KeywordMetricsProvider` spy that must never be consulted here."""

    def __init__(self) -> None:
        self.calls: list[tuple[ChannelCategory, int]] = []

    def fetch(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]:
        self.calls.append((category, max_keywords))
        return [KeywordMetric(keyword="kw", demand=1.0, competition=0.5)]


class _SpyTemplateStrategy:
    """A :class:`TemplatePerformanceStrategy` spy that must never be consulted here."""

    def __init__(self) -> None:
        self.calls: list[tuple[ChannelCategory, list[VideoStats]]] = []

    def derive(
        self, category: ChannelCategory, videos: list[VideoStats]
    ) -> list[TemplatePerformance]:
        self.calls.append((category, list(videos)))
        return [
            TemplatePerformance(
                template_id="t1", category=category, observed_performance=2.0
            )
        ]


# ---------------------------------------------------------------------------
# 4.3 — unconfigured keyword provider returns [] without a network request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(ChannelCategory))
@pytest.mark.parametrize("max_keywords", [1, 10, 1000])
def test_unconfigured_keyword_provider_returns_empty(
    category: ChannelCategory, max_keywords: int
):
    """4.3: no keyword provider configured -> [], no provider consulted, no request."""
    transport = FakeHttpTransport()  # no scripted outcomes: any request -> AssertionError
    source = _data_source(transport, keyword_provider=None)

    result = source.get_keyword_metrics(category, max_keywords)

    assert result == []
    # Degradation must not reach the network (4.3): no request is issued.
    assert transport.call_count == 0


def test_unconfigured_keyword_provider_does_not_consult_template_strategy():
    """4.3: the keyword degradation branch consults neither seam nor the network.

    A spy template strategy is injected to prove that returning ``[]`` for an
    unconfigured keyword provider never touches the *other* configured seam (or
    the transport).
    """
    transport = FakeHttpTransport()
    template_spy = _SpyTemplateStrategy()
    source = _data_source(
        transport, keyword_provider=None, template_strategy=template_spy
    )

    assert source.get_keyword_metrics(ChannelCategory.GAMING, 25) == []
    assert template_spy.calls == []
    assert transport.call_count == 0


# ---------------------------------------------------------------------------
# 5.3 — unconfigured template strategy returns [] without a network request
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(ChannelCategory))
def test_unconfigured_template_strategy_returns_empty(category: ChannelCategory):
    """5.3: no template strategy configured -> [], no strategy consulted, no request."""
    transport = FakeHttpTransport()  # no scripted outcomes: any request -> AssertionError
    source = _data_source(transport, template_strategy=None)

    result = source.get_template_performance(category)

    assert result == []
    # The unconfigured branch returns before retrieving any videos (5.3): the
    # data source issues no request.
    assert transport.call_count == 0


def test_unconfigured_template_strategy_does_not_consult_keyword_provider():
    """5.3: the template degradation branch consults neither seam nor the network.

    A spy keyword provider is injected to prove that returning ``[]`` for an
    unconfigured template strategy never touches the *other* configured seam (or
    the transport, since no video retrieval is performed).
    """
    transport = FakeHttpTransport()
    keyword_spy = _SpyKeywordProvider()
    source = _data_source(
        transport, keyword_provider=keyword_spy, template_strategy=None
    )

    assert source.get_template_performance(ChannelCategory.MUSIC) == []
    assert keyword_spy.calls == []
    assert transport.call_count == 0
