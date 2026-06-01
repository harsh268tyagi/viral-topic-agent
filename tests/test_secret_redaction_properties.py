"""Hypothesis property test for secret redaction across all outputs (task 11.5).

This module hosts the single universal correctness property for the feature's
secret-handling guarantee (design.md -> *Property 17: No secret value appears in
any rendered output*). It is the consolidated redaction property: every
per-output redaction criterion (error reasons, generation errors, delivery
errors, validation reports, startup summaries, and auth log/reasons) folds into
this one property rather than a per-criterion test.

Property 17: *For any* secret-bearing operation -- a raised ``DataSourceError``,
``GenerationError``, or ``DeliveryError`` reason; a configuration validation
report; a startup or run summary; or a token acquisition/refresh log entry --
the rendered output SHALL contain no secret value and SHALL use the
corresponding ``CredentialReference`` in its place.

Validates: Requirements 2.9, 6.7, 7.6, 8.7, 9.6, 11.5, 12.1, 12.2, 12.3, 13.6

How the property is exercised
-----------------------------
Each example draws a single distinctive, opaque secret *value* (shaped like a
credential token so its presence/absence in an output is unambiguous, matching
the realistic input space of a credential) and a ``DigestReport`` (so rendering
varies). The test then drives one secret-bearing operation per output channel,
embedding the value wherever a secret could plausibly enter the text:

- ``DataSourceError`` reasons via :func:`classify_response` (2.9), embedding the
  value in both the request description and a transport-error message.
- ``GenerationError`` reasons via :class:`LLMGenerationProvider` (6.7), with the
  value embedded in the underlying LLM client failure.
- ``DeliveryError`` reasons via the real email / Slack / Notion deliverers
  (7.6, 8.7, 9.6), with the value embedded in the underlying transport failure.
- A configuration ``ValidationReport`` via :meth:`ConfigLoader.validate` (11.5).
- A startup summary via :meth:`CompositionRoot.run` -- both the
  validation-failure summary and the component-failure summary (12.1, 12.2,
  12.3).
- ``AuthManager`` re-authorization / invalid-key reasons and its log entries
  (13.6).

Every rendered output is collected and asserted to (a) contain no occurrence of
the secret value, and (b) where the value was embedded in free-form text,
render the redacted ``<Secret {reference}>`` placeholder in its place. All work
runs against injected fakes (``FakeHttpTransport``, ``FakeSmtpTransport``, a spy
``LLMClient``, ``FakeClock``) with no real network access (16.3, 16.4).
"""

from __future__ import annotations

import logging

from hypothesis import given, settings
from hypothesis import strategies as st

from app.composition_root import CompositionRoot
from config.config_loader import (
    KEY_DELIVERY_DESTINATIONS,
    KEY_EMAIL_RECIPIENT,
    KEY_EMAIL_SENDER,
    KEY_LLM_TIMEOUT_SECONDS,
    KEY_REQUEST_TIMEOUT_SECONDS,
    KEY_SMTP_HOST,
    KEY_SMTP_PASSWORD,
    KEY_SMTP_PORT,
    KEY_SMTP_USERNAME,
    KEY_YOUTUBE_API_KEY,
    ConfigLoader,
)
from config.secrets import CredentialReference, Secret
from config.settings import (
    AuthSettings,
    EmailSettings,
    NotionSettings,
    OAuthCredentials,
    SlackSettings,
)
from config.sources import OverridesSource
from delivery.deliverer import DeliveryError
from delivery.digest_service import DigestService
from delivery.email_deliverer import EmailDeliverer
from delivery.notion_deliverer import NotionDeliverer
from delivery.slack_deliverer import SlackDeliverer
from delivery.smtp_transport import SmtpTransportError
from domain.models import (
    ChannelCategory,
    CompetitorSpike,
    Confidence,
    ContentIdea,
    DigestReport,
    Outlier,
    ScoredIdea,
    TimeWindow,
    ViralTemplate,
)
from generation.llm_client import LLMClientError
from generation.llm_generation_provider import LLMGenerationProvider
from generation.provider import GenerationError
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.datasource import NonTransientError
from infrastructure.http_transport import (
    HttpResponse,
    HttpTransportError,
)
from infrastructure.youtube_error_mapping import classify_response
from tests.edge_fakes import FakeHttpTransport, FakeSmtpTransport, SpyLLMClient

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

