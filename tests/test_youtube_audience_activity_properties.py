"""Hypothesis property test for YouTube audience-activity retrieval (task 6.2).

This module validates a single universal property of
``YouTubeDataSource.get_audience_activity`` (design.md -> *YouTubeDataSource* /
*Correctness Properties* -> Property 8):

- Property 8 (Requirements 3.1, 3.2, 3.6): for any successful YouTube Analytics
  API audience-activity response, ``get_audience_activity`` returns an
  ``AudienceActivity`` whose ``channel_id`` equals the requested owned-channel
  id, whose hourly ``buckets`` each carry a ``day_of_week`` in ``[0, 6]``, an
  ``hour`` in ``[0, 23]``, and a non-negative ``activity`` value, and whose
  ``days_covered`` is bounded to ``[0, days]`` (and is ``0`` with empty buckets
  when the response carries no activity data, 3.6).

The data source is driven through an injected :class:`FakeHttpTransport` (no
real network access, 16.3): a generated Analytics ``reports.query`` result table
(``columnHeaders`` + ``rows``) is queued as the single scripted response. The
:class:`AuthManager` is a real one built over a :class:`FakeHttpTransport` +
:class:`FakeClock` and an :class:`AuthSettings` whose ``oauth`` carries an
already-present access token, so ``get_audience_activity`` only consumes
``auth.analytics_authorized`` and ``auth.analytics_auth_header`` (a Bearer
header) without any OAuth refresh round-trip or network access.

The generator produces realistic rows — calendar dates (so the parser derives a
``day_of_week`` in ``[0, 6]``), hours in ``[0, 23]``, and non-negative metric
values — and asserts only the universal invariant the property states; the
specific error branches (missing OAuth, analytics auth error) are Property-8's
unit-test companions (task 6.3), not this property.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings, OAuthCredentials
from domain.models import AudienceActivity, HourlyActivity
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport

API_BASE_URL = "https://www.googleapis.com/youtube/v3"
ANALYTICS_BASE_URL = "https://youtubeanalytics.googleapis.com/v2"
REQUEST_TIMEOUT_SECONDS = 30.0

# A channel id is any non-empty identifier; it is echoed back onto the returned
# AudienceActivity regardless of the payload contents.
_channel_ids = st.text(st.characters(codec="utf-8"), min_size=1, max_size=40)
# The requested window length; days_covered must end up bounded to [0, days].
_days = st.integers(min_value=0, max_value=400)
# A base date the generated rows fan out from, kept well inside date.min/max.
_base_dates = st.dates(min_value=date(2000, 1, 1), max_value=date(2030, 12, 31))
# Hours fall in the valid [0, 23] range for a well-formed Analytics row.
_hours = st.integers(min_value=0, max_value=23)
# Non-negative activity values (the watch-time metric is never negative).
_activities = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)


@st.composite
def _audience_case(draw: st.DrawFn) -> dict:
    """Draw one successful Analytics result-table response and its inputs.

    Returns the requested ``channel_id`` and ``days`` plus the encoded JSON
    ``body`` the YouTube Analytics API would return: a ``reports.query`` result
    table with ``day``/``hour``/metric column headers and zero or more rows.
    Each row is a ``[date, hour, activity]`` triple drawn from valid ranges, so
    every row parses into a valid bucket.
    """
    channel_id = draw(_channel_ids)
    days = draw(_days)
    base = draw(_base_dates)

    n_rows = draw(st.integers(min_value=0, max_value=40))
    rows: list[list[object]] = []
    for _ in range(n_rows):
        # Spread row dates across a small span so several distinct days appear,
        # exercising the days_covered=min(distinct_days, days) bound.
        offset = draw(st.integers(min_value=0, max_value=10))
        row_date = base + timedelta(days=offset)
        hour = draw(_hours)
        activity = draw(_activities)
        # Mix JSON-number and string encodings the way the real API may report
        # values, so the parser's coercion is exercised both ways.
        if draw(st.booleans()):
            rows.append([row_date.isoformat(), hour, activity])
        else:
            rows.append([row_date.isoformat(), str(hour), str(activity)])

    payload = {
        "kind": "youtubeAnalytics#resultTable",
        "columnHeaders": [
            {"name": "day", "dataType": "STRING", "columnType": "DIMENSION"},
            {"name": "hour", "dataType": "STRING", "columnType": "DIMENSION"},
            {
                "name": "estimatedMinutesWatched",
                "dataType": "INTEGER",
                "columnType": "METRIC",
            },
        ],
        "rows": rows,
    }
    body = json.dumps(payload).encode("utf-8")

    return {"channel_id": channel_id, "days": days, "body": body}


def _build_data_source(transport: FakeHttpTransport) -> YouTubeDataSource:
    """Build a YouTubeDataSource over the given transport with OAuth configured.

    The ``AuthManager`` carries an OAuth access token already, so
    ``get_audience_activity`` applies the Bearer header without any refresh
    round-trip (and never touches the network for auth).
    """
    clock = FakeClock()
    oauth = OAuthCredentials(
        client_id="client-id-public",
        client_secret=Secret("client-secret", CredentialReference("oauth_client_secret")),
        refresh_token=Secret("refresh-token", CredentialReference("oauth_refresh_token")),
        access_token=Secret("access-token", CredentialReference("oauth_access_token")),
    )
    auth_settings = AuthSettings(
        youtube_api_key=Secret("api-key-value", CredentialReference("youtube_api_key")),
        oauth=oauth,
    )
    auth = AuthManager(auth_settings, transport, clock)
    return YouTubeDataSource(
        transport,
        auth,
        clock,
        api_base_url=API_BASE_URL,
        request_timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        analytics_base_url=ANALYTICS_BASE_URL,
    )


# Feature: real-provider-integration, Property 8: Audience activity has valid buckets and bounded coverage
# Validates: Requirements 3.1, 3.2, 3.6
@settings(max_examples=200)
@given(case=_audience_case())
def test_audience_activity_has_valid_buckets_and_bounded_coverage(case):
    """For any successful Analytics response, ``get_audience_activity`` returns
    an ``AudienceActivity`` whose channel id matches the request, whose buckets
    each carry day_of_week in [0,6], hour in [0,23], non-negative activity, and
    whose days_covered is bounded to [0, days] (Requirements 3.1, 3.2, 3.6)."""
    transport = FakeHttpTransport()
    transport.queue_response(HttpResponse(status=200, body=case["body"]))
    data_source = _build_data_source(transport)

    result = data_source.get_audience_activity(case["channel_id"], case["days"])

    # Exactly one AudienceActivity is returned for the requested channel (3.1).
    assert isinstance(result, AudienceActivity)
    assert result.channel_id == case["channel_id"]

    # Every bucket satisfies the day/hour/activity invariant (3.1).
    for bucket in result.buckets:
        assert isinstance(bucket, HourlyActivity)
        assert 0 <= bucket.day_of_week <= 6
        assert 0 <= bucket.hour <= 23
        assert bucket.activity >= 0

    # days_covered is bounded to [0, days] (3.2); and is 0 when no buckets (3.6).
    assert 0 <= result.days_covered <= case["days"]
    if not result.buckets:
        assert result.days_covered == 0

    # The source performs exactly one request and never retries internally.
    assert transport.call_count == 1
