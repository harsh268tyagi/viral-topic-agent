"""Example / edge-case tests for the Competitor_Tracker (tasks 9.1, 9.5).

The universal Hypothesis properties live in
``test_competitor_tracker_add_properties.py`` (Property 11),
``test_competitor_tracker_spike_properties.py`` (Property 12), and
``test_competitor_tracker_isolation_properties.py`` (Property 13). This module
covers the concrete branches and the 50-competitor cap boundary:

- adding a not-yet-present competitor stores exactly that id (6.1);
- adding at 50 monitored competitors -> ``limit-reached`` naming the max (6.8);
- monitoring retrieves trailing-30-day videos and flags >= 3x spikes (6.2-6.5);
- an unavailable competitor is reported ``unavailable`` and others continue (6.7);
- a competitor with < 5 videos is ``insufficient-data`` with detection skipped (6.6).

Requirements exercised: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from infrastructure.clock import FakeClock
from analysis.competitor_tracker import CompetitorTracker
from infrastructure.datasource import DataSource, NonTransientError
from domain.models import Configuration, VideoStats
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_DATE = datetime(2024, 1, 1, tzinfo=timezone.utc)


def _config(competitors: tuple[str, ...] = ()) -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=competitors,
        schedule=None,
        delivery_destinations=(),
    )


def _videos_on_days(
    view_counts: list[int], prefix: str = "v"
) -> list[VideoStats]:
    return [
        VideoStats(
            video_id=f"{prefix}{i + 1}",
            view_count=vc,
            published_at=(_BASE_DATE + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            format=None,
        )
        for i, vc in enumerate(view_counts)
    ]


class _ScriptedSource:
    """Serves per-channel videos; raises for ids in ``unavailable``."""

    def __init__(
        self,
        videos_by_channel: dict[str, list[VideoStats]],
        unavailable: set[str] | None = None,
    ) -> None:
        self._videos_by_channel = videos_by_channel
        self._unavailable = unavailable or set()

    def get_videos(self, channel_id, published_within_days=None):
        if channel_id in self._unavailable:
            raise NonTransientError(f"channel {channel_id} not found")
        return list(self._videos_by_channel.get(channel_id, []))

    def get_channel_metadata(self, channel_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_audience_activity(self, channel_id, days):  # pragma: no cover - unused
        raise NotImplementedError

    def get_keyword_metrics(self, category, max_keywords):  # pragma: no cover
        raise NotImplementedError

    def get_template_performance(self, category):  # pragma: no cover - unused
        raise NotImplementedError


def _resilient(source: DataSource) -> ResilientDataSource:
    return ResilientDataSource(source, RetryPolicy(), FakeClock())


# ---------------------------------------------------------------------------
# add_competitor (6.1)
# ---------------------------------------------------------------------------


def test_add_new_competitor_stores_id():
    result = CompetitorTracker().add_competitor(_config(), "rival-1")
    assert result.added is True
    assert result.limit_reached is False
    assert result.config.monitored_competitors == ("rival-1",)


def test_add_existing_competitor_is_idempotent():
    config = _config(("rival-1",))
    result = CompetitorTracker().add_competitor(config, "rival-1")
    assert result.added is False
    assert result.limit_reached is False
    assert result.config.monitored_competitors == ("rival-1",)


def test_add_preserves_existing_competitors():
    config = _config(("a", "b"))
    result = CompetitorTracker().add_competitor(config, "c")
    assert result.config.monitored_competitors == ("a", "b", "c")


# ---------------------------------------------------------------------------
# 6.8: the 50-competitor cap
# ---------------------------------------------------------------------------


def test_adding_at_fifty_competitors_is_rejected_with_limit_reached():
    """Adding a new competitor at the 50-channel cap -> limit-reached (6.8)."""
    full = tuple(f"c{i}" for i in range(CompetitorTracker.MAX_COMPETITORS))
    assert len(full) == 50
    config = _config(full)

    result = CompetitorTracker().add_competitor(config, "one-too-many")

    # Rejected: nothing stored, limit-reached signalled, and the max is named.
    assert result.added is False
    assert result.limit_reached is True
    assert result.error is not None
    assert "50" in result.error
    assert "limit-reached" in result.error
    # The configuration is returned unchanged (the new id is not stored).
    assert result.config.monitored_competitors == full
    assert "one-too-many" not in result.config.monitored_competitors


def test_adding_existing_competitor_at_cap_is_still_idempotent():
    """Re-adding an already-monitored id at the cap is idempotent, not rejected."""
    full = tuple(f"c{i}" for i in range(CompetitorTracker.MAX_COMPETITORS))
    config = _config(full)

    result = CompetitorTracker().add_competitor(config, "c0")

    assert result.added is False
    assert result.limit_reached is False
    assert result.error is None
    assert result.config.monitored_competitors == full


def test_adding_below_cap_succeeds_up_to_the_limit():
    """A 49-competitor config still accepts one more (the 50th)."""
    almost_full = tuple(f"c{i}" for i in range(CompetitorTracker.MAX_COMPETITORS - 1))
    config = _config(almost_full)

    result = CompetitorTracker().add_competitor(config, "c49")

    assert result.added is True
    assert result.limit_reached is False
    assert len(result.config.monitored_competitors) == 50


# ---------------------------------------------------------------------------
# monitor: spike detection (6.2-6.5)
# ---------------------------------------------------------------------------


def test_monitor_flags_videos_at_or_above_3x_baseline():
    # Baseline (median of five 100s plus one 300) is 100; the 300-view video is
    # exactly 3.0x -> a spike (boundary inclusive). A 250-view video (2.5x) is not.
    videos = _videos_on_days([100, 100, 100, 100, 100, 300, 250])
    source = _ScriptedSource({"rival": videos})

    reports = CompetitorTracker().monitor(_config(("rival",)), _resilient(source))

    assert len(reports) == 1
    report = reports[0]
    assert report.status == "ok"
    assert report.baseline is not None
    assert report.baseline.value == 100.0

    spike_ids = {s.video_id for s in report.spikes}
    assert spike_ids == {"v6"}
    spike = report.spikes[0]
    assert spike.channel_id == "rival"
    assert spike.view_count == 300
    assert spike.spike_factor == 3.0


def test_monitor_does_not_flag_just_below_3x():
    # 299 / 100 = 2.99 -> not a spike (boundary is inclusive at 3.0).
    videos = _videos_on_days([100, 100, 100, 100, 100, 299])
    source = _ScriptedSource({"rival": videos})
    reports = CompetitorTracker().monitor(_config(("rival",)), _resilient(source))
    assert reports[0].spikes == ()


def test_monitor_ignores_zero_view_videos():
    videos = _videos_on_days([100, 100, 100, 100, 100, 0])
    source = _ScriptedSource({"rival": videos})
    reports = CompetitorTracker().monitor(_config(("rival",)), _resilient(source))
    assert reports[0].spikes == ()


# ---------------------------------------------------------------------------
# 6.6: insufficient data
# ---------------------------------------------------------------------------


def test_monitor_marks_fewer_than_five_videos_insufficient_data():
    videos = _videos_on_days([100, 1000, 100, 100])  # 4 videos, one huge
    source = _ScriptedSource({"rival": videos})
    reports = CompetitorTracker().monitor(_config(("rival",)), _resilient(source))
    report = reports[0]
    assert report.status == "insufficient-data"
    assert report.spikes == ()


# ---------------------------------------------------------------------------
# 6.7: unavailable competitor, others continue
# ---------------------------------------------------------------------------


def test_monitor_marks_unavailable_and_continues_with_others():
    healthy = _videos_on_days([100, 100, 100, 100, 100, 500], prefix="h")
    source = _ScriptedSource({"healthy": healthy}, unavailable={"down"})

    reports = CompetitorTracker().monitor(
        _config(("down", "healthy")), _resilient(source)
    )

    # Both competitors are reported, in order, despite the first being down.
    assert [r.channel_id for r in reports] == ["down", "healthy"]

    down_report = reports[0]
    assert down_report.status == "unavailable"
    assert down_report.baseline is None
    assert down_report.spikes == ()

    healthy_report = reports[1]
    assert healthy_report.status == "ok"
    assert {s.video_id for s in healthy_report.spikes} == {"h6"}


def test_monitor_empty_config_returns_no_reports():
    source = _ScriptedSource({})
    reports = CompetitorTracker().monitor(_config(), _resilient(source))
    assert reports == []
