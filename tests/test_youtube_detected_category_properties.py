"""Hypothesis property test for the YouTube channel-category mapping (task 5.3).

This module validates a single universal property of
``YouTubeDataSource.get_channel_metadata`` (design.md -> *YouTubeDataSource*;
Property 2), driven through an injected :class:`FakeHttpTransport` so no real
network access happens (16.3):

- Property 2 (1.6, 1.7): ``detected_category`` on the returned
  :class:`ChannelMetadata` is set **if and only if** the retrieved channel
  topic maps to a supported :class:`ChannelCategory`. When the topic maps, it
  is set to that category (1.6); when it does not, it is left unset (1.7).

The expected value is derived **independently** of the implementation: this
module replicates the documented topic->category marker rule (the
``_CATEGORY_TOPIC_MARKERS`` table in ``youtube_data_source.py``) as a local
oracle rather than importing the module-level helper, so the test verifies the
implementation against the documented rule rather than against itself.

The generators deliberately produce BOTH halves of the biconditional:

- ``topicDetails.topicCategories`` values that *do* map to a supported category
  (built from category marker words) -> ``detected_category`` must be set; and
- values that map to *no* supported category -- non-marker topics, an empty
  list, a list of non-string entries, a missing/empty ``topicDetails``, a
  non-``dict`` ``topicDetails``, and a non-list ``topicCategories`` -->
  ``detected_category`` must be ``None``.
"""

from __future__ import annotations

import json

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from domain.models import ChannelCategory
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport


# ---------------------------------------------------------------------------
# Independent oracle: the documented topic -> ChannelCategory marker rule.
#
# Replicated here (rather than imported) so the property checks the
# implementation against the *documented* rule. Categories are evaluated in a
# fixed precedence so the mapping is deterministic when a topic could match more
# than one marker set; the first match wins. A topic matching no marker set
# leaves the category unset (1.7).
# ---------------------------------------------------------------------------

_EXPECTED_MARKERS: tuple[tuple[ChannelCategory, tuple[str, ...]], ...] = (
    (ChannelCategory.GAMING, ("video game", "gaming", "game")),
    (ChannelCategory.MUSIC, ("music",)),
    (ChannelCategory.SPORTS, ("sport",)),
    (
        ChannelCategory.ENTERTAINMENT,
        ("entertainment", "film", "movie", "television"),
    ),
)


def _expected_category(topic_categories: object) -> ChannelCategory | None:
    """The documented mapping of ``topicCategories`` to a supported category."""
    if not isinstance(topic_categories, (list, tuple)):
        return None
    haystack = " ".join(t.lower() for t in topic_categories if isinstance(t, str))
    if not haystack:
        return None
    for category, markers in _EXPECTED_MARKERS:
        if any(marker in haystack for marker in markers):
            return category
    return None


def _expected_detected_category(item: dict) -> ChannelCategory | None:
    """Replicate the implementation's extraction path then map it.

    Mirrors how ``get_channel_metadata`` reads the topic: a ``topicDetails``
    that is not a ``dict`` is treated as empty, and its ``topicCategories`` is
    fed to the documented marker rule.
    """
    raw = item.get("topicDetails")
    topic_details = raw if isinstance(raw, dict) else {}
    return _expected_category(topic_details.get("topicCategories"))


# ---------------------------------------------------------------------------
# Topic-string generators.
#
# Marker words always map to a supported category; non-marker words never do
# (none of them contains a category marker as a substring). Each word is
# optionally wrapped in a Wikipedia-style URL (with a space, an underscore, or
# title-casing) to mimic the real ``topicCategories`` shape -- the mapping is
# case-insensitive substring matching, so wrapping never changes the outcome.
# ---------------------------------------------------------------------------

_MARKER_WORDS = [
    "video game",
    "gaming",
    "game",
    "music",
    "sport",
    "sports",
    "esports",
    "entertainment",
    "film",
    "movie",
    "television",
]

_NON_MARKER_WORDS = [
    "cooking",
    "travel",
    "news",
    "politics",
    "science",
    "technology",
    "education",
    "comedy",
    "lifestyle",
    "fashion",
    "health",
    "business",
    "history",
    "art",
    "documentary",
]


