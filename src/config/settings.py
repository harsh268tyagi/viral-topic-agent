"""Validated configuration models for the Real Provider Integration.

This module defines the in-memory ``Settings`` aggregate that the
``ConfigLoader`` assembles from the precedence-ordered ``ConfigurationSource``s
(``.kiro/specs/real-provider-integration/design.md`` -> *Settings and component
settings*), together with the per-component settings groups and the startup
``ValidationReport`` model (Requirement 11).

Design principles encoded here:

- **Immutability.** Every settings group is a ``frozen`` dataclass, matching the
  rest of the codebase (``domain/models.py``). Frozen dataclasses give
  value-based equality and are safe to share across the composition root.
- **Secrets never render.** Credential values are held as :class:`Secret`
  instances from :mod:`config.secrets`; their ``repr``/``str`` are redacted, so
  a settings group can be logged or summarized without leaking a value
  (Requirement 12).
- **Reports name keys, never values.** :class:`ConfigProblem` and
  :class:`ValidationReport` identify a missing or malformed value by its
  documented configuration *key* only; no :class:`Secret` value ever appears in
  a report (11.5, 12.5).
- **Optional components are ``None``.** A component that is not enabled (no
  OAuth, an unselected delivery destination, an unconfigured keyword source or
  template strategy) is represented by ``None`` rather than a partially-filled
  group, so ``validate`` only checks the groups that are actually enabled
  (11.3, 11.4).

Requirements traceability: 11.5, 12.5 (and the data-model definitions for
Requirements 10, 11, 12, 13).
"""

from __future__ import annotations

from dataclasses import dataclass

from config.secrets import Secret
from domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    DeliveryDestination,
    Schedule,
)

__all__ = [
    "OAuthCredentials",
    "AuthSettings",
    "EmailSettings",
    "SlackSettings",
    "NotionSettings",
    "KeywordSourceSettings",
    "TemplateStrategySettings",
    "Settings",
    "ConfigProblem",
    "ValidationReport",
]


# ---------------------------------------------------------------------------
# Authentication settings (Requirement 13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthCredentials:
    """OAuth credentials authorizing owned-channel Analytics access (Req. 13).

    The set of values that authorize the YouTube Analytics API for an owned
    channel: a non-secret ``client_id`` plus the secret ``client_secret`` and
    the secret refresh/access tokens. ``refresh_token`` and ``access_token`` are
    optional because a freshly-configured channel may carry only one of them and
    because the access token can expire and be re-obtained from the refresh
    token (13.3, 13.4, 13.7).
    """

    client_id: str
    client_secret: Secret
    refresh_token: Secret | None = None
    access_token: Secret | None = None


@dataclass(frozen=True)
class AuthSettings:
    """Authentication configuration for the YouTube data and analytics APIs.

    ``youtube_api_key`` is the API key used for public YouTube Data API requests
    (13.1). ``oauth`` is present only when the Analytics API has been authorized
    for the owned channel (13.2); when it is ``None`` the data source degrades
    audience-activity retrieval to the documented ``NonTransientError`` (3.3).
    """

    youtube_api_key: Secret
    oauth: OAuthCredentials | None = None


# ---------------------------------------------------------------------------
# Delivery-destination settings (Requirements 7, 8, 9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailSettings:
    """SMTP configuration for the Email_Deliverer (Requirement 7).

    ``password`` is held as a :class:`Secret`; the remaining fields are
    non-secret connection and addressing values.
    """

    host: str
    port: int
    username: str
    password: Secret
    sender: str
    recipient: str


@dataclass(frozen=True)
class SlackSettings:
    """Slack configuration for the Slack_Deliverer (Requirement 8).

    ``token`` is the bearer token used with ``chat.postMessage`` (held as a
    :class:`Secret`); ``channel`` is the target destination and
    ``api_base_url`` is the Slack API base URL.
    """

    token: Secret
    channel: str
    api_base_url: str


