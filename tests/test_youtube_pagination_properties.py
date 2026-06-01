"""Hypothesis property test for YouTube video pagination (task 5.6).

This module validates a single universal property of
``YouTubeDataSource.get_videos`` pagination (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 4):

- Property 4 (Requirement 1.8): for any multi-page retrieval and any maximum
  item count ``M``, ``YouTubeDataSource`` requests successive pages until the
  API reports no further page **or** the accumulated item count reaches ``M``,
  whichever occurs first, and returns no more than ``M`` items.

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3): each generated ``search.list`` page is queued as a
scripted response. Every page except the last carries a ``nextPageToken`` so
the source chains to the next page; the last page omits it so the "no further
page" condition can fire. ``published_within_days`` is left unset so the
recency filter (Property 3) does not interfere with the pagination guarantee
being tested here (1.5).

Expected values are derived independently of the implementation by an explicit
re-simulation of the documented stopping rule: accumulate each page's items
(capped at ``M``); stop as soon as the accumulated count reaches ``M`` or a
page with no further token is consumed. The test asserts the returned item
count, the number of transport requests performed, and the page-token threading
all match that independent expectation, and that no more than ``M`` items are
returned.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import VideoStats
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 30.0


def _page_token(index: int) -> str:
    """A deterministic, url-safe ``nextPageToken`` for page ``index``."""
    return f"token{index}"


@st.composite
def _pagination_case(draw: st.DrawFn) -> dict:
    """Draw a multi-page retrieval plus its independently-derived expectation.

    Returns the maximum item count ``M`` (``max_items``), the ordered list of
    per-page item counts, the encoded JSON ``bodies`` the YouTube Data API would
    return for each page (every page but the last carrying a ``nextPageToken``),
    the per-page tokens, and the expected returned item count / request count /
    threaded tokens computed by re-simulating the documented stopping rule.
    """
    max_items = draw(st.integers(min_value=1, max_value=30))
    # 1..6 pages, each with 0..10 items, so the retrieval spans multiple pages.
    page_sizes = draw(st.lists(st.integers(min_value=0, max_value=10),
                               min_size=1, max_size=6))
    num_pages = len(page_sizes)

    bodies: list[bytes] = []
    tokens: list[str] = []
    for i, count in enumerate(page_sizes):
        items = [
            {"id": {"videoId": f"vid-{i}-{j}"}, "snippet": {"publishedAt": ""}}
            for j in range(count)
        ]
        payload: dict = {"kind": "youtube#searchListResponse", "items": items}
        if i < num_pages - 1:
            # Every page but the last advertises a further page (1.8).
            token = _page_token(i)
            tokens.append(token)
            payload["nextPageToken"] = token
        else:
            tokens.append("")  # last page: no further token
        bodies.append(json.dumps(payload).encode("utf-8"))

    # Independent re-simulation of the documented stopping rule (1.8):
    # accumulate per-page items (capped at M); stop at the first of
    # reaching M or consuming a page with no further token.
    collected = 0
    expected_requests = 0
    threaded_tokens: list[str] = []
    for i, count in enumerate(page_sizes):
        expected_requests += 1
        # The (i)-th request (for i >= 1) threads the previous page's token.
        if i > 0:
            threaded_tokens.append(tokens[i - 1])
        collected = min(collected + count, max_items)
        if collected >= max_items:
            break
        if i == num_pages - 1:  # last page has no further token
            break

    return {
        "max_items": max_items,
        "bodies": bodies,
        "expected_count": collected,
        "expected_requests": expected_requests,
        "threaded_tokens": threaded_tokens,
    }


def _build_data_source(
    transport: FakeHttpTransport, *, max_items: int
) -> YouTubeDataSource:
    """Build a ``YouTubeDataSource`` over the transport with the given ``max_items``.

    The ``AuthManager`` carries a ``Secret`` API key; ``get_videos`` only
    consumes ``auth.data_api_params()``, so no OAuth or network is needed.
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
        max_items=max_items,
    )


# Feature: real-provider-integration, Property 4: Pagination stops at the first of no-further-page or the maximum item count
# Validates: Requirements 1.8
@settings(max_examples=200)
@given(case=_pagination_case())
def test_pagination_stops_at_first_of_no_page_or_max_items(case):
    """For any multi-page retrieval and any maximum item count ``M``,
    ``get_videos`` requests successive pages until the API reports no further
    page or the accumulated item count reaches ``M``, whichever occurs first,
    and returns no more than ``M`` items (1.8)."""
    transport = FakeHttpTransport()
    for body in case["bodies"]:
        transport.queue_response(HttpResponse(status=200, body=body))
    data_source = _build_data_source(transport, max_items=case["max_items"])

    result = data_source.get_videos("UC_channel")

    # The result is a list of VideoStats.
    assert isinstance(result, list)
    assert all(isinstance(v, VideoStats) for v in result)

    # Never more than the requested maximum item count M (1.8).
    assert len(result) <= case["max_items"]

    # Pagination stops at the first of no-further-page or reaching M: the
    # returned count and the number of transport requests match the independent
    # re-simulation of the stopping rule exactly (1.8).
    assert len(result) == case["expected_count"]
    assert transport.call_count == case["expected_requests"]

    # Successive pages are requested by threading the previous page's
    # nextPageToken; the first request carries none (1.8).
    assert "pageToken=" not in transport.requests[0].url
    for request, token in zip(transport.requests[1:], case["threaded_tokens"]):
        assert f"pageToken={token}" in request.url