def _wrap_topic(word: str, style: str) -> str:
    if style == "plain":
        return word
    if style == "space":
        return f"https://en.wikipedia.org/wiki/{word} culture"
    if style == "underscore":
        return f"https://en.wikipedia.org/wiki/{word.replace(' ', '_')}_culture"
    # style == "title"
    return f"https://en.wikipedia.org/wiki/{word.replace(' ', '_').title()}"


def _topic_strings(words: list[str]) -> st.SearchStrategy[str]:
    return st.builds(
        _wrap_topic,
        st.sampled_from(words),
        st.sampled_from(["plain", "space", "underscore", "title"]),
    )


_marker_topic = _topic_strings(_MARKER_WORDS)
_non_marker_topic = _topic_strings(_NON_MARKER_WORDS)
_any_topic = st.one_of(_marker_topic, _non_marker_topic)
_non_string_entry = st.one_of(st.none(), st.integers(), st.booleans(), st.floats())


@st.composite
def _channel_item(draw: st.DrawFn) -> dict:
    """Draw one channel item with a deliberately varied ``topicDetails``.

    Covers both halves of the iff: topics that map to a supported category and
    topics (or shapes) that map to none.
    """
    item: dict = {
        "id": draw(
            st.text(
                alphabet=st.characters(min_codepoint=48, max_codepoint=122),
                min_size=1,
                max_size=24,
            )
        ),
        "snippet": {"title": draw(st.text(min_size=0, max_size=30))},
        "statistics": {
            "subscriberCount": str(draw(st.integers(min_value=0, max_value=10_000_000))),
            "videoCount": str(draw(st.integers(min_value=0, max_value=100_000))),
        },
    }

    variant = draw(
        st.sampled_from(
            [
                "maps",  # marker topics -> a supported category
                "non_maps",  # non-marker topics -> None
                "mixed",  # marker + non-marker -> precedence decides
                "empty_list",  # [] -> None
                "non_string_entries",  # ints/None mixed with maybe a string
                "no_topic_details",  # topicDetails key absent -> None
                "topic_details_not_dict",  # topicDetails not a dict -> None
                "topic_categories_missing",  # dict without topicCategories -> None
                "topic_categories_not_list",  # a scalar, not a list -> None
            ]
        )
    )

    if variant == "maps":
        item["topicDetails"] = {
            "topicCategories": draw(st.lists(_marker_topic, min_size=1, max_size=4))
        }
    elif variant == "non_maps":
        item["topicDetails"] = {
            "topicCategories": draw(
                st.lists(_non_marker_topic, min_size=1, max_size=4)
            )
        }
    elif variant == "mixed":
        item["topicDetails"] = {
            "topicCategories": draw(st.lists(_any_topic, min_size=1, max_size=5))
        }
    elif variant == "empty_list":
        item["topicDetails"] = {"topicCategories": []}
    elif variant == "non_string_entries":
        item["topicDetails"] = {
            "topicCategories": draw(
                st.lists(
                    st.one_of(_any_topic, _non_string_entry), min_size=1, max_size=4
                )
            )
        }
    elif variant == "no_topic_details":
        pass  # leave topicDetails absent
    elif variant == "topic_details_not_dict":
        item["topicDetails"] = draw(
            st.one_of(
                st.text(max_size=10),
                st.lists(st.text(max_size=5), max_size=3),
                st.integers(),
            )
        )
    elif variant == "topic_categories_missing":
        item["topicDetails"] = draw(
            st.dictionaries(
                keys=st.sampled_from(["topicIds", "etag", "other"]),
                values=st.text(max_size=10),
                max_size=3,
            )
        )
    else:  # topic_categories_not_list
        item["topicDetails"] = {
            "topicCategories": draw(st.one_of(_any_topic, st.integers()))
        }

    return item