# A distinctive, opaque credential-shaped token. Lower-case alnum with a fixed
# "tok-...-xyz" frame makes it impossible to confuse with the documented
# (upper-case) configuration keys, the fixed issue phrases, or the non-secret
# identifiers the outputs legitimately carry -- so "the value does not appear"
# is an unambiguous signal that redaction held.
_secret_values = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789",
    min_size=4,
    max_size=20,
).map(lambda s: f"tok-{s}-xyz")

_counts = st.integers(min_value=0, max_value=4)

_TEMPLATE = ViralTemplate(
    template_id="t0",
    name="tier-list ranking",
    category=ChannelCategory.GAMING,
    observed_performance=1000.0,
)


def _scored_idea(idea_id: str) -> ScoredIdea:
    idea = ContentIdea(
        idea_id=idea_id,
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(_TEMPLATE,),
        observed_metric_value=5000.0,
    )
    return ScoredIdea(idea=idea, score=50, confidence=Confidence.NORMAL)


@st.composite
def _digest_reports(draw: st.DrawFn) -> DigestReport:
    """A valid three-section report with independently empty/populated sections."""
    scored = [_scored_idea(f"i{i}") for i in range(draw(_counts))]
    spikes = [
        CompetitorSpike(
            channel_id=f"comp-{i}", video_id=f"v{i}", view_count=9000, spike_factor=3.5
        )
        for i in range(draw(_counts))
    ]
    outliers = [
        Outlier(video_id=f"o{i}", view_count=50000, outlier_factor=6.0)
        for i in range(draw(_counts))
    ]
    return DigestService().compile(scored, spikes, outliers)


# A content idea whose identifier carries no secret, used to drive the LLM
# generation failure branch.
_IDEA = ContentIdea(
    idea_id="idea-0",
    title_concept="concept",
    rationale="rationale",
    time_window=TimeWindow.WEEKLY,
    category=ChannelCategory.GAMING,
    templates=(_TEMPLATE,),
    observed_metric_value=1000.0,
)


# ---------------------------------------------------------------------------
# Settings builders (every secret carries the generated value so a leak would
# be trivially findable in any output).
# ---------------------------------------------------------------------------


def _email_settings(value: str) -> EmailSettings:
    return EmailSettings(
        host="smtp.example.com",
        port=587,
        username="agent@example.com",
        password=Secret(value, CredentialReference("smtp_password")),
        sender="agent@example.com",
        recipient="creator@example.com",
    )


def _slack_settings(value: str) -> SlackSettings:
    return SlackSettings(
        token=Secret(value, CredentialReference("slack_token")),
        channel="#digests",
        api_base_url="https://slack.example.com/api",
    )


def _notion_settings(value: str) -> NotionSettings:
    return NotionSettings(
        token=Secret(value, CredentialReference("notion_token")),
        database_id="db-1",
        api_version="2022-06-28",
        api_base_url="https://notion.example.com/v1",
    )


def _valid_overrides(value: str) -> dict[str, str]:
    """A complete, valid configuration (email selected) with secrets = value."""
    return {
        KEY_YOUTUBE_API_KEY: value,
        KEY_LLM_TIMEOUT_SECONDS: "30",
        KEY_REQUEST_TIMEOUT_SECONDS: "30",
        KEY_DELIVERY_DESTINATIONS: "email",
        KEY_SMTP_HOST: "smtp.example.com",
        KEY_SMTP_PORT: "587",
        KEY_SMTP_USERNAME: "agent@example.com",
        KEY_SMTP_PASSWORD: value,
        KEY_EMAIL_SENDER: "agent@example.com",
        KEY_EMAIL_RECIPIENT: "creator@example.com",
    }


