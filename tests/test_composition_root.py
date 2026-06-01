"""Unit tests for CompositionRoot wiring (task 11.4).

These are example-based unit tests (the *Configuration*-faithfulness property
lives in ``test_composition_configuration_properties.py`` (task 11.3); this
module does not duplicate it). They pin the concrete wiring behaviour of
``src/app/composition_root.py`` (``CompositionRoot.run``) against injected
fakes, with **no real network access** (16.3, 16.4):

- (14.1) the root builds ``ResilientDataSource(YouTubeDataSource(...))``;
- (14.2) it builds exactly one ``Deliverer`` per *selected* delivery
  destination and none for an unselected one;
- (14.4) it runs the scheduler with the constructed source + deliverers and a
  ``Configuration`` built from the validated ``Settings``;
- (14.5) a *complete* schedule runs as a scheduled run (``manual=False``);
- (14.6) an incomplete or absent schedule runs manual-only (``manual=True``);
- (14.7) a required component that fails to construct aborts the run without
  running the scheduler and reports the failing component -- including when the
  reporting itself raises (nested reporting failure still aborts).

All collaborators are injected: a stub ``ConfigLoader`` returning a chosen
``Settings`` + ``ValidationReport``, the edge fakes (``FakeHttpTransport``,
``FakeSmtpTransport``, ``SpyLLMClient``, ``FakeClock``), a recording
``scheduler_factory``/scheduler, and an injectable reporter.
"""

from __future__ import annotations

import pytest

from app.composition_root import CompositionRoot, StartupResult
from config.secrets import CredentialReference, Secret
from config.settings import (
    AuthSettings,
    EmailSettings,
    NotionSettings,
    SlackSettings,
    Settings,
    ValidationReport,
)
from delivery.email_deliverer import EmailDeliverer
from delivery.notion_deliverer import NotionDeliverer
from delivery.slack_deliverer import SlackDeliverer
from domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    Configuration,
    DeliveryDestination,
    RunSummary,
    Schedule,
)
from infrastructure.clock import FakeClock
from infrastructure.resilient_data_source import ResilientDataSource
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport, FakeSmtpTransport, SpyLLMClient


EMAIL = DeliveryDestination.EMAIL
SLACK = DeliveryDestination.SLACK
NOTION = DeliveryDestination.NOTION


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _StubConfigLoader:
    """A stub ``ConfigLoader`` returning a chosen ``Settings`` + report.

    ``CompositionRoot`` consumes a config loader only through ``load()`` and
    ``validate(settings)``, so this lightweight stub structurally satisfies what
    the root needs without touching the environment or file system.
    """

    def __init__(self, settings: Settings, report: ValidationReport) -> None:
        self._settings = settings
        self._report = report
        self.load_calls = 0
        self.validate_calls = 0

    def load(self) -> Settings:
        self.load_calls += 1
        return self._settings

    def validate(self, settings: Settings) -> ValidationReport:
        self.validate_calls += 1
        return self._report


_SUMMARY = RunSummary(steps=(), started_at="0.0", completed_at="0.0")


class _RecordingScheduler:
    """A fake scheduler that records each ``run`` and returns a fixed summary."""

    def __init__(self) -> None:
        # Each entry is (config, clock, manual).
        self.run_calls: list[tuple[Configuration, object, bool]] = []

    def run(self, config: Configuration, clock: object, *, manual: bool = False) -> RunSummary:
        self.run_calls.append((config, clock, manual))
        return _SUMMARY


class _RecordingSchedulerFactory:
    """A scheduler factory that captures the constructed source + deliverers."""

    def __init__(self) -> None:
        self.scheduler = _RecordingScheduler()
        # Each entry is (source, deliverers).
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, source: object, deliverers: dict) -> _RecordingScheduler:
        self.calls.append((source, dict(deliverers)))
        return self.scheduler


# ---------------------------------------------------------------------------
# Settings / root builders
# ---------------------------------------------------------------------------


def _secret(name: str, value: str = "value") -> Secret:
    return Secret(value, CredentialReference(name))