def _build_source() -> tuple[YouTubeDataSource, FakeHttpTransport]:
    """A YouTubeDataSource over a fake transport and a real AuthManager.

    The AuthManager is built over its own :class:`FakeHttpTransport` and a
    :class:`FakeClock` with an ``AuthSettings`` carrying a :class:`Secret` API
    key; the data source gets a separate fake transport whose single scripted
    response the test supplies. No real network access (16.3).
    """
    clock = FakeClock()
    auth_settings = AuthSettings(
        youtube_api_key=Secret("test-api-key", CredentialReference("youtube_api_key"))
    )
    auth = AuthManager(auth_settings, FakeHttpTransport(), clock)
    data_transport = FakeHttpTransport()
    source = YouTubeDataSource(
        data_transport,
        auth,
        clock,
        api_base_url="https://youtube.test/v3",
        request_timeout_seconds=30.0,
    )
    return source, data_transport


# Feature: real-provider-integration, Property 2: Detected category is set if and only if the retrieved topic maps to a supported category
# Validates: Requirements 1.6, 1.7
@settings(max_examples=200)
@given(item=_channel_item(), channel_id=st.text(min_size=1, max_size=24))
def test_detected_category_is_set_iff_topic_maps_to_supported_category(
    item, channel_id
):
    """For any retrieved channel topic, ``get_channel_metadata`` sets
    ``detected_category`` exactly when the topic maps to a supported
    ``ChannelCategory`` (1.6), to that category, and leaves it unset
    otherwise (1.7)."""
    source, transport = _build_source()
    transport.queue_response(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps({"items": [item]}).encode("utf-8"),
        )
    )

    metadata = source.get_channel_metadata(channel_id)

    expected = _expected_detected_category(item)

    # The biconditional (Property 2): set iff the topic maps to a supported
    # category, and equal to that category when set.
    if expected is None:
        assert metadata.detected_category is None
    else:
        assert isinstance(expected, ChannelCategory)
        assert metadata.detected_category == expected

    # The source performs exactly one request and never retries internally.
    assert transport.call_count == 1


# ---------------------------------------------------------------------------
# Focused example checks documenting each branch of the iff.
# ---------------------------------------------------------------------------


def _detected_for(topic_details: object) -> ChannelCategory | None:
    source, transport = _build_source()
    item: dict = {
        "id": "chan",
        "snippet": {"title": "A channel"},
        "statistics": {"subscriberCount": "10", "videoCount": "5"},
    }
    if topic_details is not _ABSENT:
        item["topicDetails"] = topic_details
    transport.queue_response(
        HttpResponse(
            status=200,
            headers={},
            body=json.dumps({"items": [item]}).encode("utf-8"),
        )
    )
    return source.get_channel_metadata("chan").detected_category


_ABSENT = object()


def test_gaming_topic_maps_to_gaming():
    assert (
        _detected_for(
            {"topicCategories": ["https://en.wikipedia.org/wiki/Video_game_culture"]}
        )
        is ChannelCategory.GAMING
    )


def test_music_topic_maps_to_music():
    assert (
        _detected_for({"topicCategories": ["https://en.wikipedia.org/wiki/Music"]})
        is ChannelCategory.MUSIC
    )


def test_sports_topic_maps_to_sports():
    assert (
        _detected_for({"topicCategories": ["https://en.wikipedia.org/wiki/Sport"]})
        is ChannelCategory.SPORTS
    )


def test_entertainment_topic_maps_to_entertainment():
    assert (
        _detected_for({"topicCategories": ["https://en.wikipedia.org/wiki/Film"]})
        is ChannelCategory.ENTERTAINMENT
    )


def test_unmapped_topic_leaves_category_unset():
    assert (
        _detected_for({"topicCategories": ["https://en.wikipedia.org/wiki/Cooking"]})
        is None
    )


def test_empty_topic_list_leaves_category_unset():
    assert _detected_for({"topicCategories": []}) is None


def test_missing_topic_details_leaves_category_unset():
    assert _detected_for(_ABSENT) is None


def test_non_list_topic_categories_leaves_category_unset():
    # A scalar that *contains* a marker still does not map: only a list/tuple of
    # topic strings is consulted.
    assert _detected_for({"topicCategories": "music"}) is None
