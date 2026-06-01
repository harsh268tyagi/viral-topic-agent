"""Example-based unit tests for the three real deliverers' happy/failure paths.

Covers task 9.6 for the Real Provider Integration feature: the success and
failure branches of the concrete :class:`~delivery.email_deliverer.EmailDeliverer`,
:class:`~delivery.slack_deliverer.SlackDeliverer`, and
:class:`~delivery.notion_deliverer.NotionDeliverer`, plus the Slack last-resort
fallback when a ``DeliveryError`` cannot be constructed/raised after a posting
failure. Every branch runs against the injected fakes in ``tests/edge_fakes.py``
(``FakeHttpTransport``, ``FakeSmtpTransport``) so there is no real network access
(16.3).

What each test group asserts:

- **Success (7.3, 8.3, 9.3):** a succeeding fake makes ``deliver`` return
  ``None`` and perform exactly one transmit/post/record.
- **Failure (7.5, 8.5, 9.5):** a failing fake makes ``deliver`` raise a
  :class:`~delivery.deliverer.DeliveryError`. Email fails on an SMTP error;
  Slack fails on a non-2xx status, a transport error, *and* an ``ok == false``
  body; Notion fails on a non-2xx status and a transport error.
- **Slack last-resort fallback (8.6):** when constructing/raising the
  ``DeliveryError`` itself fails (forced here by monkeypatching ``redact`` to
  raise), the deliverer records a delivery-failed log entry naming the Slack
  destination and still surfaces a failure to the caller.
- **Secret hygiene (7.6, 8.7, 9.6 -> here asserted on the raised reason):** no
  configured secret value ever appears in a raised ``DeliveryError`` reason,
  even when the underlying failure text embeds the secret.

Validates: Requirements 7.3, 7.5, 8.3, 8.5, 8.6, 9.3, 9.5
"""

from __future__ import annotations

import json
import logging

import pytest

from config.secrets import CredentialReference, Secret
from config.settings import EmailSettings, NotionSettings, SlackSettings
from delivery import slack_deliverer as slack_module
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
from infrastructure.http_transport import HttpResponse, HttpTransportError
from tests.edge_fakes import FakeHttpTransport, FakeSmtpTransport

# ---------------------------------------------------------------------------
# Distinctive secret values: chosen so that if redaction ever failed the raw
# value would be trivially findable in the raised reason.
# ---------------------------------------------------------------------------

_SMTP_PASSWORD_VALUE = "smtp-password-SHOULD-NEVER-LEAK-123"
_SLACK_TOKEN_VALUE = "xoxb-slack-token-SHOULD-NEVER-LEAK-456"
_NOTION_TOKEN_VALUE = "secret-notion-token-SHOULD-NEVER-LEAK-789"


# ---------------------------------------------------------------------------
# Report fixtures (a populated and an all-empty report; both render to three
# valid sections via the shared renderer, so deliver never aborts on shape).
# ---------------------------------------------------------------------------

_TEMPLATE = ViralTemplate(
    template_id="t0",
    name="tier-list ranking",
    category=ChannelCategory.GAMING,
    observed_performance=1000.0,
)


def _populated_report() -> DigestReport:
    idea = ContentIdea(
        idea_id="i0",
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(_TEMPLATE,),
        observed_metric_value=5000.0,
    )
    scored = [ScoredIdea(idea=idea, score=50, confidence=Confidence.NORMAL)]
    spikes = [
        CompetitorSpike(
            channel_id="comp-1", video_id="v0", view_count=9000, spike_factor=3.5
        )
    ]
    outliers = [Outlier(video_id="o0", view_count=50000, outlier_factor=6.0)]
    return DigestService().compile(scored, spikes, outliers)


def _empty_report() -> DigestReport:
    return DigestService().compile([], [], [])


@pytest.fixture
def report() -> DigestReport:
    return _populated_report()


# ---------------------------------------------------------------------------
# Settings fixtures
# ---------------------------------------------------------------------------


def _email_settings() -> EmailSettings:
    return EmailSettings(
        host="smtp.example.com",
        port=587,
        username="agent@example.com",
        password=Secret(_SMTP_PASSWORD_VALUE, CredentialReference("smtp_password")),
        sender="agent@example.com",
        recipient="creator@example.com",
    )


def _slack_settings() -> SlackSettings:
    return SlackSettings(
        token=Secret(_SLACK_TOKEN_VALUE, CredentialReference("slack_token")),
        channel="#viral-digest",
        api_base_url="https://slack.example.com/api",
    )