def _invalid_overrides(value: str) -> dict[str, str]:
    """A configuration that fails validation while still carrying secret values.

    The public API key (a secret) carries the value but is valid; a non-secret
    timeout is malformed and the SMTP password (a secret) is missing for the
    selected email destination, so the report carries problems identified by
    *key* only -- never by value (11.5).
    """
    return {
        KEY_YOUTUBE_API_KEY: value,
        KEY_LLM_TIMEOUT_SECONDS: "0",  # malformed: not a positive number
        KEY_REQUEST_TIMEOUT_SECONDS: "30",
        KEY_DELIVERY_DESTINATIONS: "email",
        KEY_SMTP_HOST: "smtp.example.com",
        KEY_SMTP_PORT: "587",
        KEY_SMTP_USERNAME: "agent@example.com",
        KEY_SMTP_PASSWORD: "",  # missing required secret -> reported by key
        KEY_EMAIL_SENDER: "agent@example.com",
        KEY_EMAIL_RECIPIENT: "creator@example.com",
    }


def _composition_root(loader: ConfigLoader, reporter, *, smtp_factory=None):
    """A ``CompositionRoot`` wired with injected fakes (no real network)."""
    return CompositionRoot(
        loader,
        http_transport=FakeHttpTransport(),
        llm_client=SpyLLMClient(),
        clock=FakeClock(),
        smtp_transport_factory=smtp_factory,
        reporter=reporter,
    )


