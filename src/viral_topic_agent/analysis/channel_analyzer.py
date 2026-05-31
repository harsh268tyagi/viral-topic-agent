"""Owned-channel analysis (Requirement 2).

The :class:`ChannelAnalyzer` retrieves an Owned_Channel's metadata and published
video list (with per-video view counts) through :class:`ResilientDataSource` and
turns them into a derived :class:`~viral_topic_agent.models.ChannelProfile`.

Design reference (``.kiro/specs/viral-topic-agent/design.md`` -> Channel_Analyzer):

- Retrieve channel metadata, the published video list, and per-video view counts
  via the resilience layer (2.1). The per-video view counts ride along inside the
  retrieved :class:`~viral_topic_agent.models.VideoStats`, so the video-list call
  supplies both.
- Produce a :class:`ChannelProfile` carrying the detected category, subscriber
  count, total video count, and the ``Baseline_View_Count`` computed over the
  most-recent 30 videos via the shared
  :func:`~viral_topic_agent.baseline.compute_baseline_view_count` with ``cap=30``
  (2.3). The baseline confidence/unavailable markers for 0 and 1-4 video channels
  (2.2, 2.4, 2.7) are handled inside that shared function.
- When the Data_Source reports a partial failure but *some* data is retrievable,
  build the profile from what was retrieved and record the reported reason in
  ``partial_failure_reason`` (2.5).
- Only when *no* Owned_Channel data is retrievable at all, return a
  :class:`DataRetrievalError` identifying the channel and the failure reason (2.6).

The analyzer never raises for an expected degraded state: every external call is
made through ``ResilientDataSource.call`` which returns a ``Result``, so failures
are branched on as data.

Requirements traceability: 2.1, 2.3, 2.5, 2.6 (with 2.2/2.4/2.7 delegated to
``compute_baseline_view_count``).
"""

from __future__ import annotations

from dataclasses import dataclass

from viral_topic_agent.analysis.baseline import compute_baseline_view_count
from viral_topic_agent.infrastructure.datasource import DataOperation, DataRequest, DataSourceFailure
from viral_topic_agent.domain.models import ChannelMetadata, ChannelProfile, VideoStats
from viral_topic_agent.infrastructure.resilient_data_source import ResilientDataSource
from viral_topic_agent.infrastructure.result import Err, Ok, Result

__all__ = ["ChannelAnalyzer", "DataRetrievalError"]

# The owned-channel baseline is the median of the most-recent 30 videos (2.2).
_BASELINE_CAP = 30


@dataclass(frozen=True)
class DataRetrievalError:
    """A total data-retrieval failure for an Owned_Channel (2.6).

    Returned (inside an ``Err``) only when *no* Owned_Channel data could be
    retrieved at all. Identifies the affected channel and the failure reason so
    a caller can render the failure without re-deriving it.
    """

    channel_id: str
    reason: str


class ChannelAnalyzer:
    """Analyzes an Owned_Channel into a :class:`ChannelProfile` (Requirement 2)."""

    def analyze(
        self, channel_id: str, source: ResilientDataSource
    ) -> Result[ChannelProfile, DataRetrievalError]:
        """Retrieve and summarize ``channel_id`` into a profile.

        Retrieves the channel metadata and the published video list (each video
        carrying its own view count) through ``source`` (2.1), then builds a
        :class:`ChannelProfile` with the detected category, subscriber count,
        video count, and the most-recent-30 baseline (2.3).

        Args:
            channel_id: The Owned_Channel to analyze. Always echoed back, both on
                a successful profile and on a :class:`DataRetrievalError`, so the
                affected channel is identifiable (2.6).
            source: The resilient data-source handle. All retrieval flows through
                it, so transient/rate-limit/non-transient handling (Requirement
                16) is already applied and surfaced as ``Result`` values.

        Returns:
            ``Ok(ChannelProfile)`` when at least one of the two retrievals
            succeeds. When only one succeeds, the profile is built from the
            retrieved data and ``partial_failure_reason`` records the other
            request's failure reason (2.5). ``Err(DataRetrievalError)`` only when
            *both* retrievals fail, i.e. no data at all is retrievable (2.6).
        """
        metadata_result = source.call(
            DataRequest(
                operation=DataOperation.CHANNEL_METADATA,
                target=channel_id,
                params={"channel_id": channel_id},
            )
        )
        videos_result = source.call(
            DataRequest(
                operation=DataOperation.VIDEOS,
                target=channel_id,
                params={"channel_id": channel_id},
            )
        )

        metadata: ChannelMetadata | None = (
            metadata_result.unwrap() if metadata_result.is_ok() else None
        )
        videos: list[VideoStats] | None = (
            list(videos_result.unwrap()) if videos_result.is_ok() else None
        )

        # 2.6: neither request returned data -> total retrieval failure. The
        # reason combines whatever the two failures reported so the caller sees
        # why nothing came back.
        if metadata is None and videos is None:
            reason = self._combine_reasons(
                metadata_result.unwrap_err(), videos_result.unwrap_err()
            )
            return Err(DataRetrievalError(channel_id=channel_id, reason=reason))

        # 2.5: at most one request failed. Collect the failed request's reason so
        # the profile can record it while still being produced from retrieved
        # data. When both succeeded this stays None.
        partial_reasons: list[str] = []
        if metadata is None:
            partial_reasons.append(
                self._describe(metadata_result.unwrap_err())
            )
        if videos is None:
            partial_reasons.append(self._describe(videos_result.unwrap_err()))
        partial_failure_reason = "; ".join(partial_reasons) or None

        # The baseline is computed over whatever videos were retrieved. When the
        # video list itself failed, we have no view counts, so the baseline is
        # computed over an empty list -> UNAVAILABLE (consistent with 2.4).
        baseline = compute_baseline_view_count(videos or [], cap=_BASELINE_CAP)

        # 2.3: subscriber/video counts and detected category come from metadata
        # when available. When metadata failed but videos succeeded, fall back to
        # the retrieved data: an unknown subscriber count is reported as 0 and the
        # video count is the number of retrieved videos.
        if metadata is not None:
            detected_category = metadata.detected_category
            subscriber_count = metadata.subscriber_count
            video_count = metadata.video_count
        else:
            detected_category = None
            subscriber_count = 0
            video_count = len(videos or [])

        return Ok(
            ChannelProfile(
                channel_id=channel_id,
                detected_category=detected_category,
                subscriber_count=subscriber_count,
                video_count=video_count,
                baseline=baseline,
                partial_failure_reason=partial_failure_reason,
            )
        )

    @staticmethod
    def _describe(failure: DataSourceFailure) -> str:
        """Render a single failure as ``"<operation-ish target>: <reason>"``.

        ``DataSourceFailure`` already carries the human-readable ``target`` and
        ``reason``; we surface both so a partial-failure reason is actionable.
        """
        return f"{failure.target}: {failure.reason}"

    @classmethod
    def _combine_reasons(
        cls, metadata_failure: DataSourceFailure, videos_failure: DataSourceFailure
    ) -> str:
        """Combine the two retrieval failure reasons for a total-failure error."""
        return (
            f"metadata retrieval failed ({cls._describe(metadata_failure)}); "
            f"video retrieval failed ({cls._describe(videos_failure)})"
        )
