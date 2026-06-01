"""Hypothesis property test for YouTube template-performance derivation (task 6.6).

This module validates a single universal property of
``YouTubeDataSource.get_template_performance`` (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 10):

- Property 10 (Requirements 5.1, 5.2): for any configured
  ``TemplatePerformanceStrategy`` applied to retrieved video statistics,
  ``get_template_performance`` returns one ``TemplatePerformance`` per derived
  template (a one-to-one mapping of the strategy's output, 5.1), each populating
  the template identifier, the channel category, the observed performance, the
  sample size, the Short-format average view count, and the long-form-format
  average view count (5.2).

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3). ``get_template_performance`` first retrieves the
category's video statistics via ``get_videos`` (one ``search.list`` request),
then applies the configured strategy to them; the transport scripts that single
video-list response. The :class:`AuthManager` is a real one built over a
:class:`FakeHttpTransport` + :class:`FakeClock` and an :class:`AuthSettings`
carrying a :class:`Secret` API key, since the retrieval only consumes
``auth.data_api_params()`` (no OAuth, no network).

The strategy under injection is :class:`_FakeTemplatePerformanceStrategy`, a
deterministic fake that derives one fully-populated ``TemplatePerformance`` per
generated spec (tagging each with the ``category`` it is handed) and records the
``category``/``videos`` it was applied to. Because the data source returns the
strategy's output verbatim, the expected mapping is recomputed independently and
asserted equal element-for-element (one-to-one, order preserved), and every
field of every returned value is asserted populated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import ChannelCategory, TemplatePerformance, VideoStats
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 30.0

# A derived template's identifier is always populated (a non-empty handle).
_template_ids = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)
# Observed performance and per-format averages are finite, non-negative values.
_finite_values = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)
# A derived template rests on at least one sample video.
_sample_sizes = st.integers(min_value=1, max_value=10**6)
# Categories the strategy may be invoked for.
_categories = st.sampled_from(list(ChannelCategory))


@st.composite
def _template_spec(draw: st.DrawFn) -> dict:
    """Draw one fully-populated derived-template spec (category supplied later).

    Every field the property enumerates is populated with a definite value: a
    non-empty ``template_id``, a finite ``observed_performance``, a
    ``sample_size`` of at least one, and non-``None`` Short/long-form average
    view counts. The :class:`_FakeTemplatePerformanceStrategy` tags each spec
    with the ``category`` it is handed when deriving.
    """
    return {
        "template_id": draw(_template_ids),
        "observed_performance": draw(_finite_values),
        "sample_size": draw(_sample_sizes),
        "short_form_avg_views": draw(_finite_values),
        "long_form_avg_views": draw(_finite_values),
    }


@st.composite
def _video_item(draw: st.DrawFn) -> dict:
    """Draw one ``search.list`` video item the data source parses into VideoStats.

    The search result nests the id under ``id.videoId`` and the published
    timestamp under ``snippet.publishedAt``; the view count is reported as a
    numeric string. These shape the retrieved video statistics the strategy is
    applied to.
    """
    video_id = draw(st.text(st.characters(codec="utf-8"), max_size=20))
    published_at = draw(
        st.dates().map(lambda d: f"{d.isoformat()}T00:00:00Z")
    )
    view_count = draw(st.integers(min_value=0, max_value=10**12))
    return {
        "id": {"videoId": video_id},
        "snippet": {"publishedAt": published_at},
        "statistics": {"viewCount": str(view_count)},
    }


@st.composite
def _template_case(draw: st.DrawFn) -> dict:
    """Draw one template-performance case: a category, derived specs, and videos.

    Returns the requested ``category``, the list of derived-template ``specs``
    the strategy will produce, the number of retrieved videos, and the encoded
    JSON ``body`` of the single ``search.list`` response the data source reads
    before applying the strategy. The video list is kept under the data source's
    default ``max_items`` (50) so the retrieval is a single, unpaginated request.
    """
    category = draw(_categories)
    specs = draw(st.lists(_template_spec(), max_size=8))
    items = draw(st.lists(_video_item(), max_size=20))
    body = json.dumps(
        {"kind": "youtube#searchListResponse", "items": items}
    ).encode("utf-8")
    return {
        "category": category,
        "specs": specs,
        "video_count": len(items),
        "body": body,
    }


@dataclass
class _FakeTemplatePerformanceStrategy:
    """A deterministic :class:`TemplatePerformanceStrategy` fake for the property.

    Structurally satisfies the
    :class:`~infrastructure.template_performance_strategy.TemplatePerformanceStrategy`
    protocol. ``derive`` records the ``category`` and ``videos`` it is applied to
    (so the test can confirm the strategy was applied to the retrieved video
    statistics) and returns one fully-populated :class:`TemplatePerformance` per
    configured spec, tagging each with the ``category`` it is handed.
    """

    specs: list[dict]
    received: list[tuple[ChannelCategory, list[VideoStats]]] = field(
        default_factory=list
    )

    def derive(
        self, category: ChannelCategory, videos: list[VideoStats]
    ) -> list[TemplatePerformance]:
        self.received.append((category, list(videos)))
        return [
            TemplatePerformance(
                template_id=spec["template_id"],
                category=category,
                observed_performance=spec["observed_performance"],
                sample_size=spec["sample_size"],
                short_form_avg_views=spec["short_form_avg_views"],
                long_form_avg_views=spec["long_form_avg_views"],
            )
            for spec in self.specs
        ]

    @property
    def call_count(self) -> int:
        """How many times :meth:`derive` has been invoked."""
        return len(self.received)


def _build_data_source(
    transport: FakeHttpTransport, strategy: _FakeTemplatePerformanceStrategy
) -> YouTubeDataSource:
    """Build a YouTubeDataSource over the transport with the strategy configured.

    The ``AuthManager`` carries a ``Secret`` API key; the video retrieval that
    feeds the strategy only consumes ``auth.data_api_params()``, so no OAuth or
    network access is involved.
    """
    clock = FakeClock()
    auth_settings = AuthSettings(
        youtube_api_key=Secret("api-key-value", CredentialReference("youtube_api_key"))
    )
    auth = AuthManager(auth_settings, transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url=API_BASE_URL,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        template_strategy=strategy,
    )


# Feature: real-provider-integration, Property 10: Template performance maps one-to-one with all fields populated
# Validates: Requirements 5.1, 5.2
@settings(max_examples=100)
@given(case=_template_case())
def test_template_performance_maps_one_to_one_with_all_fields_populated(case):
    """For any configured ``TemplatePerformanceStrategy`` applied to retrieved
    video statistics, ``get_template_performance`` returns one
    ``TemplatePerformance`` per derived template (one-to-one, 5.1), each with the
    template identifier, channel category, observed performance, sample size, and
    Short/long-form average view counts populated (5.2).

    **Validates: Requirements 5.1, 5.2**
    """
    transport = FakeHttpTransport()
    transport.queue_response(HttpResponse(status=200, body=case["body"]))
    strategy = _FakeTemplatePerformanceStrategy(specs=case["specs"])
    data_source = _build_data_source(transport, strategy)

    result = data_source.get_template_performance(case["category"])

    # The result is the one-to-one mapping of the strategy's derived templates:
    # exactly one TemplatePerformance per derived template, order preserved (5.1).
    expected = [
        TemplatePerformance(
            template_id=spec["template_id"],
            category=case["category"],
            observed_performance=spec["observed_performance"],
            sample_size=spec["sample_size"],
            short_form_avg_views=spec["short_form_avg_views"],
            long_form_avg_views=spec["long_form_avg_views"],
        )
        for spec in case["specs"]
    ]
    assert result == expected
    assert len(result) == len(case["specs"])

    # Every TemplatePerformance field is populated (5.2).
    for tp in result:
        assert isinstance(tp, TemplatePerformance)
        assert tp.template_id  # non-empty template identifier
        assert isinstance(tp.category, ChannelCategory)
        assert tp.category == case["category"]
        assert isinstance(tp.observed_performance, float)
        assert isinstance(tp.sample_size, int)
        assert tp.sample_size >= 1
        assert tp.short_form_avg_views is not None
        assert tp.long_form_avg_views is not None

    # The strategy was applied exactly once to the retrieved video statistics,
    # for the requested category (5.1).
    assert strategy.call_count == 1
    received_category, received_videos = strategy.received[0]
    assert received_category == case["category"]
    assert len(received_videos) == case["video_count"]
    assert all(isinstance(v, VideoStats) for v in received_videos)

    # The source performs exactly one request (the video retrieval) and never
    # retries internally (16.5).
    assert transport.call_count == 1
