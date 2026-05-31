"""Integration tests for latency budgets and external wiring (task 20.1).

Each test wires a *real* component to a fast, deterministic stub
:class:`~tests.integration_support.StubDataSource` /
:class:`~viral_topic_agent.generation.InMemoryGenerationProvider` through the
genuine :class:`~viral_topic_agent.resilient_data_source.ResilientDataSource`
(over a :class:`~viral_topic_agent.clock.RealClock`) and measures the real
wall-clock time the operation consumes with :func:`time.perf_counter`, asserting
it sits comfortably within the requirement's budget.

These tests verify the *components themselves* are fast against fast
dependencies. The separate resilience-timeout behaviour (the 30 s request
timeout, retry backoff, etc.) is covered by the ResilientDataSource property and
unit tests; here the stub always returns immediately, so the elapsed time is the
component's own work.

Budgets verified:

- 1.1  authorization request issued within 5 s of initiation
- 1.4  owned-channel data retrieval within 30 s with valid credentials
- 3.8  trend discovery responds within 10 s
- 11.1 SEO retrieval of up to 1,000 candidate keywords within 10 s
- 10.1 script generation produces all artifacts within 60 s

Requirements: 1.1, 1.4, 3.8, 10.1, 11.1.
"""

from __future__ import annotations

import time

from viral_topic_agent.analysis.channel_analyzer import ChannelAnalyzer
from viral_topic_agent.infrastructure.clock import RealClock
from viral_topic_agent.connection.connection_manager import (
    ConnectionManager,
    InMemoryCredentialStore,
)
from viral_topic_agent.infrastructure.datasource import DataOperation, DataRequest
from viral_topic_agent.generation import InMemoryGenerationProvider
from viral_topic_agent.domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy
from viral_topic_agent.generation.script_generator import ScriptGenerator
from viral_topic_agent.analysis.seo_analyzer import MAX_CANDIDATE_KEYWORDS, SEOAnalyzer
from viral_topic_agent.analysis.trend_discovery import TrendDiscoveryEngine

from .integration_support import StubDataSource

# Comfort factor: each test asserts the operation finishes in well under the
# requirement's budget. We use a generous fraction of the budget so the test is
# robust on a slow/loaded CI machine while still proving the component is not
# anywhere near the limit against fast dependencies.
_COMFORT_FRACTION = 0.5


def _resilient(source: StubDataSource) -> ResilientDataSource:
    """A real ResilientDataSource over a RealClock and the fast stub source.

    Using the real resilience layer (not a mock) means these tests exercise the
    genuine call path; the stub returns instantly so no retry/backoff/timeout
    behaviour is triggered and the elapsed time is the component's own work.
    """
    return ResilientDataSource(source, RetryPolicy(), RealClock())


def _make_idea() -> ContentIdea:
    template = ViralTemplate(
        template_id="tpl-1",
        name="Tier List Ranking",
        category=ChannelCategory.GAMING,
        observed_performance=5000.0,
    )
    return ContentIdea(
        idea_id="idea-1",
        title_concept="Top 10 Gaming Moments",
        rationale="Derived from the weekly window with an observed metric of 5000.",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(template,),
        observed_metric_value=5000.0,
    )


# ---------------------------------------------------------------------------
# 1.1: authorization request issued within 5 s of initiation
# ---------------------------------------------------------------------------


def test_authorization_request_issued_within_5s():
    """initiate_connection issues the auth request well within 5 s (1.1)."""
    manager = ConnectionManager(InMemoryCredentialStore())

    start = time.perf_counter()
    result = manager.initiate_connection("chan-1", RealClock())
    elapsed = time.perf_counter() - start

    assert result.channel_id == "chan-1"
    assert elapsed < 5.0 * _COMFORT_FRACTION


# ---------------------------------------------------------------------------
# 1.4: owned-channel data retrieval within 30 s with valid credentials
# ---------------------------------------------------------------------------


def test_data_retrieval_with_valid_credentials_within_30s():
    """retrieve_with_credentials returns within 30 s for a connected channel (1.4)."""
    manager = ConnectionManager(InMemoryCredentialStore())
    source = _resilient(StubDataSource())
    channel = AuthorizedChannel(
        channel_id="chan-1",
        credentials_ref="cred-ref",
        connected=True,
        credentials_expired=False,
    )
    request = DataRequest(
        operation=DataOperation.CHANNEL_METADATA,
        target="chan-1",
        params={"channel_id": "chan-1"},
    )

    start = time.perf_counter()
    result = manager.retrieve_with_credentials(channel, request, source, RealClock())
    elapsed = time.perf_counter() - start

    assert result.is_ok()
    assert result.unwrap().channel_id == "chan-1"
    assert elapsed < 30.0 * _COMFORT_FRACTION


# ---------------------------------------------------------------------------
# 3.8: trend discovery responds within 10 s
# ---------------------------------------------------------------------------


def test_trend_discovery_responds_within_10s():
    """discover() across all windows returns within 10 s (3.8)."""
    engine = TrendDiscoveryEngine()
    source = _resilient(StubDataSource())

    start = time.perf_counter()
    result = engine.discover(source)
    elapsed = time.perf_counter() - start

    # Sanity: the stub yields ideas in every window (not an empty/error result).
    assert any(result.ideas_by_window.values())
    assert result.window_errors == {}
    assert elapsed < 10.0 * _COMFORT_FRACTION


# ---------------------------------------------------------------------------
# 11.1: SEO retrieval of up to 1,000 candidate keywords within 10 s
# ---------------------------------------------------------------------------


def test_seo_analysis_of_1000_keywords_within_10s():
    """analyze() over the full 1,000-keyword budget returns within 10 s (11.1)."""
    analyzer = SEOAnalyzer()
    # The stub supplies the full candidate budget so the analyzer classifies the
    # maximum it is required to handle (11.1).
    source = _resilient(StubDataSource(keyword_count=MAX_CANDIDATE_KEYWORDS))

    start = time.perf_counter()
    result = analyzer.analyze(ChannelCategory.GAMING, source)
    elapsed = time.perf_counter() - start

    assert result.error is None
    assert elapsed < 10.0 * _COMFORT_FRACTION


# ---------------------------------------------------------------------------
# 10.1: script generation produces all artifacts within 60 s
# ---------------------------------------------------------------------------


def test_script_generation_produces_all_artifacts_within_60s():
    """generate() produces outline, script, SEO tags, and description in <60 s (10.1)."""
    generator = ScriptGenerator()
    provider = InMemoryGenerationProvider()
    idea = _make_idea()
    seo_keywords = [f"kw-{i}" for i in range(10)]

    start = time.perf_counter()
    result = generator.generate(idea, seo_keywords, provider)
    elapsed = time.perf_counter() - start

    assert result.is_ok()
    bundle = result.unwrap()
    # All four artifacts were produced.
    assert bundle.outline
    assert bundle.script
    assert bundle.seo_tags
    assert bundle.description
    assert elapsed < 60.0 * _COMFORT_FRACTION