@dataclass(frozen=True)
class NotionSettings:
    """Notion configuration for the Notion_Deliverer (Requirement 9).

    ``token`` is the integration token (held as a :class:`Secret`);
    ``database_id`` names the target database, ``api_version`` is the value sent
    in the Notion API-version header, and ``api_base_url`` is the API base URL.
    """

    token: Secret
    database_id: str
    api_version: str
    api_base_url: str


# ---------------------------------------------------------------------------
# Optional data-point source settings (Requirements 4, 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class KeywordSourceSettings:
    """Configuration for the optional Keyword_Metrics_Provider (Requirement 4).

    The public YouTube Data API does not expose keyword demand/competition, so
    these values come from a configurable external source. Fields are inferred
    from Requirement 4: the source is reached over HTTP (``api_base_url``) and
    may require its own credential (``api_key``, held as a :class:`Secret` when
    present). ``max_keywords`` is the default cap applied when a caller does not
    request a smaller maximum (4.2). When the whole group is ``None`` on
    :class:`Settings`, ``get_keyword_metrics`` degrades to an empty list (4.3).
    """

    api_base_url: str
    api_key: Secret | None = None
    max_keywords: int = 25


@dataclass(frozen=True)
class TemplateStrategySettings:
    """Configuration for the optional Template_Performance_Strategy (Req. 5).

    A ready-made viral-template-performance feed does not exist, so template
    performance is derived from retrieved video statistics through a configurable
    strategy. Fields are inferred from Requirement 5 (and the format-recommender
    sample thresholds): ``strategy`` names which derivation strategy to apply,
    ``min_sample_size`` is the minimum number of sample videos required before a
    derived ``TemplatePerformance`` is emitted, and ``lookback_days`` optionally
    bounds how far back retrieved videos are considered. When the whole group is
    ``None`` on :class:`Settings`, ``get_template_performance`` degrades to an
    empty list (5.3).
    """

    strategy: str
    min_sample_size: int = 5
    lookback_days: int | None = None


# ---------------------------------------------------------------------------
# Aggregate settings (Requirement 10) and Configuration source fields (Req. 14)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """The validated, in-memory configuration assembled by the Config_Loader.

    Groups the authentication settings, request/LLM timeouts, the optional
    data-point sources, and the optional per-destination delivery settings, plus
    the non-secret fields the :class:`CompositionRoot` needs to build a domain
    :class:`~domain.models.Configuration` (14.3): authorized channels, selected
    category, monitored competitors, schedule, and selected delivery
    destinations.

    Optional components are ``None`` when not enabled, so startup validation
    checks only the groups that are actually in use (11.3, 11.4).
    """

    auth: AuthSettings
    llm_timeout_seconds: float
    request_timeout_seconds: float
    keyword_source: KeywordSourceSettings | None = None
    template_strategy: TemplateStrategySettings | None = None
    email: EmailSettings | None = None
    slack: SlackSettings | None = None
    notion: NotionSettings | None = None
    # The fields needed to build a domain Configuration (Requirement 14.3):
    authorized_channels: tuple[AuthorizedChannel, ...] = ()
    selected_category: ChannelCategory | None = None
    monitored_competitors: tuple[str, ...] = ()
    schedule: Schedule | None = None
    delivery_destinations: tuple[DeliveryDestination, ...] = ()


# ---------------------------------------------------------------------------
# Startup validation report (Requirement 11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigProblem:
    """A single configuration problem identified by key only (11.5, 12.5).

    ``key`` is the documented configuration key for the offending value and
    never the value itself, so a problem may be reported in a startup summary
    without leaking a secret. ``issue`` describes the nature of the problem,
    such as ``"missing"`` or ``"malformed: <expected form>"``.
    """

    key: str
    issue: str


@dataclass(frozen=True)
class ValidationReport:
    """The result of startup validation: all problems found, by key (Req. 11).

    Lists every missing or malformed required configuration value so the Creator
    can fix them all at once before any external request is issued (11.2). The
    :attr:`ok` property is ``True`` exactly when no problems were found, which
    the :class:`CompositionRoot` uses to decide whether to proceed.
    """

    problems: tuple[ConfigProblem, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the configuration is valid (no problems were reported)."""
        return not self.problems