def _notion_settings() -> NotionSettings:
    return NotionSettings(
        token=Secret(_NOTION_TOKEN_VALUE, CredentialReference("notion_token")),
        database_id="db-1234567890",
        api_version="2022-06-28",
        api_base_url="https://notion.example.com/v1",
    )


def _ok_body() -> bytes:
    return json.dumps({"ok": True}).encode("utf-8")


def _not_ok_body(error_code: str = "channel_not_found") -> bytes:
    return json.dumps({"ok": False, "error": error_code}).encode("utf-8")


# ===========================================================================
# EmailDeliverer (Requirement 7)
# ===========================================================================


def test_email_deliver_success_returns_none_and_transmits_once(
    report: DigestReport,
) -> None:
    """A succeeding SMTP fake makes ``deliver`` return ``None`` (7.3).

    Validates: Requirements 7.3
    """
    smtp = FakeSmtpTransport()
    deliverer = EmailDeliverer(smtp, _email_settings())

    assert deliverer.deliver(report) is None
    # Exactly one transmission attempt; the single-attempt contract is preserved.
    assert smtp.call_count == 1


def test_email_deliver_success_on_empty_report_returns_none() -> None:
    """An all-empty (but valid three-section) report still delivers (7.2, 7.3).

    Validates: Requirements 7.3
    """
    smtp = FakeSmtpTransport()
    deliverer = EmailDeliverer(smtp, _email_settings())

    assert deliverer.deliver(_empty_report()) is None
    assert smtp.call_count == 1


