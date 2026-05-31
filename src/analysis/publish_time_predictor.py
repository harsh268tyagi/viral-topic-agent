"""Publish-time prediction (Requirement 9).

The :class:`PublishTimePredictor` recommends the single best day-of-week and a
contiguous 1-3 hour publishing window for an Owned_Channel, based on the
channel's audience activity (falling back to the Channel_Category aggregate when
the owned-channel data is unavailable).

Design reference (``.kiro/specs/viral-topic-agent/design.md`` -> Publish_Time_Predictor):

- Retrieve at least the most-recent 7 days of audience activity through
  :class:`ResilientDataSource` (9.1). The resilience layer already bounds retries
  to 3 total attempts (Requirement 16), so the predictor delegates the retry to
  it and only branches on the returned ``Result``.
- When activity covering >= 7 days is available, recommend exactly one
  day-of-week (0..6) and exactly one contiguous window 1-3 hours long, expressed
  in the Creator's configured time zone (9.2), or UTC when none is configured
  (9.3).
- When retrieval *fails* (the source returns an ``Err`` after exhausting its 3
  attempts), return an :class:`AudienceDataRetrievalError` identifying the
  channel (9.4).
- When the owned-channel activity is *unavailable* (retrieval succeeded but the
  data does not cover 7 days / carries no buckets), derive the recommendation
  from the supplied Channel_Category aggregate activity and mark it
  ``LOW`` confidence (9.5).
- When both the owned-channel activity and the category aggregate are
  unavailable, return a :class:`NoDataError` identifying the channel and produce
  no recommendation (9.6).

Retrieval failure (9.4) vs. data unavailable (9.5)
--------------------------------------------------
These are distinct conditions and are handled differently:

- *Retrieval failure* is a mechanical failure of the data source (network /
  rate-limit / non-transient). It surfaces as ``Err(DataSourceFailure)`` from
  ``ResilientDataSource.call`` and maps to the audience-data-retrieval error.
- *Data unavailable* is a successful retrieval that simply does not carry usable
  data (fewer than 7 days covered, or no hourly buckets). This triggers the
  category-aggregate fallback.

Window selection
----------------
The hourly buckets are aggregated into a 7x24 grid of activity. For every day
and every contiguous window of 1, 2, or 3 hours that fits within the day (no
midnight wrap, so the recommended day-of-week is unambiguous), the total bucket
activity over the window is computed. The window with the greatest total
activity is chosen; ties are broken deterministically by the earliest day, then
the earliest start hour, then the shortest duration. The buckets are interpreted
as already being in the target time zone, so selecting the time zone is a
labelling concern (9.2 / 9.3) layered over the same deterministic computation.

Requirements traceability: 9.1, 9.2, 9.3, 9.4, 9.5, 9.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from infrastructure.datasource import DataOperation, DataRequest
from domain.models import AudienceActivity, Confidence, HourlyActivity, PublishRecommendation
from infrastructure.resilient_data_source import ResilientDataSource
from infrastructure.result import Err, Ok, Result

__all__ = [
    "PublishTimePredictor",
    "AudienceDataRetrievalError",
    "NoDataError",
    "PublishTimeError",
]

# Audience activity must cover at least the most-recent 7 days (9.1, 9.2).
_MIN_DAYS = 7
# Recommended window duration bounds, in hours (9.2).
_MIN_WINDOW_HOURS = 1
_MAX_WINDOW_HOURS = 3
_DAYS_PER_WEEK = 7
_HOURS_PER_DAY = 24


@dataclass(frozen=True)
class AudienceDataRetrievalError:
    """Audience-activity retrieval failed after all attempts (9.4).

    Returned (inside an ``Err``) when ``ResilientDataSource`` exhausts its
    bounded retries and reports a failure. Identifies the affected Owned_Channel
    and carries the underlying failure reason so a caller can render it without
    re-deriving it.
    """

    channel_id: str
    reason: str


@dataclass(frozen=True)
class NoDataError:
    """No usable activity for either the channel or its category (9.6).

    Returned (inside an ``Err``) when the owned-channel activity is unavailable
    *and* no usable Channel_Category aggregate activity was supplied, so no
    recommendation can be produced. Identifies the affected Owned_Channel.
    """

    channel_id: str
    reason: str


# The predict() error channel is either a retrieval failure (9.4) or a
# total no-data condition (9.6).
PublishTimeError = Union[AudienceDataRetrievalError, NoDataError]


def _is_available(activity: AudienceActivity | None) -> bool:
    """Whether ``activity`` is usable for producing a recommendation.

    Usable means it covers at least 7 days (9.1) and carries at least one hourly
    bucket to analyze. A ``None`` series, a series covering fewer than 7 days, or
    one with no buckets is treated as unavailable.
    """
    return (
        activity is not None
        and activity.days_covered >= _MIN_DAYS
        and len(activity.buckets) > 0
    )


def _pick_peak_window(buckets: tuple[HourlyActivity, ...]) -> tuple[int, int, int]:
    """Return ``(day_of_week, start_hour, duration_hours)`` for the peak window.

    Aggregates the buckets into a 7x24 activity grid and scans every contiguous
    1-3 hour window that fits within a single day, selecting the one with the
    greatest summed activity. Ties resolve deterministically to the earliest day,
    then the earliest start hour, then the shortest duration (achieved by scanning
    in ascending order and only replacing the incumbent on a strictly greater
    total).
    """
    grid = [[0.0] * _HOURS_PER_DAY for _ in range(_DAYS_PER_WEEK)]
    for bucket in buckets:
        if 0 <= bucket.day_of_week < _DAYS_PER_WEEK and 0 <= bucket.hour < _HOURS_PER_DAY:
            grid[bucket.day_of_week][bucket.hour] += bucket.activity

    # Seed with the always-valid earliest window so the result is total.
    best_total: float | None = None
    best: tuple[int, int, int] = (0, 0, _MIN_WINDOW_HOURS)

    for day in range(_DAYS_PER_WEEK):
        for start in range(_HOURS_PER_DAY):
            running = 0.0
            for duration in range(_MIN_WINDOW_HOURS, _MAX_WINDOW_HOURS + 1):
                end = start + duration - 1
                if end >= _HOURS_PER_DAY:
                    break  # window would wrap past midnight; stop extending
                running += grid[day][end]
                if best_total is None or running > best_total:
                    best_total = running
                    best = (day, start, duration)

    return best


def _resolve_timezone(tz: object | None) -> str:
    """Resolve the recommendation time-zone label (9.2 / 9.3).

    ``None`` yields ``"UTC"`` (9.3). A string is used verbatim. Anything else
    (e.g. a :class:`zoneinfo.ZoneInfo`) is reduced to its IANA key when it
    exposes one, otherwise to ``str(tz)``.
    """
    if tz is None:
        return "UTC"
    if isinstance(tz, str):
        return tz
    key = getattr(tz, "key", None)
    if isinstance(key, str) and key:
        return key
    return str(tz)


class PublishTimePredictor:
    """Recommends an Owned_Channel publish day + window (Requirement 9)."""

    def predict(
        self,
        channel_id: str,
        source: ResilientDataSource,
        tz: object | None,
        category_activity: AudienceActivity | None,
    ) -> Result[PublishRecommendation, PublishTimeError]:
        """Produce a publish-time recommendation for ``channel_id``.

        Args:
            channel_id: The Owned_Channel to recommend for. Always echoed back on
                an error so the affected channel is identifiable (9.4, 9.6).
            source: The resilient data-source handle. The audience-activity
                retrieval flows through it, so the bounded 3-attempt retry
                (9.4 / Requirement 16) is already applied and surfaced as a
                ``Result``.
            tz: The Creator's configured time zone (an IANA name string or a
                ``ZoneInfo``), or ``None`` to express the recommendation in UTC
                (9.3).
            category_activity: The Channel_Category aggregate activity used as a
                low-confidence fallback when the owned-channel activity is
                unavailable (9.5). ``None`` when no aggregate is available.

        Returns:
            ``Ok(PublishRecommendation)`` with ``NORMAL`` confidence when the
            owned-channel activity is available (9.2), or ``LOW`` confidence when
            derived from the category aggregate (9.5). ``Err(AudienceDataRetrievalError)``
            when retrieval fails after all attempts (9.4). ``Err(NoDataError)``
            when neither source has usable activity (9.6).
        """
        timezone_label = _resolve_timezone(tz)

        # 9.1: retrieve >= 7 days of audience activity through the resilience
        # layer, which already bounds the retry to 3 total attempts (9.4).
        activity_result = source.call(
            DataRequest(
                operation=DataOperation.AUDIENCE_ACTIVITY,
                target=channel_id,
                params={"channel_id": channel_id, "days": _MIN_DAYS},
            )
        )

        # 9.4: retrieval failed (mechanical failure) -> audience-data-retrieval
        # error identifying the channel. Distinct from "data unavailable" below.
        if activity_result.is_err():
            failure = activity_result.unwrap_err()
            return Err(
                AudienceDataRetrievalError(
                    channel_id=channel_id,
                    reason=f"{failure.reason} (after {failure.attempts} attempt(s))",
                )
            )

        owned_activity: AudienceActivity | None = activity_result.unwrap()

        # 9.2 / 9.3: owned-channel activity is available -> recommend from it
        # with NORMAL confidence, labelled with the resolved time zone.
        if _is_available(owned_activity):
            assert owned_activity is not None  # narrowed by _is_available
            return Ok(
                self._recommend(owned_activity, timezone_label, Confidence.NORMAL)
            )

        # 9.5: owned-channel activity unavailable -> fall back to the category
        # aggregate, marked LOW confidence.
        if _is_available(category_activity):
            assert category_activity is not None  # narrowed by _is_available
            return Ok(
                self._recommend(category_activity, timezone_label, Confidence.LOW)
            )

        # 9.6: neither source has usable activity -> no-data error, no recommendation.
        return Err(
            NoDataError(
                channel_id=channel_id,
                reason=(
                    "no audience activity available for the channel or its "
                    "category aggregate"
                ),
            )
        )

    @staticmethod
    def _recommend(
        activity: AudienceActivity, timezone_label: str, confidence: Confidence
    ) -> PublishRecommendation:
        """Build a :class:`PublishRecommendation` from a usable activity series."""
        day, start_hour, duration = _pick_peak_window(activity.buckets)
        return PublishRecommendation(
            day_of_week=day,
            window_start_hour=start_hour,
            window_duration_hours=duration,
            timezone=timezone_label,
            confidence=confidence,
        )