def _make_settings(
    *,
    destinations: tuple[DeliveryDestination, ...],
    schedule: Schedule | None,
    authorized_channels: tuple[AuthorizedChannel, ...] = (),
    selected_category: ChannelCategory | None = None,
    competitors: tuple[str, ...] = (),
) -> Settings:
    """Build a validated-shaped ``Settings`` for the given selection/schedule."""
    email = (
        EmailSettings(
            host="smtp.example.com",
            port=587,
            username="smtp-user",
            password=_secret("smtp_password", "smtp-pw"),
            sender="agent@example.com",
            recipient="creator@example.com",
        )
        if EMAIL in destinations
        else None
    )
    slack = (
        SlackSettings(
            token=_secret("slack_token", "slack-tok"),
            channel="#digests",
            api_base_url="https://slack.test/api",
        )
        if SLACK in destinations
        else None
    )
    notion = (
        NotionSettings(
            token=_secret("notion_token", "notion-tok"),
            database_id="db-123",
            api_version="2022-06-28",
            api_base_url="https://notion.test",
        )
        if NOTION in destinations
        else None
    )
    return Settings(
        auth=AuthSettings(youtube_api_key=_secret("youtube_api_key", "yt-key")),
        llm_timeout_seconds=60.0,
        request_timeout_seconds=30.0,
        email=email,
        slack=slack,
        notion=notion,
        authorized_channels=authorized_channels,
        selected_category=selected_category,
        monitored_competitors=competitors,
        schedule=schedule,
        delivery_destinations=destinations,
    )


def _make_root(
    settings: Settings,
    *,
    report: ValidationReport | None = None,
    scheduler_factory=None,
    reporter=None,
    smtp_transport_factory=None,
    clock: FakeClock | None = None,
) -> CompositionRoot:
    """Wire a ``CompositionRoot`` over injected fakes (no real network)."""
    loader = _StubConfigLoader(settings, report or ValidationReport())
    return CompositionRoot(
        loader,
        http_transport=FakeHttpTransport(),
        llm_client=SpyLLMClient(),
        clock=clock or FakeClock(),
        # Build email transports as fakes so no SMTP connection is ever opened.
        smtp_transport_factory=smtp_transport_factory or (lambda s: FakeSmtpTransport()),
        scheduler_factory=scheduler_factory,
        reporter=reporter,
    )


def _complete_schedule() -> Schedule:
    return Schedule(recurrence_interval="daily", run_time="08:00")


# ---------------------------------------------------------------------------
# 14.1 — ResilientDataSource wrapping YouTubeDataSource
# ---------------------------------------------------------------------------


