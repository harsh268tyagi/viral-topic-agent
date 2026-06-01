"""Conformance tests for the real deliverers against the ``Deliverer`` boundary (task 9.7).

Asserts that each concrete, network-backed deliverer structurally satisfies the
existing single-method :class:`~delivery.deliverer.Deliverer` protocol *without
modifying that protocol* (Requirements 7.1, 8.1, 9.1, 16.1):

- :class:`~delivery.email_deliverer.EmailDeliverer` over the ``SmtpTransport`` port,
- :class:`~delivery.slack_deliverer.SlackDeliverer` over the ``HttpTransport`` port,
- :class:`~delivery.notion_deliverer.NotionDeliverer` over the ``HttpTransport`` port.

``Deliverer`` is a ``runtime_checkable`` ``Protocol``, so the structural check is a
single ``isinstance`` against the unmodified protocol -- mirroring the existing
conformance checks for the in-memory stubs
(``tests/test_delivery.py::test_per_destination_stubs_satisfy_deliverer_protocol``)
and the ``GenerationProvider`` protocol (``tests/test_llm_generation_provider.py``).
A method-presence check and a static-typing assignment document the same contract
from the other two angles.

Each deliverer is constructed against the injected fakes from ``tests/edge_fakes.py``
(``FakeHttpTransport``, ``FakeSmtpTransport``) and the settings models from
``src/config/settings.py`` with :class:`~config.secrets.Secret` values from
``src/config/secrets.py``, so the checks need no real network access (16.3).

This module is deliberately limited to structural conformance; the happy/failure
delivery paths are covered separately by task 9.6.
"""

from __future__ import annotations

import pytest

from config.secrets import CredentialReference, Secret
from config.settings import EmailSettings, NotionSettings, SlackSettings
from delivery.deliverer import Deliverer
from delivery.email_deliverer import EmailDeliverer
from delivery.notion_deliverer import NotionDeliverer
from delivery.slack_deliverer import SlackDeliverer

from tests.edge_fakes import FakeHttpTransport, FakeSmtpTransport


# ---------------------------------------------------------------------------
# Construction helpers (injected fakes + redactable secret settings)
# ---------------------------------------------------------------------------


def _secret(name: str, value: str) -> Secret:
    """A :class:`Secret` wrapping ``value`` behind the non-secret handle ``name``."""
    return Secret(value, CredentialReference(name))


def _email_deliverer() -> EmailDeliverer:
    """Build an ``EmailDeliverer`` over a :class:`FakeSmtpTransport` (7.1)."""
    settings = EmailSettings(
        host="smtp.example.com",
        port=587,
        username="digest@example.com",
        password=_secret("smtp_password", "smtp-secret"),
        sender="digest@example.com",
        recipient="creator@example.com",
    )
    return EmailDeliverer(FakeSmtpTransport(), settings)


def _slack_deliverer() -> SlackDeliverer:
    """Build a ``SlackDeliverer`` over a :class:`FakeHttpTransport` (8.1)."""
    settings = SlackSettings(
        token=_secret("slack_token", "xoxb-secret"),
        channel="#viral-digest",
        api_base_url="https://slack.example.com/api",
    )
    return SlackDeliverer(FakeHttpTransport(), settings)


def _notion_deliverer() -> NotionDeliverer:
    """Build a ``NotionDeliverer`` over a :class:`FakeHttpTransport` (9.1)."""
    settings = NotionSettings(
        token=_secret("notion_token", "secret_notion-token"),
        database_id="db-1234",
        api_version="2022-06-28",
        api_base_url="https://notion.example.com/v1",
    )
    return NotionDeliverer(FakeHttpTransport(), settings)


# Each real deliverer paired with a readable label for parametrized reporting.
_DELIVERER_BUILDERS = (
    pytest.param(_email_deliverer, id="EmailDeliverer"),
    pytest.param(_slack_deliverer, id="SlackDeliverer"),
    pytest.param(_notion_deliverer, id="NotionDeliverer"),
)


# ---------------------------------------------------------------------------
# Runtime structural conformance (isinstance against the runtime_checkable Protocol)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build_deliverer", _DELIVERER_BUILDERS)
def test_real_deliverer_satisfies_deliverer_protocol(build_deliverer):
    """Each real deliverer is an instance of the runtime-checkable ``Deliverer`` (7.1, 8.1, 9.1, 16.1)."""
    deliverer = build_deliverer()
    assert isinstance(deliverer, Deliverer)


# ---------------------------------------------------------------------------
# Method-presence conformance (the single-method boundary)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("build_deliverer", _DELIVERER_BUILDERS)
def test_real_deliverer_exposes_single_deliver_method(build_deliverer):
    """Each real deliverer exposes the single ``deliver`` operation of the boundary (7.1, 8.1, 9.1)."""
    deliverer = build_deliverer()
    assert callable(getattr(deliverer, "deliver"))


# ---------------------------------------------------------------------------
# Static-typing conformance (assignable to a ``Deliverer``-typed binding)
# ---------------------------------------------------------------------------


def test_real_deliverers_typing_assignment():
    """Each concrete deliverer is usable wherever a ``Deliverer`` is expected (16.1).

    The static-typing counterpart to the runtime ``isinstance`` check: it
    documents that the concrete deliverers are assignable to a
    ``Deliverer``-typed binding without the protocol being modified.
    """
    email: Deliverer = _email_deliverer()
    slack: Deliverer = _slack_deliverer()
    notion: Deliverer = _notion_deliverer()
    assert email is not None
    assert slack is not None
    assert notion is not None
