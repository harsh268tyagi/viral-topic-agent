"""The concrete YouTube-backed ``DataSource`` implementation.

The design (``.kiro/specs/real-provider-integration/design.md`` ->
*YouTubeDataSource*) calls for a concrete ``DataSource`` that talks to the
public YouTube Data API v3 (and, for audience activity, the YouTube Analytics
API). It implements the existing :class:`~infrastructure.datasource.DataSource`
protocol *exactly* and raises only members of the existing
``DataSourceError`` hierarchy, so the existing ``ResilientDataSource`` decorator
keeps applying retry, rate-limit backoff, and timeout policy unchanged (16.1,
16.5).

This module owns request shaping, response parsing, pagination, and **error
classification only**: it performs no retry or backoff of its own (16.5). All
failure signals are routed through
:func:`~infrastructure.youtube_error_mapping.classify_response`, which returns
the single hierarchy member the caller should raise.

Implemented here: the constructor, :meth:`get_channel_metadata` (1.2, 1.6, 1.7),
:meth:`get_videos` (1.3, 1.4, 1.5, 1.8), :meth:`get_audience_activity`
(Requirement 3) — owned-channel audience activity via the YouTube Analytics
API — and the two configurable-seam methods :meth:`get_keyword_metrics`
(Requirement 4) and :meth:`get_template_performance` (Requirement 5), which
delegate to an injected
:class:`~infrastructure.keyword_metrics_provider.KeywordMetricsProvider` and
:class:`~infrastructure.template_performance_strategy.TemplatePerformanceStrategy`
respectively and degrade to an empty list when the corresponding seam is not
configured (4.3, 5.3).

Requirements traceability: 1.2/1.6/1.7 (channel metadata + category mapping),
1.3/1.4/1.5/1.8 (videos, recency filter, pagination), 3.1/3.2/3.3/3.4/3.5/3.6
(audience activity: valid buckets, bounded coverage, missing-OAuth and
auth-error handling, classified failures, empty-but-successful), 4.1/4.2/4.3/4.4
(keyword metrics: one-to-one mapping, cap, unconfigured degradation, classified
failure), 5.1/5.2/5.3/5.4 (template performance: derive-one-per-template,
all fields populated, unconfigured degradation, classified retrieval failure).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from urllib.parse import urlencode

from domain.models import (
    AudienceActivity,
    ChannelCategory,
    ChannelMetadata,
    HourlyActivity,
    KeywordMetric,
    TemplatePerformance,
    VideoStats,
)
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import Clock
from infrastructure.datasource import NonTransientError
from infrastructure.http_transport import (
    HttpResponse,
    HttpTransport,
    HttpTransportError,
)
from infrastructure.keyword_metrics_provider import KeywordMetricsProvider
from infrastructure.template_performance_strategy import TemplatePerformanceStrategy
from infrastructure.youtube_error_mapping import classify_response

__all__ = ["YouTubeDataSource"]


# ---------------------------------------------------------------------------
# Topic -> supported ChannelCategory mapping (1.6, 1.7)
# ---------------------------------------------------------------------------

# The YouTube Data API reports a channel's topics as ``topicDetails.topicCategories``,
# a list of Wikipedia article URLs (e.g. ``.../wiki/Video_game_culture``). A
# retrieved topic maps to a supported :class:`ChannelCategory` when any of that
# category's keyword markers appears (case-insensitively) in any retrieved topic
# string. Categories are evaluated in this fixed order so the mapping is
# deterministic when a topic could match more than one marker set; the first
# match wins. A topic matching none of the marker sets leaves
# ``detected_category`` unset (1.7).
_CATEGORY_TOPIC_MARKERS: tuple[tuple[ChannelCategory, tuple[str, ...]], ...] = (
    (ChannelCategory.GAMING, ("video game", "gaming", "game")),
    (ChannelCategory.MUSIC, ("music",)),
    (ChannelCategory.SPORTS, ("sport",)),
    (
        ChannelCategory.ENTERTAINMENT,
        ("entertainment", "film", "movie", "television"),
    ),
)


def _map_topics_to_category(topics: object) -> ChannelCategory | None:
    """Map retrieved channel topics to a supported category, else ``None``.

    ``topics`` is whatever the API placed under ``topicDetails.topicCategories``
    (normally a list of strings); non-string entries and non-list values are
    ignored. Returns the first supported :class:`ChannelCategory` whose marker
    set appears in any topic (1.6), or ``None`` when no topic maps (1.7).
    """
    if not isinstance(topics, (list, tuple)):
        return None
    haystack = " ".join(t.lower() for t in topics if isinstance(t, str))
    if not haystack:
        return None
    for category, markers in _CATEGORY_TOPIC_MARKERS:
        if any(marker in haystack for marker in markers):
            return category
    return None


def _coerce_count(value: object) -> int:
    """Parse a YouTube statistics count (a numeric string) into an ``int``.

    YouTube returns ``statistics`` counts as strings (e.g. ``"12345"``); a value
    that is absent or not a base-10 integer degrades to ``0`` so a channel that
    hides a count still yields a well-formed :class:`ChannelMetadata`.
    """
    if isinstance(value, bool):  # guard: bool is an int subclass
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return 0
    return 0


# Seconds in one day, used to convert a ``published_within_days`` window into a
# lower-bound instant relative to the injected clock's "now" (1.4).
_SECONDS_PER_DAY = 86_400.0


def _parse_iso8601_to_epoch(value: object) -> float | None:
    """Parse an ISO-8601 timestamp into POSIX seconds, or ``None``.

    Mirrors the parsing the analysis layer already uses for
    ``VideoStats.published_at``: a trailing ``Z`` is normalised to ``+00:00``
    (``datetime.fromisoformat`` only accepts ``Z`` from Python 3.11) and a naive
    timestamp is assumed to be UTC, so every parsed instant is comparable on a
    single absolute timeline. A value that is absent or not a parseable ISO-8601
    string yields ``None`` so the recency filter can exclude it (its instant
    cannot be placed within the window).
    """
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = f"{raw[:-1]}+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


# ---------------------------------------------------------------------------
# Audience-activity row parsing (Requirement 3)
# ---------------------------------------------------------------------------

# The YouTube Analytics API ``reports.query`` response is a result table: a list
# of ``columnHeaders`` (each ``{"name": ...}``) and a list of ``rows`` (each a
# positional list of dimension/metric values). The audience-activity query asks
# for the ``day`` and ``hour`` dimensions and a watch-time metric, so each row
# carries a calendar date, an hour-of-day, and an activity value. These constants
# name the two dimensions the parser resolves by header name (falling back to a
# fixed ``day, hour, metric`` column order when headers are absent).
_ANALYTICS_DAY_COLUMN = "day"
_ANALYTICS_HOUR_COLUMN = "hour"


def _coerce_optional_int(value: object) -> int | None:
    """Parse an integer that may arrive as an ``int``, whole ``float``, or string.

    The Analytics API reports a dimension value sometimes as a JSON number and
    sometimes as a string; a non-integral or unparseable value yields ``None``
    so the caller can drop the row. ``bool`` is rejected (it is an ``int``
    subclass but never a valid dimension value).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_activity(value: object) -> float | None:
    """Parse an activity metric into a ``float``, or ``None`` when unparseable.

    Accepts a JSON number or a numeric string (the API reports metrics either
    way); ``bool`` and anything non-numeric yield ``None`` so the row is dropped.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _date_to_weekday(value: object) -> int | None:
    """Map an ISO date (``YYYY-MM-DD``) to a weekday in ``[0, 6]`` (Monday=0).

    ``HourlyActivity.day_of_week`` is documented as ``0..6`` with Monday=0, which
    is exactly :meth:`datetime.date.weekday`. A value that is absent or not a
    parseable ISO date yields ``None`` so the caller can drop the row, keeping
    the bucket invariant total (3.1).
    """
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()).weekday()
    except ValueError:
        return None


class YouTubeDataSource:
    """A :class:`~infrastructure.datasource.DataSource` backed by the YouTube APIs.

    Satisfies the existing ``DataSource`` protocol structurally (1.1, 16.1). It
    is constructor-injected with an :class:`HttpTransport`, an
    :class:`AuthManager`, and a :class:`Clock` so every branch is exercisable
    against fakes with no real network access (16.3, 16.4). It classifies
    failures but never retries (16.5): every failure signal is routed through
    :func:`classify_response`.
    """

    def __init__(
        self,
        transport: HttpTransport,
        auth: AuthManager,
        clock: Clock,
        *,
        api_base_url: str,
        request_timeout_seconds: float,
        analytics_base_url: str = "https://youtubeanalytics.googleapis.com/v2",
        keyword_provider: KeywordMetricsProvider | None = None,
        template_strategy: TemplatePerformanceStrategy | None = None,
        max_items: int = 50,
    ) -> None:
        self._transport = transport
        self._auth = auth
        self._clock = clock
        self._api_base_url = api_base_url.rstrip("/")
        self._request_timeout_seconds = request_timeout_seconds
        self._analytics_base_url = analytics_base_url.rstrip("/")
        self._keyword_provider = keyword_provider
        self._template_strategy = template_strategy
        self._max_items = max_items

    # ------------------------------------------------------------------
    # Channel metadata (Requirements 1.2, 1.6, 1.7) — this task
    # ------------------------------------------------------------------

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        """Retrieve one channel's metadata from the YouTube Data API (1.2).

        Returns exactly one :class:`ChannelMetadata` populated with the
        requested ``channel_id`` and the retrieved title, subscriber count, and
        video count (1.2). ``detected_category`` is set only when the retrieved
        topic maps to a supported :class:`ChannelCategory` (1.6) and is left
        unset otherwise (1.7). Failure signals are routed through
        :func:`classify_response` so the source raises only members of the
        existing error hierarchy (16.5).
        """
        params = {
            "part": "snippet,statistics,topicDetails",
            "id": channel_id,
            **self._auth.data_api_params(),
        }
        url = f"{self._api_base_url}/channels?{urlencode(params)}"
        description = f"get_channel_metadata for channel {channel_id}"

        response = self._request("GET", url, request_description=description)
        payload = self._parse_json(response, request_description=description)

        items = payload.get("items")
        if not isinstance(items, list) or not items:
            # A successful (2xx) response with no matching channel: the channel
            # id does not resolve. Surface a non-retryable failure naming the
            # request rather than fabricating empty metadata.
            raise NonTransientError(f"{description} returned no channel")

        item = items[0] if isinstance(items[0], dict) else {}
        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        statistics = (
            item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
        )
        topic_details = (
            item.get("topicDetails")
            if isinstance(item.get("topicDetails"), dict)
            else {}
        )

        title = snippet.get("title", "")
        if not isinstance(title, str):
            title = str(title)

        return ChannelMetadata(
            channel_id=channel_id,
            title=title,
            subscriber_count=_coerce_count(statistics.get("subscriberCount")),
            video_count=_coerce_count(statistics.get("videoCount")),
            detected_category=_map_topics_to_category(
                topic_details.get("topicCategories")
            ),
        )

    # ------------------------------------------------------------------
    # Remaining DataSource methods — implemented by later tasks
    # ------------------------------------------------------------------

    def get_videos(
        self, channel_id: str, published_within_days: int | None = None
    ) -> list[VideoStats]:
        """Retrieve a channel's videos as :class:`VideoStats` (1.3, 1.4, 1.5, 1.8).

        Returns one :class:`VideoStats` per retrieved video, each carrying the
        video id, view count, and the ISO-8601 published timestamp exactly as
        the YouTube Data API reports it (1.3). Pages are requested by following
        ``nextPageToken`` until the API reports no further page **or** the count
        of retrieved videos reaches ``max_items``, whichever comes first (1.8);
        the configured timeout is applied per request and no retry/backoff is
        performed here, every failure being routed through
        :func:`classify_response` by :meth:`_request` (16.4, 16.5).

        When ``published_within_days`` is ``N``, only videos whose published
        instant lies within ``[now - N days, now]`` are returned, using the
        injected :class:`Clock` for ``now`` (1.4, 16.4). With no
        ``published_within_days`` the full retrieved set is returned without any
        time filtering (1.5).
        """
        description = f"get_videos for channel {channel_id}"
        collected: list[VideoStats] = []
        page_token: str | None = None

        while True:
            url = self._build_videos_url(channel_id, page_token)
            response = self._request("GET", url, request_description=description)
            payload = self._parse_json(response, request_description=description)

            items = payload.get("items")
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    collected.append(self._parse_video_item(item))
                    if len(collected) >= self._max_items:
                        break

            # Stop at the first of: the maximum item count is reached (1.8) ...
            if len(collected) >= self._max_items:
                collected = collected[: self._max_items]
                break

            # ... or the API reports no further page (1.8).
            next_token = payload.get("nextPageToken")
            if not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token

        if published_within_days is None:
            # No recency filter: return one VideoStats per retrieved video (1.5).
            return collected
        return self._filter_by_recency(collected, published_within_days)

    def get_audience_activity(self, channel_id: str, days: int) -> AudienceActivity:
        """Retrieve owned-channel audience activity from the Analytics API (Req. 3).

        Requires OAuth authorization for the owned channel: when no OAuth
        credentials are configured at all, this raises a
        :class:`NonTransientError` whose reason indicates that audience activity
        requires YouTube Analytics API authorization for the owned channel (3.3)
        and returns no :class:`AudienceActivity`.

        On a successful response it returns one :class:`AudienceActivity` whose
        ``channel_id`` equals the requested owned-channel id, whose ``buckets``
        each carry a ``day_of_week`` in ``[0, 6]`` (Monday=0), an ``hour`` in
        ``[0, 23]``, and a non-negative ``activity`` value, and whose
        ``days_covered`` is the number of distinct days present in the retrieved
        data, bounded to ``[0, days]`` (3.1, 3.2). A row that cannot be parsed
        into a valid bucket (unparseable date/hour, hour out of range, or a
        negative/unparseable activity value) is dropped, so the bucket invariant
        holds for any response. A successful response carrying no activity data
        yields an :class:`AudienceActivity` with ``days_covered`` of ``0`` and
        empty ``buckets`` (3.6).

        An Analytics authorization or permission error surfaces as a
        :class:`NonTransientError` naming the owned channel and returns no
        activity (3.4); a rate limit, a server error of status 500 or greater, a
        network connection error, or a timeout each surface as the corresponding
        member of the existing error hierarchy per Requirement 2, via
        :func:`classify_response` (3.5). This method performs no retry or backoff
        of its own beyond a single OAuth-token refresh-and-reissue (16.5, 13.3).
        """
        if not self._auth.analytics_authorized:
            # No OAuth configured at all: audience activity is unavailable and
            # this is a non-retryable configuration condition (3.3).
            raise NonTransientError(
                f"get_audience_activity for owned channel {channel_id} requires "
                "YouTube Analytics API authorization for the owned channel"
            )

        description = f"get_audience_activity for owned channel {channel_id}"
        url = self._build_analytics_url(channel_id, days)
        response = self._request_analytics(channel_id, url, description)
        payload = self._parse_json(response, request_description=description)
        return self._parse_audience_activity(channel_id, days, payload)

    def get_keyword_metrics(
        self, category: ChannelCategory, max_keywords: int
    ) -> list[KeywordMetric]:
        """Retrieve keyword demand/competition metrics for a category (Req. 4).

        When a :class:`~infrastructure.keyword_metrics_provider.KeywordMetricsProvider`
        is configured, this delegates to it and returns one
        :class:`KeywordMetric` per retrieved keyword, carrying that keyword's
        demand and competition values (4.1), capped at ``max_keywords`` (4.2). A
        ``max_keywords`` of ``0`` or fewer yields an empty list (no keyword is
        requested). When **no** provider is configured this returns an empty
        list, preserving graceful degradation to ``insufficient-data`` (4.3).

        A configured provider that is unavailable or errors surfaces as exactly
        one member of the existing ``DataSourceError`` hierarchy: a
        transport-level failure (timeout, connection error/reset) raised by the
        provider is mapped through :func:`classify_response` (4.4); a
        ``DataSourceError`` the provider raises directly propagates unchanged.
        This method performs no retry or backoff of its own (16.5).
        """
        if self._keyword_provider is None:
            # No provider configured: degrade to insufficient-data (4.3).
            return []

        description = f"get_keyword_metrics for category {category.value}"
        try:
            metrics = self._keyword_provider.fetch(category, max_keywords)
        except HttpTransportError as exc:  # includes HttpTimeoutError
            # A configured provider's transport-level failure maps to the
            # corresponding classified DataSourceError (4.4).
            raise classify_response(description, transport_error=exc)

        if max_keywords <= 0:
            return []
        # Cap the result at the requested maximum (4.2); the one-to-one mapping
        # of retrieved keyword -> KeywordMetric is the provider's contract (4.1).
        return list(metrics[:max_keywords])

    def get_template_performance(
        self, category: ChannelCategory
    ) -> list[TemplatePerformance]:
        """Derive viral-template performance for a category (Requirement 5).

        When a
        :class:`~infrastructure.template_performance_strategy.TemplatePerformanceStrategy`
        is configured, this retrieves the category's video statistics and
        applies the strategy to them, returning one :class:`TemplatePerformance`
        per derived template (5.1) with every field populated by the strategy
        (5.2). When **no** strategy is configured this returns an empty list,
        preserving graceful degradation (5.3).

        Deriving requires retrieving video statistics from the YouTube Data API;
        if that retrieval fails it surfaces as the corresponding classified
        ``DataSourceError`` because :meth:`get_videos` routes every failure
        through :func:`classify_response` (5.4). A ``DataSourceError`` the
        strategy raises directly propagates unchanged. This method performs no
        retry or backoff of its own (16.5).
        """
        if self._template_strategy is None:
            # No strategy configured: degrade to an empty list (5.3).
            return []

        # Retrieve the video statistics the strategy derives from. Any retrieval
        # failure is classified by get_videos -> _request (5.4).
        videos = self.get_videos(category.value)
        return list(self._template_strategy.derive(category, videos))

    # ------------------------------------------------------------------
    # Internal request / parsing helpers
    # ------------------------------------------------------------------

    def _build_videos_url(self, channel_id: str, page_token: str | None) -> str:
        """Build one ``search.list`` request URL for a channel's videos (1.8).

        Requests the channel's videos ordered by recency, capping the per-page
        size at ``max_items`` (the API also bounds it), and threads
        ``pageToken`` when continuing a multi-page retrieval so successive pages
        are followed via ``nextPageToken`` (1.8). Authentication is applied by
        merging :meth:`AuthManager.data_api_params` (the API key, 13.1).
        """
        params: dict[str, str] = {
            "part": "snippet",
            "channelId": channel_id,
            "type": "video",
            "order": "date",
            "maxResults": str(min(self._max_items, 50)),
            **self._auth.data_api_params(),
        }
        if page_token:
            params["pageToken"] = page_token
        return f"{self._api_base_url}/search?{urlencode(params)}"

    @staticmethod
    def _parse_video_item(item: Mapping[str, Any]) -> VideoStats:
        """Map one retrieved video item to a :class:`VideoStats` (1.3).

        Extracts the video id, the view count, and the ISO-8601 published
        timestamp. The search result nests the id under ``id.videoId`` and the
        published timestamp under ``snippet.publishedAt``; statistics (the view
        count) are reported as numeric strings. Missing pieces degrade to a
        well-formed value (empty id, ``0`` views, empty timestamp) rather than
        failing the whole page.
        """
        id_field = item.get("id")
        if isinstance(id_field, dict):
            video_id = id_field.get("videoId", "")
        else:
            video_id = id_field if isinstance(id_field, str) else ""
        if not isinstance(video_id, str):
            video_id = str(video_id)

        snippet = item.get("snippet") if isinstance(item.get("snippet"), dict) else {}
        statistics = (
            item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
        )

        published_at = snippet.get("publishedAt", "")
        if not isinstance(published_at, str):
            published_at = str(published_at)

        return VideoStats(
            video_id=video_id,
            view_count=_coerce_count(statistics.get("viewCount")),
            published_at=published_at,
        )

    def _filter_by_recency(
        self, videos: list[VideoStats], published_within_days: int
    ) -> list[VideoStats]:
        """Keep only videos published within ``[now - N days, now]`` (1.4).

        ``now`` is read from the injected :class:`Clock` (16.4); the window's
        lower bound is ``now - published_within_days`` days. A video whose
        ``published_at`` cannot be parsed as an ISO-8601 instant is excluded,
        since its instant cannot be placed within the window. Order is
        preserved.
        """
        now = self._clock.monotonic()
        lower_bound = now - published_within_days * _SECONDS_PER_DAY
        kept: list[VideoStats] = []
        for video in videos:
            instant = _parse_iso8601_to_epoch(video.published_at)
            if instant is None:
                continue
            if lower_bound <= instant <= now:
                kept.append(video)
        return kept

    # ------------------------------------------------------------------
    # Audience-activity request / parsing helpers (Requirement 3)
    # ------------------------------------------------------------------

    def _build_analytics_url(self, channel_id: str, days: int) -> str:
        """Build one Analytics ``reports.query`` URL for audience activity (3.1).

        Requests the ``day`` and ``hour`` dimensions with a watch-time metric
        over the trailing ``days``-day window ending today, scoped to the owned
        channel. The window end is "today" per the injected clock's notion of a
        wall date is not modelled (the :class:`Clock` is monotonic only), so the
        date range is expressed relative to ``date.today()``; the values only
        shape the request and do not affect parsing, which derives
        ``days_covered`` from the returned rows (3.2). Authentication is the
        OAuth bearer applied in :meth:`_request_analytics`.
        """
        window = max(days, 0)
        end = date.today()
        start = end - timedelta(days=window)
        params = {
            "ids": f"channel=={channel_id}",
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": f"{_ANALYTICS_DAY_COLUMN},{_ANALYTICS_HOUR_COLUMN}",
            "metrics": "estimatedMinutesWatched",
        }
        return f"{self._analytics_base_url}/reports?{urlencode(params)}"

    def _request_analytics(
        self, channel_id: str, url: str, description: str
    ) -> HttpResponse:
        """Perform one Analytics request, refreshing the token once if expired.

        Applies the owned channel's OAuth bearer (13.2). On an authorization or
        permission response that indicates an *expired* access token and a
        refresh token is available, the token is refreshed and the request is
        reissued exactly once (13.3). An authorization/permission error that is
        not a recoverable expiry surfaces as a :class:`NonTransientError` naming
        the owned channel (3.4); every other failure signal (rate limit, server
        error, connection error, timeout) is mapped by :func:`classify_response`
        per Requirement 2 (3.5).
        """
        response = self._send_analytics(channel_id, url)

        if self._is_analytics_auth_failure(response):
            if self._auth.is_access_token_expired(response):
                # Expired token: refresh (raises NonTransientError naming the
                # channel when no refresh token / refresh fails, 13.4/13.7) and
                # reissue exactly once (13.3).
                self._auth.refresh_access_token(channel_id)
                response = self._send_analytics(channel_id, url)
                if self._is_analytics_auth_failure(response):
                    raise self._analytics_auth_error(channel_id)
            else:
                # A genuine authorization/permission failure (3.4).
                raise self._analytics_auth_error(channel_id)

        if response.status >= 400:
            # Rate limit, server error, or any other error status -> Req. 2 (3.5).
            raise classify_response(description, response=response)
        return response

    def _send_analytics(self, channel_id: str, url: str) -> HttpResponse:
        """Issue one Analytics GET with the OAuth bearer, mapping transport errors.

        A transport-level failure (connection error/reset or timeout) is routed
        through :func:`classify_response` to the corresponding transient/timeout
        error (3.5); the HTTP status (including auth/error statuses) is returned
        for the caller to interpret.
        """
        description = f"get_audience_activity for owned channel {channel_id}"
        headers = self._auth.analytics_auth_header(channel_id)
        try:
            return self._transport.request(
                "GET",
                url,
                headers=dict(headers),
                body=None,
                timeout_seconds=self._request_timeout_seconds,
            )
        except HttpTransportError as exc:  # includes HttpTimeoutError
            raise classify_response(description, transport_error=exc)

    @staticmethod
    def _is_analytics_auth_failure(response: HttpResponse) -> bool:
        """Whether an Analytics response is an authorization/permission failure."""
        return response.status in (401, 403)

    @staticmethod
    def _analytics_auth_error(channel_id: str) -> NonTransientError:
        """Build the authorization-failure error naming the owned channel (3.4)."""
        return NonTransientError(
            f"get_audience_activity for owned channel {channel_id} was denied: "
            "YouTube Analytics API authorization or permission failure"
        )

    def _parse_audience_activity(
        self, channel_id: str, days: int, payload: Mapping[str, Any]
    ) -> AudienceActivity:
        """Build an :class:`AudienceActivity` from an Analytics result table (3.1, 3.2, 3.6).

        Resolves the ``day`` and ``hour`` columns (by header name, else by the
        requested ``day, hour, metric`` order) and turns each parseable row into
        an :class:`HourlyActivity` whose ``day_of_week`` derives from the row's
        calendar date (Monday=0, so always ``[0, 6]``), whose ``hour`` is
        validated to ``[0, 23]``, and whose ``activity`` is the non-negative
        metric value. Rows that cannot be parsed into a valid bucket are dropped
        so the bucket invariant always holds. ``days_covered`` is the count of
        distinct calendar dates among the kept rows, bounded to ``[0, days]``
        (3.2). A response with no usable rows yields zero coverage and empty
        buckets (3.6).
        """
        day_index, hour_index, metric_index = self._resolve_analytics_columns(payload)

        rows = payload.get("rows")
        buckets: list[HourlyActivity] = []
        distinct_dates: set[str] = set()
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, (list, tuple)):
                    continue
                bucket = self._row_to_bucket(row, day_index, hour_index, metric_index)
                if bucket is None:
                    continue
                buckets.append(bucket)
                raw_date = row[day_index]
                if isinstance(raw_date, str):
                    distinct_dates.add(raw_date.strip())

        upper = max(days, 0)
        days_covered = min(len(distinct_dates), upper)
        return AudienceActivity(
            channel_id=channel_id,
            days_covered=days_covered,
            buckets=tuple(buckets),
        )

    @staticmethod
    def _resolve_analytics_columns(
        payload: Mapping[str, Any]
    ) -> tuple[int, int, int]:
        """Resolve the (day, hour, metric) column indices of the result table.

        Prefers the positions reported in ``columnHeaders`` (matching the
        ``day``/``hour`` dimension names case-insensitively, with the first
        non-dimension column treated as the metric). Falls back to the fixed
        ``day=0, hour=1, metric=2`` order the query requests when headers are
        absent or incomplete.
        """
        headers = payload.get("columnHeaders")
        day_index = 0
        hour_index = 1
        metric_index = 2
        if isinstance(headers, list):
            names = [
                h.get("name") if isinstance(h, dict) else None for h in headers
            ]
            lowered = [n.lower() if isinstance(n, str) else None for n in names]
            if _ANALYTICS_DAY_COLUMN in lowered:
                day_index = lowered.index(_ANALYTICS_DAY_COLUMN)
            if _ANALYTICS_HOUR_COLUMN in lowered:
                hour_index = lowered.index(_ANALYTICS_HOUR_COLUMN)
            # The metric is the first column that is neither dimension.
            for i, name in enumerate(lowered):
                if name not in (_ANALYTICS_DAY_COLUMN, _ANALYTICS_HOUR_COLUMN):
                    metric_index = i
                    break
        return day_index, hour_index, metric_index

    @staticmethod
    def _row_to_bucket(
        row: Any, day_index: int, hour_index: int, metric_index: int
    ) -> HourlyActivity | None:
        """Convert one result-table row to a valid :class:`HourlyActivity`, or ``None``.

        Returns ``None`` (dropping the row) when any column is missing, the date
        is unparseable, the hour is not in ``[0, 23]``, or the activity is
        unparseable or negative — so every returned bucket satisfies the
        ``day_of_week ∈ [0, 6]``, ``hour ∈ [0, 23]``, ``activity ≥ 0`` invariant
        (3.1).
        """
        max_index = max(day_index, hour_index, metric_index)
        if len(row) <= max_index:
            return None

        day_of_week = _date_to_weekday(row[day_index])
        if day_of_week is None:
            return None

        hour = _coerce_optional_int(row[hour_index])
        if hour is None or not 0 <= hour <= 23:
            return None

        activity = _coerce_activity(row[metric_index])
        if activity is None or activity < 0:
            return None

        return HourlyActivity(day_of_week=day_of_week, hour=hour, activity=activity)

    def _request(
        self,
        method: str,
        url: str,
        *,
        request_description: str,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """Perform exactly one request, mapping failures via classify_response.

        Transport-level failures (timeout, connection error/reset) and HTTP
        error statuses are both routed through :func:`classify_response`, which
        returns the single error-hierarchy member to raise (16.5). No retry or
        backoff happens here (16.5); the timeout is applied per the configured
        ``request_timeout_seconds`` (16.4).
        """
        try:
            response = self._transport.request(
                method,
                url,
                headers=dict(headers or {}),
                body=None,
                timeout_seconds=self._request_timeout_seconds,
            )
        except HttpTransportError as exc:  # includes HttpTimeoutError
            raise classify_response(request_description, transport_error=exc)

        if response.status >= 400:
            raise classify_response(request_description, response=response)
        return response

    @staticmethod
    def _parse_json(
        response: HttpResponse, *, request_description: str
    ) -> Mapping[str, Any]:
        """Decode a successful response body as a JSON object.

        A body that is not valid JSON, or whose top level is not an object, is a
        contract violation rather than a retryable condition, so it surfaces as a
        :class:`NonTransientError` naming the request.
        """
        try:
            data = json.loads(response.body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise NonTransientError(
                f"{request_description} returned an unparseable response body"
            ) from exc
        if not isinstance(data, dict):
            raise NonTransientError(
                f"{request_description} returned an unexpected response shape"
            )
        return data
