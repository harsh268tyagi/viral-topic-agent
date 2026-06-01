"""Self-contained demo runner for the Viral Topic Agent.

This is NOT part of the library; it's a small launcher so you can see the
end-to-end pipeline run without a real YouTube/LLM provider. It:

1. Loads settings from ``.env`` (a tiny stdlib parser — no third-party deps).
2. Builds a deterministic in-memory ``DataSource`` (a "dummy" provider).
3. Wires the real ``AutomationScheduler`` over a real ``ResilientDataSource``
   and ``RealClock``, with in-memory stub deliverers.
4. Runs one manual pipeline pass and prints the resulting ``RunSummary``.

Run it from the project root:

    python run_demo.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# The package layout puts the layers directly under ``src/`` (see pyproject's
# ``pythonpath = ["src"]`` for pytest). Mirror that for a standalone script.
PROJECT_ROOT = Path(__file__).resolve().parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from analysis.scoring import CategoryAggregate  # noqa: E402
from delivery import EmailDeliverer, NotionDeliverer, SlackDeliverer  # noqa: E402
from domain.models import (  # noqa: E402
    AudienceActivity,
    AuthorizedChannel,
    ChannelCategory,
    ChannelMetadata,
    Configuration,
    DeliveryDestination,
    HourlyActivity,
    KeywordMetric,
    Schedule,
    StepStatus,
    VideoStats,
    ViralTemplate,
)
from infrastructure.clock import RealClock  # noqa: E402
from infrastructure.resilient_data_source import (  # noqa: E402
    ResilientDataSource,
    RetryPolicy,
)
from orchestration.automation_scheduler import AutomationScheduler  # noqa: E402


# ---------------------------------------------------------------------------
# .env loading (stdlib only)
# ---------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """Parse a minimal ``KEY=VALUE`` ``.env`` file into a dict.

    Ignores blank lines and ``#`` comments, strips surrounding whitespace and
    optional quotes, and also exports values into ``os.environ`` so the rest of
    the process can read them the usual way.
    """
    settings: dict[str, str] = {}
    if not path.exists():
        return settings
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        settings[key] = value
        os.environ.setdefault(key, value)
    return settings


# ---------------------------------------------------------------------------
# Dummy in-memory data source (stands in for the YouTube Data API)
# ---------------------------------------------------------------------------


class DummyDataSource:
    """A deterministic, dependency-free ``DataSource`` for the demo.

    Returns internally-consistent canned data: a flat baseline of views with a
    single high-view "spike" video, so the run surfaces both a competitor spike
    (>= 3x) and an owned-channel outlier (>= 5x).
    """

    def __init__(
        self,
        *,
        category: ChannelCategory,
        baseline_views: int,
        spike_multiplier: int,
    ) -> None:
        self.category = category
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        view_counts = [baseline_views] * 5 + [baseline_views * spike_multiplier]
        self._videos = [
            VideoStats(
                video_id=f"v{i + 1}",
                view_count=vc,
                published_at=(base + timedelta(days=i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                format=None,
            )
            for i, vc in enumerate(view_counts)
        ]
        self._templates = tuple(
            ViralTemplate(
                template_id=f"tpl-{i}",
                name=f"Template {i}",
                category=category,
                observed_performance=float(1000 - i * 100),
            )
            for i in range(6)
        )

    def get_channel_metadata(self, channel_id: str) -> ChannelMetadata:
        return ChannelMetadata(
            channel_id=channel_id,
            title=f"Channel {channel_id}",
            subscriber_count=10_000,
            video_count=len(self._videos),
            detected_category=self.category,
        )

    def get_videos(
        self, channel_id: str | None = None, published_within_days: int | None = None
    ) -> list[VideoStats]:
        return list(self._videos)

    def get_audience_activity(
        self, channel_id: str | None = None, days: int = 7
    ) -> AudienceActivity:
        buckets = tuple(
            HourlyActivity(day_of_week=d, hour=h, activity=float((d * 24 + h) % 50))
            for d in range(7)
            for h in range(24)
        )
        return AudienceActivity(
            channel_id=channel_id, days_covered=max(days, 7), buckets=buckets
        )

    def get_keyword_metrics(
        self, category: ChannelCategory | None = None, max_keywords: int = 1000
    ) -> list[KeywordMetric]:
        return [
            KeywordMetric(
                keyword=f"kw-{i}",
                demand=float(i % 100),
                competition=float((i * 7) % 100),
            )
            for i in range(min(100, max_keywords))
        ]

    def get_template_performance(
        self, category: ChannelCategory | None = None, **_kwargs: object
    ) -> list[ViralTemplate]:
        return list(self._templates)


# ---------------------------------------------------------------------------
# Settings -> domain config
# ---------------------------------------------------------------------------


def _parse_category(value: str) -> ChannelCategory | None:
    value = value.strip().lower()
    if not value:
        return None
    try:
        return ChannelCategory(value)
    except ValueError:
        valid = ", ".join(c.value for c in ChannelCategory)
        raise SystemExit(f"Invalid SELECTED_CATEGORY={value!r}. Use one of: {valid}")


def _parse_destinations(value: str) -> tuple[DeliveryDestination, ...]:
    dests: list[DeliveryDestination] = []
    for token in value.split(","):
        token = token.strip().lower()
        if not token:
            continue
        try:
            dests.append(DeliveryDestination(token))
        except ValueError:
            valid = ", ".join(d.value for d in DeliveryDestination)
            raise SystemExit(
                f"Invalid delivery destination {token!r}. Use one of: {valid}"
            )
    return tuple(dests)


def build_configuration(env: dict[str, str]) -> Configuration:
    channel_id = env.get("OWNED_CHANNEL_ID", "demo-channel-1")
    competitors = tuple(
        c.strip() for c in env.get("COMPETITORS", "").split(",") if c.strip()
    )
    return Configuration(
        authorized_channels=(
            AuthorizedChannel(
                channel_id=channel_id,
                credentials_ref="demo-cred-ref",
                connected=True,
                credentials_expired=False,
            ),
        ),
        selected_category=_parse_category(env.get("SELECTED_CATEGORY", "")),
        monitored_competitors=competitors,
        schedule=Schedule(
            recurrence_interval=env.get("SCHEDULE_INTERVAL") or None,
            run_time=env.get("SCHEDULE_RUN_TIME") or None,
        ),
        delivery_destinations=_parse_destinations(
            env.get("DELIVERY_DESTINATIONS", "email")
        ),
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    env = load_env(PROJECT_ROOT / ".env")
    if not env:
        print("No .env found; using built-in defaults.\n")

    config = build_configuration(env)
    category = config.selected_category or ChannelCategory.GAMING

    baseline_views = int(env.get("DEMO_BASELINE_VIEWS", "100"))
    spike_multiplier = int(env.get("DEMO_SPIKE_MULTIPLIER", "10"))

    source = ResilientDataSource(
        DummyDataSource(
            category=category,
            baseline_views=baseline_views,
            spike_multiplier=spike_multiplier,
        ),
        RetryPolicy(),
        RealClock(),
    )

    deliverer_factories = {
        DeliveryDestination.EMAIL: EmailDeliverer,
        DeliveryDestination.SLACK: SlackDeliverer,
        DeliveryDestination.NOTION: NotionDeliverer,
    }
    deliverers = {
        dest: deliverer_factories[dest]() for dest in config.delivery_destinations
    }

    scheduler = AutomationScheduler(
        source=source,
        deliverers=deliverers,
        category_aggregate=CategoryAggregate(
            category=category,
            aggregate_performance=float(baseline_views),
        ),
    )

    print("Viral Topic Agent — demo run")
    print("=" * 48)
    print(f"Owned channel : {config.authorized_channels[0].channel_id}")
    print(f"Category      : {category.value}")
    print(f"Competitors   : {', '.join(config.monitored_competitors) or '(none)'}")
    print(
        f"Schedule      : {config.schedule.recurrence_interval} @ "
        f"{config.schedule.run_time}"
    )
    print(
        "Destinations  : "
        + (", ".join(d.value for d in config.delivery_destinations) or "(none)")
    )
    print("=" * 48)

    summary = scheduler.run(config, RealClock(), manual=True)

    print(f"\nRun started_at={summary.started_at} completed_at={summary.completed_at}")
    print(f"overlap_skipped={summary.overlap_skipped}\n")
    print("Pipeline steps:")
    icons = {
        StepStatus.SUCCEEDED: "[OK]  ",
        StepStatus.FAILED: "[FAIL]",
        StepStatus.SKIPPED: "[SKIP]",
    }
    for step in summary.steps:
        print(f"  {icons[step.status]} {step.step}  ->  {step.status.value}")

    print("\nDelivery results:")
    for dest, deliverer in deliverers.items():
        delivered = getattr(deliverer, "delivered", False)
        attempts = getattr(deliverer, "attempts", 0)
        print(f"  {dest.value:8} delivered={delivered} attempts={attempts}")

    all_ok = all(s.status is StepStatus.SUCCEEDED for s in summary.steps)
    print("\nResult:", "all steps succeeded" if all_ok else "some steps did not succeed")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
