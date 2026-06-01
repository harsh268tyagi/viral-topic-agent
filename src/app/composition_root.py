"""The :class:`CompositionRoot` and its entry point (Requirement 14).

The design (``.kiro/specs/real-provider-integration/design.md`` ->
*CompositionRoot*) calls for a single factory that reads validated
:class:`~config.settings.Settings`, wires the real edge components into the
existing seams, builds a domain :class:`~domain.models.Configuration`, and runs
the :class:`~orchestration.automation_scheduler.AutomationScheduler`.

Lifecycle of :meth:`CompositionRoot.run` (mirrors the design's startup flow):

1. **Load + validate first (11.1, 11.2).** ``ConfigLoader.load`` assembles the
   ``Settings`` and ``ConfigLoader.validate`` checks every required value for
   each enabled component and every selected delivery destination -- *before*
   anything is constructed and before any external request is issued. If
   validation fails, every problem is reported by key (the
   :class:`~config.settings.ValidationReport` carries keys + expected-form
   descriptions only, so no :class:`~config.secrets.Secret` value can appear),
   the scheduler is never run, and no external request is made (11.2, 12.5).

2. **Construct the real components (14.1, 14.2).** With valid settings the root
   builds ``ResilientDataSource(YouTubeDataSource(...))`` (14.1), the
   :class:`~generation.llm_generation_provider.LLMGenerationProvider`, and one
   :class:`~delivery.deliverer.Deliverer` per *selected* delivery destination
   (14.2). Each construction step is named; if any required component fails to
   construct, the root aborts without running the scheduler, reports the failing
   component, and still aborts even when the reporting itself fails (14.7).

3. **Build the Configuration (14.3).** A domain
   :class:`~domain.models.Configuration` is built from the ``Settings`` carrying
   the authorized channels, the selected category, the monitored competitors,
   the schedule, and the delivery destinations (Property 18).

4. **Run the scheduler (14.4, 14.5, 14.6).** The
   :class:`AutomationScheduler` is run with the constructed
   :class:`ResilientDataSource`, the constructed deliverers, and the built
   ``Configuration`` (14.4). When the settings specify a *complete* schedule
   (both a recurrence interval and a run time) the run executes as a scheduled
   run (14.5); otherwise it executes only as a manual trigger (14.6).

Layer & dependency policy (Requirement 15): this module is stdlib-only and
introduces no third-party import of its own. The concrete vendor ports -- the
:class:`~infrastructure.http_transport.HttpTransport`, the
:class:`~generation.llm_client.LLMClient`, the
:class:`~delivery.smtp_transport.SmtpTransport`, and the
:class:`~infrastructure.clock.Clock` -- are injected, so every branch of the
root is exercisable against fakes with no real network access (16.3, 16.4) and
the entry point chooses the production transports.

Requirements traceability: 11.1, 11.2, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Mapping

from config.config_loader import ConfigLoader
from config.secrets import Secret, redact
from config.settings import EmailSettings, Settings, ValidationReport
from delivery.deliverer import Deliverer
from delivery.email_deliverer import EmailDeliverer
from delivery.notion_deliverer import NotionDeliverer
from delivery.slack_deliverer import SlackDeliverer
from delivery.smtp_transport import SmtpTransport, SmtplibSmtpTransport
from domain.models import Configuration, DeliveryDestination, RunSummary, Schedule
from generation.llm_client import LLMClient
from generation.llm_generation_provider import LLMGenerationProvider
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import Clock
from infrastructure.http_transport import HttpTransport
from infrastructure.keyword_metrics_provider import KeywordMetricsProvider
from infrastructure.resilient_data_source import ResilientDataSource, RetryPolicy
from infrastructure.template_performance_strategy import TemplatePerformanceStrategy
from infrastructure.youtube_data_source import YouTubeDataSource
from orchestration.automation_scheduler import AutomationScheduler

__all__ = [
    "DEFAULT_YOUTUBE_DATA_API_BASE_URL",
    "DEFAULT_YOUTUBE_ANALYTICS_BASE_URL",
    "StartupResult",
    "CompositionRoot",
    "Reporter",
    "SchedulerFactory",
    "SmtpTransportFactory",
]

logger = logging.getLogger(__name__)


# Production base URLs for the YouTube APIs (overridable for tests/staging).
DEFAULT_YOUTUBE_DATA_API_BASE_URL = "https://www.googleapis.com/youtube/v3"
DEFAULT_YOUTUBE_ANALYTICS_BASE_URL = "https://youtubeanalytics.googleapis.com/v2"


# A reporter records a startup failure (validation or construction). It is a
# single side-effecting callable so the entry point can route it to logs and a
# test can inject one that raises to exercise the nested-reporting-failure path
# (14.7). The message handed to it is already redacted (keys/types only).
Reporter = Callable[[str], None]

# Builds the AutomationScheduler from the constructed source + deliverers. A
# factory (rather than a ready scheduler) is required because the scheduler owns
# the source and deliverers, which only exist after construction succeeds.
SchedulerFactory = Callable[
    [ResilientDataSource, Mapping[DeliveryDestination, Deliverer]],
    AutomationScheduler,
]

# Builds the SmtpTransport for the email deliverer from the resolved
# EmailSettings (host/port/credentials live on those settings). Injected so a
# test supplies a FakeSmtpTransport and production supplies a real one.
SmtpTransportFactory = Callable[[EmailSettings], SmtpTransport]


# ---------------------------------------------------------------------------
# Internal signal: a required component failed to construct (14.7)
# ---------------------------------------------------------------------------


class _ComponentError(Exception):
    """A required component failed to construct (14.7).

    Carries the human-readable ``component`` name (for the failure report) and
    the originating ``cause`` so the root can abort without running the
    scheduler and report which component failed.
    """

    def __init__(self, component: str, cause: BaseException) -> None:
        super().__init__(component)
        self.component = component
        self.cause = cause


# ---------------------------------------------------------------------------
# Startup result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartupResult:
    """The outcome of one :meth:`CompositionRoot.run` invocation.

    ``started`` is ``True`` only when validation passed, every required
    component constructed, and the scheduler was run. On a validation failure
    ``validation_report`` carries the problems (keys only) and ``started`` is
    ``False`` (11.2). On a construction failure ``failed_component`` names the
    component that failed and ``started`` is ``False`` (14.7). On success
    ``run_summary`` is the scheduler's summary and ``manual`` records whether
    the run was a manual trigger (``True``) or a scheduled run (``False``)
    (14.5, 14.6).
    """

    started: bool
    validation_report: ValidationReport
    run_summary: RunSummary | None = None
    failed_component: str | None = None
    manual: bool = False


# ---------------------------------------------------------------------------
# CompositionRoot
# ---------------------------------------------------------------------------


class CompositionRoot:
    """Reads ``Settings``, wires the real components, and runs the scheduler.

    Constructor-injected with the :class:`ConfigLoader` and the vendor ports it
    needs -- an :class:`HttpTransport` (shared by the YouTube data source and the
    Slack/Notion deliverers), an :class:`LLMClient`, a :class:`Clock`, and a
    :class:`SmtpTransport` factory for the email deliverer -- so every branch is
    exercisable against fakes with no real network access (16.3, 16.4). The
    optional keyword/template seams, the scheduler factory, the failure
    reporter, the YouTube base URLs, the per-page ``max_items`` cap, and an
    explicit :class:`RetryPolicy` are all overridable; sensible production
    defaults are supplied.
    """

    def __init__(
        self,
        config_loader: ConfigLoader,
        *,
        http_transport: HttpTransport,
        llm_client: LLMClient,
        clock: Clock,
        smtp_transport_factory: SmtpTransportFactory | None = None,
        keyword_provider: KeywordMetricsProvider | None = None,
        template_strategy: TemplatePerformanceStrategy | None = None,
        scheduler_factory: SchedulerFactory | None = None,
        reporter: Reporter | None = None,
        data_api_base_url: str = DEFAULT_YOUTUBE_DATA_API_BASE_URL,
        analytics_base_url: str = DEFAULT_YOUTUBE_ANALYTICS_BASE_URL,
        max_items: int = 50,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._config_loader = config_loader
        self._http_transport = http_transport
        self._llm_client = llm_client
        self._clock = clock
        self._smtp_transport_factory = (
            smtp_transport_factory or _default_smtp_transport_factory
        )
        self._keyword_provider = keyword_provider
        self._template_strategy = template_strategy
        self._scheduler_factory = scheduler_factory or _default_scheduler_factory
        self._reporter = reporter or _default_reporter
        self._data_api_base_url = data_api_base_url
        self._analytics_base_url = analytics_base_url
        self._max_items = max_items
        self._retry_policy = retry_policy

    # ------------------------------------------------------------------
    # Entry point (Requirement 14)
    # ------------------------------------------------------------------

    def run(self) -> StartupResult:
        """Load + validate, wire the components, and run the scheduler.

        Returns a :class:`StartupResult` describing the outcome. Validation runs
        first and before any construction or external request; a failed
        validation reports every problem by key and never runs the scheduler
        (11.1, 11.2). A required component that fails to construct aborts the run
        and reports the failing component -- even when the reporting itself fails
        (14.7). Otherwise the scheduler is run with the constructed source,
        deliverers, and built :class:`Configuration`, as a scheduled run for a
        complete schedule or a manual trigger otherwise (14.4, 14.5, 14.6).
        """
        # 1. Load + validate before constructing anything or issuing any request
        #    (11.1). load() is total; validate() collects all problems (11.2).
        settings = self._config_loader.load()
        report = self._config_loader.validate(settings)
        if not report.ok:
            # 11.2/12.5: report all problems by key (secrets never appear), and
            # never run the scheduler / issue any external request.
            self._safe_report(_format_validation(report), settings)
            return StartupResult(started=False, validation_report=report)

        # 2. Construct the required components (14.1, 14.2). The first failure
        #    aborts the run and is reported, naming the failing component (14.7).
        try:
            source = self._construct(
                "ResilientDataSource", lambda: self._build_source(settings)
            )
            # 14.2: the LLM provider is a required component even though the
            # scheduled pipeline does not consume it directly; its construction
            # failure must abort the run.
            self._construct(
                "LLMGenerationProvider", lambda: self._build_provider(settings)
            )
            deliverers = self._construct(
                "Deliverers", lambda: self._build_deliverers(settings)
            )
            scheduler = self._construct(
                "AutomationScheduler",
                lambda: self._scheduler_factory(source, deliverers),
            )
        except _ComponentError as exc:
            # 14.7: prevent the scheduler from running, report the component.
            self._safe_report(
                _format_component(exc.component, exc.cause, _collect_secrets(settings)),
                settings,
            )
            return StartupResult(
                started=False,
                validation_report=report,
                failed_component=exc.component,
            )

        # 3. Build the Configuration from the Settings (14.3, Property 18).
        config = self._build_configuration(settings)

        # 4. Run the scheduler (14.4). A complete schedule runs as a scheduled
        #    run; otherwise the invocation is the manual trigger (14.5, 14.6).
        manual = not _schedule_is_complete(settings.schedule)
        summary = scheduler.run(config, self._clock, manual=manual)
        return StartupResult(
            started=True,
            validation_report=report,
            run_summary=summary,
            manual=manual,
        )

    # ------------------------------------------------------------------
    # Component construction (14.1, 14.2)
    # ------------------------------------------------------------------

    def _build_source(self, settings: Settings) -> ResilientDataSource:
        """Build ``ResilientDataSource(YouTubeDataSource(...))`` (14.1).

        Wires the YouTube data source over the injected transport, an
        :class:`AuthManager` (API key + OAuth selection, Req. 13), and the
        injected clock, then wraps it in the existing
        :class:`ResilientDataSource` so retry/backoff/timeout policy stays
        unchanged (16.5). The optional keyword/template seams are passed through.
        """
        auth = AuthManager(settings.auth, self._http_transport, self._clock)
        youtube = YouTubeDataSource(
            self._http_transport,
            auth,
            self._clock,
            api_base_url=self._data_api_base_url,
            request_timeout_seconds=settings.request_timeout_seconds,
            analytics_base_url=self._analytics_base_url,
            keyword_provider=self._keyword_provider,
            template_strategy=self._template_strategy,
            max_items=self._max_items,
        )
        policy = self._retry_policy or RetryPolicy(
            request_timeout_seconds=settings.request_timeout_seconds
        )
        return ResilientDataSource(youtube, policy, self._clock)

    def _build_provider(self, settings: Settings) -> LLMGenerationProvider:
        """Build the :class:`LLMGenerationProvider` over the injected client (14.2)."""
        return LLMGenerationProvider(
            self._llm_client, request_timeout_seconds=settings.llm_timeout_seconds
        )

    def _build_deliverers(
        self, settings: Settings
    ) -> dict[DeliveryDestination, Deliverer]:
        """Build one :class:`Deliverer` per *selected* delivery destination (14.2).

        Only destinations present in ``settings.delivery_destinations`` are
        built; an unselected destination contributes no deliverer (11.4). A
        selected destination whose settings group is absent is a construction
        failure (it should have been caught by validation) and is surfaced so the
        run aborts naming the component (14.7).
        """
        deliverers: dict[DeliveryDestination, Deliverer] = {}
        for destination in settings.delivery_destinations:
            if destination is DeliveryDestination.EMAIL:
                if settings.email is None:
                    raise ValueError("email destination selected but unconfigured")
                smtp = self._smtp_transport_factory(settings.email)
                deliverers[destination] = EmailDeliverer(smtp, settings.email)
            elif destination is DeliveryDestination.SLACK:
                if settings.slack is None:
                    raise ValueError("slack destination selected but unconfigured")
                deliverers[destination] = SlackDeliverer(
                    self._http_transport,
                    settings.slack,
                    timeout_seconds=settings.request_timeout_seconds,
                )
            elif destination is DeliveryDestination.NOTION:
                if settings.notion is None:
                    raise ValueError("notion destination selected but unconfigured")
                deliverers[destination] = NotionDeliverer(
                    self._http_transport,
                    settings.notion,
                    timeout_seconds=settings.request_timeout_seconds,
                )
        return deliverers

    def _build_configuration(self, settings: Settings) -> Configuration:
        """Build a :class:`Configuration` faithfully reflecting ``Settings`` (14.3).

        Carries the authorized channels, the selected category, the monitored
        competitors, the schedule, and the delivery destinations exactly as the
        validated ``Settings`` hold them (Property 18).
        """
        return Configuration(
            authorized_channels=settings.authorized_channels,
            selected_category=settings.selected_category,
            monitored_competitors=settings.monitored_competitors,
            schedule=settings.schedule,
            delivery_destinations=settings.delivery_destinations,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _construct(component: str, factory: Callable[[], object]) -> object:
        """Run ``factory``; on any error raise :class:`_ComponentError` (14.7)."""
        try:
            return factory()
        except Exception as exc:  # noqa: BLE001 - any construction error aborts the run
            raise _ComponentError(component, exc) from exc

    def _safe_report(self, message: str, settings: Settings) -> None:
        """Report a startup failure, tolerating a failing reporter (14.7).

        The scheduler must never run when a startup failure is reported, so a
        reporter that itself fails must not propagate: 14.7 requires the
        abort to hold "including when reporting the construction failure itself
        fails". The reporter's own failure is swallowed (best-effort fallback
        log) so the caller still receives the aborted :class:`StartupResult`.
        """
        try:
            self._reporter(message)
        except Exception:  # noqa: BLE001 - 14.7: reporting may itself fail
            # Best-effort fallback to the module logger; guarded so even a
            # failing logger cannot resurrect the (already aborted) run.
            try:
                logger.error("startup reporting failed while handling: %s", message)
            except Exception:  # noqa: BLE001 - nothing more we can safely do
                pass


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _schedule_is_complete(schedule: Schedule | None) -> bool:
    """A schedule is complete when it has both a recurrence interval and a time.

    Mirrors :meth:`AutomationScheduler.set_schedule`'s completeness rule (14.5):
    a schedule missing either field is not a recurring schedule, so the run is
    manual-only (14.6).
    """
    if schedule is None:
        return False
    return bool((schedule.recurrence_interval or "").strip()) and bool(
        (schedule.run_time or "").strip()
    )


def _collect_secrets(settings: Settings) -> tuple[Secret, ...]:
    """Gather every :class:`Secret` in ``settings`` for defensive redaction.

    Used to scrub a component-failure reason so that even a third-party error
    message embedding a credential cannot leak through the report (12.1, 12.3).
    """
    secrets: list[Secret] = [settings.auth.youtube_api_key]
    oauth = settings.auth.oauth
    if oauth is not None:
        secrets.append(oauth.client_secret)
        if oauth.refresh_token is not None:
            secrets.append(oauth.refresh_token)
        if oauth.access_token is not None:
            secrets.append(oauth.access_token)
    if settings.email is not None:
        secrets.append(settings.email.password)
    if settings.slack is not None:
        secrets.append(settings.slack.token)
    if settings.notion is not None:
        secrets.append(settings.notion.token)
    if settings.keyword_source is not None and settings.keyword_source.api_key is not None:
        secrets.append(settings.keyword_source.api_key)
    return tuple(secrets)


def _format_validation(report: ValidationReport) -> str:
    """Render a validation failure as a key-only message (11.2, 11.5, 12.5)."""
    problems = ", ".join(f"{p.key} ({p.issue})" for p in report.problems)
    return f"startup aborted: configuration validation failed: {problems}"


def _format_component(
    component: str, cause: BaseException, secrets: tuple[Secret, ...]
) -> str:
    """Render a construction failure, redacting any secret value (14.7, 12.3).

    Names the failing component and the error *type* only; the cause text is
    additionally passed through :func:`redact` for defence in depth so no
    credential value can surface in the report.
    """
    detail = redact(f"{type(cause).__name__}: {cause}", secrets)
    return f"startup aborted: failed to construct {component}: {detail}"


def _default_reporter(message: str) -> None:
    """Default failure reporter: record the (already redacted) message in logs."""
    logger.error("%s", message)


def _default_scheduler_factory(
    source: ResilientDataSource,
    deliverers: Mapping[DeliveryDestination, Deliverer],
) -> AutomationScheduler:
    """Build a real :class:`AutomationScheduler` over the constructed components."""
    return AutomationScheduler(source=source, deliverers=dict(deliverers))


def _default_smtp_transport_factory(settings: EmailSettings) -> SmtpTransport:
    """Build a production :class:`SmtplibSmtpTransport` from the email settings.

    The SMTP password is revealed only here, at the transport boundary where it
    must be transmitted (Requirement 12); it is never logged or placed in an
    error reason.
    """
    return SmtplibSmtpTransport(
        settings.host,
        settings.port,
        username=settings.username,
        password=settings.password.reveal(),
    )
