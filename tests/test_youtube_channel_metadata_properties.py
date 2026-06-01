"""Hypothesis property test for YouTube channel metadata retrieval (task 5.2).

This module validates a single universal property of
``YouTubeDataSource.get_channel_metadata`` (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 1):

- Property 1 (Requirement 1.2): for any successful YouTube Data API channel
  response, ``get_channel_metadata`` returns exactly one ``ChannelMetadata``
  whose ``channel_id`` equals the requested identifier and whose ``title``,
  ``subscriber_count``, and ``video_count`` equal the corresponding retrieved
  values.

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3): a generated channels-list JSON payload is queued as
the single scripted response. The :class:`AuthManager` is a real one built over
a :class:`FakeHttpTransport` + :class:`FakeClock` and an :class:`AuthSettings`
carrying a :class:`Secret` API key, since ``get_channel_metadata`` only calls
``auth.data_api_params()`` (no OAuth, no network).

Expected values are derived independently of the implementation: the test
generates the source title and the (non-negative integer) subscriber/video
counts, serializes the counts to numeric strings in the payload exactly as the
real API reports them, and asserts the returned ``ChannelMetadata`` mirrors the
generated source values.

The ``detected_category`` mapping (1.6, 1.7) is Property 2 (task 5.3) and is not
asserted here; this property is scoped to the data-mirroring guarantee (1.2).
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import ChannelMetadata
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
REQUEST_TIMEOUT_SECONDS = 30.0

# utf-8-encodable text only, so the generated payload always serializes cleanly.
_text = st.text(st.characters(codec="utf-8"), max_size=100)
# A channel id is any non-empty identifier; it is url-encoded into the request
# and echoed back onto the returned ChannelMetadata regardless of the payload.
_channel_ids = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)
# YouTube reports statistics counts as numeric strings of a non-negative count.
_counts = st.integers(min_value=0, max_value=10**18)
# Optional topicDetails.topicCategories (Wikipedia-style URLs). Included to make
# the payload realistic; detected_category itself is Property 2's concern.
_topic_categories = st.lists(_text, max_size=4)


@st.composite
def _channel_case(draw: st.DrawFn) -> dict:
    """Draw one successful channels-list response plus its expected mirror.

    Returns the requested ``channel_id``, the independently-derived expected
    ``title``/``subscriber_count``/``video_count``, and the encoded JSON
    ``body`` the YouTube Data API would return for that channel.
    """
    channel_id = draw(_channel_ids)
    title = draw(_text)
    subscriber_count = draw(_counts)
    video_count = draw(_counts)

    item: dict = {
        "id": channel_id,
        "snippet": {"title": title},
        "statistics": {
            # The real API returns these as numeric *strings*.
            "subscriberCount": str(subscriber_count),
            "videoCount": str(video_count),
        },
    }
    # Optionally attach topicDetails to exercise the realistic payload shape.
    if draw(st.booleans()):
        item["topicDetails"] = {"topicCategories": draw(_topic_categories)}

    payload = {"kind": "youtube#channelListResponse", "items": [item]}
    body = json.dumps(payload).encode("utf-8")

    return {
        "channel_id": channel_id,
        "title": title,
        "subscriber_count": subscriber_count,
        "video_count": video_count,
        "body": body,
    }


def _build_data_source(transport: FakeHttpTransport) -> YouTubeDataSource:
    """Build a YouTubeDataSource over the given transport with a real AuthManager.

    The ``AuthManager`` carries a ``Secret`` API key; ``get_channel_metadata``
    only consumes ``auth.data_api_params()``, so no OAuth or network is needed.
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
    )


# Feature: real-provider-integration, Property 1: Channel metadata mirrors the retrieved channel data
# Validates: Requirements 1.2
@settings(max_examples=200)
@given(case=_channel_case())
def test_channel_metadata_mirrors_retrieved_channel_data(case):
    """For any successful YouTube Data API channel response,
    ``get_channel_metadata`` returns exactly one ``ChannelMetadata`` whose
    ``channel_id`` equals the requested id and whose ``title``,
    ``subscriber_count``, and ``video_count`` equal the retrieved values."""
    transport = FakeHttpTransport()
    transport.queue_response(HttpResponse(status=200, body=case["body"]))
    data_source = _build_data_source(transport)

    result = data_source.get_channel_metadata(case["channel_id"])

    # Exactly one ChannelMetadata is returned (a single value, not a list).
    assert isinstance(result, ChannelMetadata)

    # It mirrors the retrieved data, with the requested id echoed back (1.2).
    assert result.channel_id == case["channel_id"]
    assert result.title == case["title"]
    assert result.subscriber_count == case["subscriber_count"]
    assert result.video_count == case["video_count"]

    # The source performs exactly one request and never retries internally.
    assert transport.call_count == 1
