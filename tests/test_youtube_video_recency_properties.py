"""Hypothesis property test for YouTube video retrieval + recency (task 5.5).

This module validates a single universal property of
``YouTubeDataSource.get_videos`` (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 3):

- Property 3 (Requirements 1.3, 1.4, 1.5): for any set of retrieved videos and
  any optional ``published_within_days = N``, ``get_videos`` returns one
  ``VideoStats`` per retrieved video (each carrying the video id, view count,
  and ISO-8601 published timestamp) filtered to exactly those whose published
  instant lies within ``[now - N days, now]`` when ``N`` is supplied (using the
  injected ``Clock`` for ``now``), and to the full retrieved set when ``N`` is
  not supplied.

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3): a single generated ``search.list`` JSON payload is
queued as the scripted response, so this property is scoped to mapping and
recency filtering (pagination is Property 4, task 5.6). ``now`` is supplied by a
:class:`FakeClock` pinned to a fixed reference instant, and each video's
published timestamp is generated as a known integer-second offset (``delta``)
from that instant, so the expected included set is derived independently of the
implementation: a parseable video is included under ``N`` iff
``-(N * 86400) <= delta <= 0`` (the inclusive window ``[now - N days, now]``);
an unparseable timestamp is excluded under any ``N`` but returned unfiltered
when ``N`` is absent. The :class:`AuthManager` is a real one over a
:class:`FakeHttpTransport` + :class:`FakeClock` carrying a :class:`Secret` API
key, since ``get_videos`` only consumes ``auth.data_api_params()`` (no OAuth,
no network).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

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
SECONDS_PER_DAY = 86_400

# A fixed reference "now" on the absolute timeline. The FakeClock returns this
# from monotonic(), and every generated published timestamp is an integer-second
# offset from it, so window boundaries are exact (integer-valued floats).
NOW_EPOCH = int(datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc).timestamp())

# A channel id is any non-empty identifier; it only shapes the request URL.
_channel_ids = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)
# Video ids and view counts are echoed through onto the returned VideoStats.
_video_ids = st.text(st.characters(codec="utf-8"), max_size=20)
_view_counts = st.integers(min_value=0, max_value=10**12)
# Timestamps the implementation cannot parse as ISO-8601: each yields no instant,
# so it is excluded by the recency filter and returned as-is when N is absent.
_UNPARSEABLE = ("", "not-a-date", "soon", "N/A", "2024-13-45T99:99:99", "tomorrow")


@st.composite
def _videos_case(draw: st.DrawFn) -> dict:
    """Draw one ``search.list`` page plus the independently-derived expected set.

    Returns the requested ``channel_id``, the optional ``published_within_days``
    (``None`` or a non-negative day count), the encoded JSON ``body`` the
    YouTube Data API would return, and ``expected`` -- the ordered list of
    ``(video_id, view_count, published_at)`` tuples ``get_videos`` must return.
    """
    channel_id = draw(_channel_ids)
    published_within_days = draw(
        st.one_of(st.none(), st.integers(min_value=0, max_value=365))
    )
    # Window used only to bias generated offsets so they straddle the boundaries
    # (a future instant > now, the now boundary, the lower boundary, and before).
    window_days = 365 if published_within_days is None else published_within_days
    margin = 5 * SECONDS_PER_DAY
    delta_strategy = st.integers(
        min_value=-(window_days * SECONDS_PER_DAY) - margin, max_value=margin
    )

    count = draw(st.integers(min_value=0, max_value=20))
    items: list[dict] = []
    videos: list[dict] = []  # {video_id, view_count, published_at, delta|None}
    for _ in range(count):
        video_id = draw(_video_ids)
        view_count = draw(_view_counts)
        if draw(st.booleans()):  # parseable ISO-8601 timestamp at a known offset
            delta = draw(delta_strategy)
            published_at = datetime.fromtimestamp(
                NOW_EPOCH + delta, tz=timezone.utc
            ).isoformat()
        else:  # unparseable timestamp: no placeable instant
            delta = None
            published_at = draw(st.sampled_from(_UNPARSEABLE))

        # The search.list result nests the id under id.videoId and the published
        # timestamp under snippet.publishedAt; view counts arrive as strings.
        items.append(
            {
                "id": {"videoId": video_id},
                "snippet": {"publishedAt": published_at},
                "statistics": {"viewCount": str(view_count)},
            }
        )
        videos.append(
            {
                "video_id": video_id,
                "view_count": view_count,
                "published_at": published_at,
                "delta": delta,
            }
        )

    # No nextPageToken: a single-page retrieval, keeping this property scoped to
    # mapping + recency rather than pagination (Property 4).
    payload = {"kind": "youtube#searchListResponse", "items": items}
    body = json.dumps(payload).encode("utf-8")

    if published_within_days is None:
        # No recency filter: one VideoStats per retrieved video, in order (1.5).
        expected = [
            (v["video_id"], v["view_count"], v["published_at"]) for v in videos
        ]
    else:
        # Inclusive window [now - N days, now] <=> -(N*86400) <= delta <= 0 (1.4).
        lower = -(published_within_days * SECONDS_PER_DAY)
        expected = [
            (v["video_id"], v["view_count"], v["published_at"])
            for v in videos
            if v["delta"] is not None and lower <= v["delta"] <= 0
        ]

    return {
        "channel_id": channel_id,
        "published_within_days": published_within_days,
        "body": body,
        "expected": expected,
    }


def _build_data_source(transport: FakeHttpTransport, clock: FakeClock) -> YouTubeDataSource:
    """Build a YouTubeDataSource over the transport/clock with a real AuthManager.

    ``get_videos`` only consumes ``auth.data_api_params()`` (the API key) and the
    injected ``clock`` for ``now``; no OAuth or network is required.
    """
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


# Feature: real-provider-integration, Property 3: Video retrieval maps one-to-one and applies exact recency filtering
# Validates: Requirements 1.3, 1.4, 1.5
@settings(max_examples=200)
@given(case=_videos_case())
def test_video_retrieval_maps_one_to_one_and_applies_exact_recency_filtering(case):
    """For any retrieved videos and any optional ``published_within_days = N``,
    ``get_videos`` returns one ``VideoStats`` per retrieved video (id, view
    count, ISO-8601 published timestamp) filtered to exactly the videos within
    ``[now - N days, now]`` when ``N`` is supplied, and the full retrieved set
    when ``N`` is absent."""
    transport = FakeHttpTransport()
    transport.queue_response(HttpResponse(status=200, body=case["body"]))
    clock = FakeClock(start=float(NOW_EPOCH))
    data_source = _build_data_source(transport, clock)

    result = data_source.get_videos(
        case["channel_id"], case["published_within_days"]
    )

    # Every returned element is a VideoStats (a one-to-one mapping per video).
    assert all(isinstance(v, VideoStats) for v in result)

    # The returned ids, view counts, and published timestamps equal exactly the
    # expected (filtered or full) set, in order (1.3, 1.4, 1.5).
    actual = [(v.video_id, v.view_count, v.published_at) for v in result]
    assert actual == case["expected"]

    # A single page is scripted, so exactly one transport request is performed
    # and no recency filter ever fabricates or drops the wrong videos.
    assert transport.call_count == 1