def test_builds_resilient_data_source_wrapping_youtube_data_source():
    settings = _make_settings(destinations=(EMAIL,), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    result = root.run()

    assert result.started is True
    # The scheduler is built from exactly one constructed source.
    assert len(factory.calls) == 1
    source, _deliverers = factory.calls[0]
    assert isinstance(source, ResilientDataSource)
    assert isinstance(source.inner, YouTubeDataSource)


# ---------------------------------------------------------------------------
# 14.2 — one deliverer per selected destination, none for unselected
# ---------------------------------------------------------------------------


def test_one_deliverer_per_selected_destination_and_none_for_unselected():
    # Select email + slack; leave notion unselected.
    settings = _make_settings(destinations=(EMAIL, SLACK), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    root.run()

    _source, deliverers = factory.calls[0]
    assert set(deliverers.keys()) == {EMAIL, SLACK}
    assert isinstance(deliverers[EMAIL], EmailDeliverer)
    assert isinstance(deliverers[SLACK], SlackDeliverer)
    # The unselected destination contributes no deliverer.
    assert NOTION not in deliverers


def test_builds_a_deliverer_for_every_selected_destination():
    settings = _make_settings(
        destinations=(EMAIL, SLACK, NOTION), schedule=_complete_schedule()
    )
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    root.run()

    _source, deliverers = factory.calls[0]
    assert set(deliverers.keys()) == {EMAIL, SLACK, NOTION}
    assert isinstance(deliverers[NOTION], NotionDeliverer)


def test_no_deliverers_when_no_destination_is_selected():
    settings = _make_settings(destinations=(), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    root.run()

    _source, deliverers = factory.calls[0]
    assert deliverers == {}


# ---------------------------------------------------------------------------
# 14.4 — scheduler runs with constructed components + built Configuration
# ---------------------------------------------------------------------------


def test_scheduler_runs_with_constructed_components_and_built_configuration():
    channels = (
        AuthorizedChannel(
            channel_id="UC_owned", credentials_ref="youtube_api_key", connected=True
        ),
    )
    settings = _make_settings(
        destinations=(EMAIL,),
        schedule=_complete_schedule(),
        authorized_channels=channels,
        selected_category=ChannelCategory.GAMING,
        competitors=("UC_a", "UC_b"),
    )
    factory = _RecordingSchedulerFactory()
    clock = FakeClock()
    root = _make_root(settings, scheduler_factory=factory, clock=clock)

    result = root.run()

    # The constructed scheduler ran exactly once.
    assert len(factory.scheduler.run_calls) == 1
    config, run_clock, _manual = factory.scheduler.run_calls[0]

    # It ran with the injected clock and a Configuration built from the Settings.
    assert run_clock is clock
    assert config == Configuration(
        authorized_channels=channels,
        selected_category=ChannelCategory.GAMING,
        monitored_competitors=("UC_a", "UC_b"),
        schedule=settings.schedule,
        delivery_destinations=(EMAIL,),
    )
    # The summary the scheduler returned is surfaced on the result.
    assert result.started is True
    assert result.run_summary is _SUMMARY


# ---------------------------------------------------------------------------
# 14.5 — a complete schedule runs as a scheduled (non-manual) run
# ---------------------------------------------------------------------------


def test_complete_schedule_runs_as_scheduled_not_manual():
    settings = _make_settings(destinations=(EMAIL,), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    result = root.run()

    assert result.manual is False
    _config, _clock, manual = factory.scheduler.run_calls[0]
    assert manual is False


# ---------------------------------------------------------------------------
# 14.6 — an incomplete or absent schedule runs manual-only
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "schedule",
    [
        None,
        Schedule(recurrence_interval="daily", run_time=None),
        Schedule(recurrence_interval=None, run_time="08:00"),
        Schedule(recurrence_interval="", run_time="08:00"),
        Schedule(recurrence_interval="daily", run_time="   "),
    ],
    ids=["absent", "no-run-time", "no-interval", "blank-interval", "blank-run-time"],
)
def test_incomplete_or_absent_schedule_runs_manual_only(schedule):
    settings = _make_settings(destinations=(EMAIL,), schedule=schedule)
    factory = _RecordingSchedulerFactory()
    root = _make_root(settings, scheduler_factory=factory)

    result = root.run()

    assert result.manual is True
    _config, _clock, manual = factory.scheduler.run_calls[0]
    assert manual is True


# ---------------------------------------------------------------------------
# 14.7 — a construction failure aborts without running the scheduler
# ---------------------------------------------------------------------------


def _boom_smtp_factory(_settings) -> FakeSmtpTransport:
    """An SMTP factory that fails to construct a transport (drives 14.7)."""
    raise RuntimeError("smtp construction failed")


def test_construction_failure_aborts_without_running_scheduler_and_reports_component():
    # Email is selected, so building its deliverer requires the SMTP transport
    # factory, which is rigged to fail -> the "Deliverers" component fails.
    settings = _make_settings(destinations=(EMAIL,), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()
    reported: list[str] = []
    root = _make_root(
        settings,
        scheduler_factory=factory,
        reporter=reported.append,
        smtp_transport_factory=_boom_smtp_factory,
    )

    result = root.run()

    # The run aborted, naming the failing component.
    assert isinstance(result, StartupResult)
    assert result.started is False
    assert result.failed_component == "Deliverers"
    assert result.run_summary is None

    # The scheduler was never constructed nor run.
    assert factory.calls == []
    assert factory.scheduler.run_calls == []

    # The failure was reported, identifying the component (and no secret leaks).
    assert len(reported) == 1
    assert "Deliverers" in reported[0]
    assert "smtp-pw" not in reported[0]


def test_construction_failure_aborts_even_when_the_reporter_raises():
    # The nested-reporting-failure path: the reporter itself raises, yet the run
    # must still abort cleanly (no exception escapes) without running scheduler.
    settings = _make_settings(destinations=(EMAIL,), schedule=_complete_schedule())
    factory = _RecordingSchedulerFactory()

    def _boom_reporter(message: str) -> None:
        raise RuntimeError("reporting failed")

    root = _make_root(
        settings,
        scheduler_factory=factory,
        reporter=_boom_reporter,
        smtp_transport_factory=_boom_smtp_factory,
    )

    # run() must not propagate the reporter's failure.
    result = root.run()

    assert result.started is False
    assert result.failed_component == "Deliverers"
    assert factory.scheduler.run_calls == []