# ---------------------------------------------------------------------------
# Property 17
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 17: No secret value appears in any rendered output
# Validates: Requirements 2.9, 6.7, 7.6, 8.7, 9.6, 11.5, 12.1, 12.2, 12.3, 13.6
@settings(max_examples=100)
@given(value=_secret_values, report=_digest_reports())
def test_no_secret_value_appears_in_any_rendered_output(
    value: str, report: DigestReport
) -> None:
    """For any secret value, no secret-bearing operation's rendered output leaks it.

    Each output is asserted to contain no occurrence of the secret value, and --
    where the value was embedded in free-form text -- to render the redacted
    ``<Secret {reference}>`` placeholder in its place.

    Validates: Requirements 2.9, 6.7, 7.6, 8.7, 9.6, 11.5, 12.1, 12.2, 12.3, 13.6
    """
    # Each entry is (label, rendered_text, required_placeholder | None). When the
    # placeholder is None we only require the value to be absent (no place where
    # a secret would otherwise have appeared); otherwise the placeholder must
    # appear where the value was scrubbed.
    outputs: list[tuple[str, str, str | None]] = []

    # 1. DataSourceError reasons (2.9). The value is embedded in the request
    #    description and in a transport-error message; both must redact.
    yt_secret = Secret(value, CredentialReference("youtube_api_key"))
    http_error = classify_response(
        f"get_channel_metadata for channel {value}",
        response=HttpResponse(status=400),
        secrets=[yt_secret],
    )
    outputs.append(("datasource-http", http_error.reason, "<Secret youtube_api_key>"))

    conn_error = classify_response(
        "get_videos for channel UC-1",
        transport_error=HttpTransportError(f"connection reset using {value}"),
        secrets=[yt_secret],
    )
    outputs.append(("datasource-conn", conn_error.reason, "<Secret youtube_api_key>"))

    # 2. GenerationError reason (6.7). The value is embedded in the LLM client's
    #    failure; the provider's reason must never surface it (it uses a fixed,
    #    secret-free reason and no partial artifact).
    client = SpyLLMClient(fail_with=LLMClientError(f"llm auth failed with {value}"))
    provider = LLMGenerationProvider(client, request_timeout_seconds=1.0)
    try:
        provider.generate_outline(_IDEA)
    except GenerationError as exc:
        outputs.append(("generation-reason", exc.reason, None))
        outputs.append(("generation-str", str(exc), None))

    # 3. DeliveryError reasons (7.6, 8.7, 9.6). The value is embedded in each
    #    underlying transport failure; every reason must redact it.
    smtp = FakeSmtpTransport(
        fail_with=SmtpTransportError(f"login rejected for {value}")
    )
    try:
        EmailDeliverer(smtp, _email_settings(value)).deliver(report)
    except DeliveryError as exc:
        outputs.append(("delivery-email", exc.reason, "<Secret smtp_password>"))

    slack_transport = FakeHttpTransport(
        [HttpTransportError(f"handshake failed using {value}")]
    )
    try:
        SlackDeliverer(slack_transport, _slack_settings(value)).deliver(report)
    except DeliveryError as exc:
        outputs.append(("delivery-slack", exc.reason, "<Secret slack_token>"))

    notion_transport = FakeHttpTransport(
        [HttpTransportError(f"refused with {value}")]
    )
    try:
        NotionDeliverer(notion_transport, _notion_settings(value)).deliver(report)
    except DeliveryError as exc:
        outputs.append(("delivery-notion", exc.reason, "<Secret notion_token>"))

    # 4. Configuration validation report (11.5). Secrets are configured (carrying
    #    the value); the report identifies problems by key only.
    invalid_loader = ConfigLoader([OverridesSource(_invalid_overrides(value))])
    validation_report = invalid_loader.validate(invalid_loader.load())
    assert not validation_report.ok  # a malformed/missing required value blocks startup
    for problem in validation_report.problems:
        outputs.append((f"validation-{problem.key}", f"{problem.key} {problem.issue}", None))

    # 5a. Startup summary -- validation failure (12.1, 12.2). The CompositionRoot
    #     reports every problem by key and never runs the scheduler / issues a
    #     request.
    validation_messages: list[str] = []
    root = _composition_root(
        ConfigLoader([OverridesSource(_invalid_overrides(value))]),
        validation_messages.append,
    )
    result = root.run()
    assert result.started is False
    for message in validation_messages:
        outputs.append(("startup-validation", message, None))

    # 5b. Startup summary -- component-construction failure (12.3, 14.7). Validation
    #     passes; a failing SMTP transport factory whose message embeds the value
    #     forces a construction failure, and the reported summary must redact it.
    def _raising_smtp_factory(_settings) -> object:
        raise ValueError(f"smtp init blew up with {value}")

    component_messages: list[str] = []
    root2 = _composition_root(
        ConfigLoader([OverridesSource(_valid_overrides(value))]),
        component_messages.append,
        smtp_factory=_raising_smtp_factory,
    )
    result2 = root2.run()
    assert result2.started is False
    assert result2.failed_component == "Deliverers"
    for message in component_messages:
        # The embedded value is scrubbed and replaced by a redacted placeholder.
        outputs.append(("startup-component", message, "<Secret "))

    # 6. AuthManager reasons + log entries (13.6). Every secret carries the value;
    #    the re-authorization and invalid-key reasons (built through redact over
    #    all secrets) and any log entry emitted while handling them must exclude
    #    the value.
    auth_logger = logging.getLogger("infrastructure.auth_manager")
    captured_logs: list[str] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured_logs.append(record.getMessage())

    handler = _ListHandler()
    auth_logger.addHandler(handler)
    try:
        auth = AuthManager(
            AuthSettings(
                youtube_api_key=Secret(value, CredentialReference("youtube_api_key")),
                oauth=OAuthCredentials(
                    client_id="oauth-client-id",
                    client_secret=Secret(value, CredentialReference("oauth_client_secret")),
                    refresh_token=None,
                    access_token=None,
                ),
            ),
            FakeHttpTransport(),
            FakeClock(),
        )
        outputs.append(("auth-invalid-key", auth.invalid_api_key_error().reason, None))
        try:
            # No access token and no refresh token -> re-authorization required.
            auth.analytics_auth_header("UC-owned-1")
        except NonTransientError as exc:
            outputs.append(("auth-reauth", exc.reason, None))
    finally:
        auth_logger.removeHandler(handler)

    for message in captured_logs:
        assert value not in message, f"secret leaked in auth log entry: {message!r}"

    # ---- Universal assertions over every rendered output --------------------
    assert outputs, "expected at least one secret-bearing output to assert on"
    for label, text, placeholder in outputs:
        assert value not in text, f"secret value leaked in {label}: {text!r}"
        if placeholder is not None:
            assert placeholder in text, (
                f"{label} did not render the credential reference placeholder "
                f"in place of the secret: {text!r}"
            )