def test_email_deliver_failure_raises_delivery_error(report: DigestReport) -> None:
    """A failing SMTP fake makes ``deliver`` raise ``DeliveryError`` (7.5).

    Validates: Requirements 7.5
    """
    smtp = FakeSmtpTransport(fail_with=SmtpTransportError("connection refused"))
    deliverer = EmailDeliverer(smtp, _email_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_email_failure_reason_excludes_smtp_password(report: DigestReport) -> None:
    """The SMTP password never appears in the raised reason, even if embedded (7.6).

    Validates: Requirements 7.5
    """
    # The transport error text embeds the secret value to prove redaction runs.
    smtp = FakeSmtpTransport(
        fail_with=SmtpTransportError(f"login rejected for {_SMTP_PASSWORD_VALUE}")
    )
    deliverer = EmailDeliverer(smtp, _email_settings())

    with pytest.raises(DeliveryError) as exc_info:
        deliverer.deliver(report)

    assert _SMTP_PASSWORD_VALUE not in exc_info.value.reason


# ===========================================================================
# SlackDeliverer (Requirement 8)
# ===========================================================================


def test_slack_deliver_success_returns_none_and_posts_once(
    report: DigestReport,
) -> None:
    """A 2xx + ``ok == true`` response makes ``deliver`` return ``None`` (8.3).

    Validates: Requirements 8.3
    """
    transport = FakeHttpTransport([HttpResponse(status=200, body=_ok_body())])
    deliverer = SlackDeliverer(transport, _slack_settings())

    assert deliverer.deliver(report) is None
    # Exactly one POST to chat.postMessage; no retry lives in the deliverer.
    assert transport.call_count == 1
    assert transport.last_request is not None
    assert transport.last_request.method == "POST"
    assert transport.last_request.url.endswith("/chat.postMessage")


def test_slack_deliver_failure_on_non_2xx_raises_delivery_error(
    report: DigestReport,
) -> None:
    """A non-2xx Slack status makes ``deliver`` raise ``DeliveryError`` (8.5).

    Validates: Requirements 8.5
    """
    transport = FakeHttpTransport([HttpResponse(status=500, body=b"{}")])
    deliverer = SlackDeliverer(transport, _slack_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_slack_deliver_failure_on_transport_error_raises_delivery_error(
    report: DigestReport,
) -> None:
    """A transport-level failure makes ``deliver`` raise ``DeliveryError`` (8.5).

    Validates: Requirements 8.5
    """
    transport = FakeHttpTransport([HttpTransportError("connection reset")])
    deliverer = SlackDeliverer(transport, _slack_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_slack_deliver_failure_on_ok_false_body_raises_delivery_error(
    report: DigestReport,
) -> None:
    """An HTTP 200 with ``ok == false`` is a logical failure -> ``DeliveryError`` (8.5).

    Validates: Requirements 8.5
    """
    transport = FakeHttpTransport([HttpResponse(status=200, body=_not_ok_body())])
    deliverer = SlackDeliverer(transport, _slack_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_slack_failure_reason_excludes_token(report: DigestReport) -> None:
    """The Slack bearer token never appears in the raised reason (8.7).

    Validates: Requirements 8.5
    """
    # The transport error text embeds the token to prove redaction runs.
    transport = FakeHttpTransport(
        [HttpTransportError(f"handshake failed using {_SLACK_TOKEN_VALUE}")]
    )
    deliverer = SlackDeliverer(transport, _slack_settings())

    with pytest.raises(DeliveryError) as exc_info:
        deliverer.deliver(report)

    assert _SLACK_TOKEN_VALUE not in exc_info.value.reason


def test_slack_last_resort_fallback_logs_and_surfaces_failure(
    report: DigestReport,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """When the ``DeliveryError`` itself cannot be built, log + surface a failure (8.6).

    Forcing ``redact`` to raise simulates being unable to construct/raise the
    ``DeliveryError`` after a posting failure. The deliverer must then record a
    delivery-failed log entry naming the Slack destination and still surface a
    failure to the caller rather than swallow it.

    Validates: Requirements 8.6
    """

    def _boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("redaction unavailable")

    # ``redact`` is used inside SlackDeliverer._surface_failure; making it raise
    # drives the 8.6 last-resort fallback path.
    monkeypatch.setattr(slack_module, "redact", _boom)

    settings = _slack_settings()
    # A non-2xx response triggers a posting failure that must be surfaced.
    transport = FakeHttpTransport([HttpResponse(status=500, body=b"{}")])
    deliverer = SlackDeliverer(transport, settings)

    with caplog.at_level(logging.ERROR, logger="delivery.slack_deliverer"):
        # A failure is still surfaced to the caller (it is *not* swallowed). The
        # fallback re-raises the underlying posting failure, not a DeliveryError
        # (which could not be constructed).
        with pytest.raises(Exception) as exc_info:
            deliverer.deliver(report)

    assert not isinstance(exc_info.value, DeliveryError)
    # A delivery-failed entry naming the Slack destination was recorded (8.6).
    assert "delivery-failed" in caplog.text
    assert settings.channel in caplog.text


# ===========================================================================
# NotionDeliverer (Requirement 9)
# ===========================================================================


def test_notion_deliver_success_returns_none_and_records_once(
    report: DigestReport,
) -> None:
    """A 2xx response makes ``deliver`` return ``None`` (9.3).

    Validates: Requirements 9.3
    """
    transport = FakeHttpTransport([HttpResponse(status=200, body=b"{}")])
    deliverer = NotionDeliverer(transport, _notion_settings())

    assert deliverer.deliver(report) is None
    # Exactly one pages.create POST; no retry lives in the deliverer.
    assert transport.call_count == 1
    assert transport.last_request is not None
    assert transport.last_request.method == "POST"
    assert transport.last_request.url.endswith("/pages")


def test_notion_deliver_failure_on_non_2xx_raises_delivery_error(
    report: DigestReport,
) -> None:
    """A non-2xx Notion status makes ``deliver`` raise ``DeliveryError`` (9.5).

    Validates: Requirements 9.5
    """
    transport = FakeHttpTransport([HttpResponse(status=500, body=b"{}")])
    deliverer = NotionDeliverer(transport, _notion_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_notion_deliver_failure_on_transport_error_raises_delivery_error(
    report: DigestReport,
) -> None:
    """A transport-level failure makes ``deliver`` raise ``DeliveryError`` (9.5).

    Validates: Requirements 9.5
    """
    transport = FakeHttpTransport([HttpTransportError("connection reset")])
    deliverer = NotionDeliverer(transport, _notion_settings())

    with pytest.raises(DeliveryError):
        deliverer.deliver(report)


def test_notion_failure_reason_excludes_token(report: DigestReport) -> None:
    """The Notion integration token never appears in the raised reason (9.6).

    Validates: Requirements 9.5
    """
    # The transport error text embeds the token to prove redaction runs.
    transport = FakeHttpTransport(
        [HttpTransportError(f"refused with {_NOTION_TOKEN_VALUE}")]
    )
    deliverer = NotionDeliverer(transport, _notion_settings())

    with pytest.raises(DeliveryError) as exc_info:
        deliverer.deliver(report)

    assert _NOTION_TOKEN_VALUE not in exc_info.value.reason
