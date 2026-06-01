"""Layered configuration loader for the Real Provider Integration.

This module implements the precedence-resolving :class:`ConfigLoader` described
in ``.kiro/specs/real-provider-integration/design.md`` (-> *ConfigLoader,
Settings, ConfigurationSource* and *Configuration precedence (Requirement 10)*).

``ConfigLoader.load`` assembles a validated-shaped :class:`~config.settings.Settings`
from a precedence-ordered sequence of :class:`~config.sources.ConfigurationSource`s,
**highest precedence first**:

1. :class:`~config.sources.OverridesSource` - explicit, in-process overrides.
2. :class:`~config.sources.EnvSource` - process environment variables.
3. :class:`~config.sources.DotEnvSource` - a local ``.env`` file.
4. :class:`~config.sources.ConfigFileSource` - configuration-file defaults.

For each documented configuration key, the loader takes the value from the
highest-precedence source that supplies it (10.1, 10.2); for a key absent from
every source, it falls back to the documented per-key default in
:data:`DEFAULTS` (10.3). Each resolved value is then mapped to its corresponding
:class:`Settings` field by its documented configuration key (10.5).

Scope (task 2.4): this module implements **value resolution and field mapping
only**. Startup validation - detecting and reporting missing or malformed
required values - is implemented separately by ``ConfigLoader.validate`` (task
2.6, Requirement 11). To keep the two concerns cleanly separable,
:meth:`ConfigLoader.load` is **total**: it never raises on missing or
un-coercible values. Instead it produces a ``Settings`` in which an absent
required secret/string becomes an empty placeholder (an empty
:class:`~config.secrets.Secret` keyed by its documented reference, or an empty
string), and an un-coercible numeric value falls back to its documented default.
``validate`` is responsible for reporting those as problems by key.

Only the standard library is used here, keeping the configuration layer
dependency-free per the design's layer-placement table.

Requirements traceability: 10.1, 10.2, 10.3, 10.5.
"""

from __future__ import annotations

import math
import re
from typing import Iterable, Mapping, Sequence

from config.secrets import CredentialReference, Secret
from config.settings import (
    AuthSettings,
    ConfigProblem,
    EmailSettings,
    KeywordSourceSettings,
    NotionSettings,
    OAuthCredentials,
    Settings,
    SlackSettings,
    TemplateStrategySettings,
    ValidationReport,
)
from config.sources import ConfigurationSource
from domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    DeliveryDestination,
    Schedule,
)

__all__ = ["ConfigLoader", "DEFAULTS"]


# ---------------------------------------------------------------------------
# Documented configuration keys (Requirement 10.5)
#
# Each constant is the documented configuration key for exactly one Settings
# field. Sources expose these keys (case-sensitive) and the loader maps each to
# its field below. Secret fields derive their CredentialReference name from the
# lower-cased key (e.g. "YOUTUBE_API_KEY" -> "youtube_api_key"), matching the
# handles used throughout the design.
# ---------------------------------------------------------------------------

# Authentication (Requirement 13)
KEY_YOUTUBE_API_KEY = "YOUTUBE_API_KEY"
KEY_OAUTH_CLIENT_ID = "YOUTUBE_OAUTH_CLIENT_ID"
KEY_OAUTH_CLIENT_SECRET = "YOUTUBE_OAUTH_CLIENT_SECRET"
KEY_OAUTH_REFRESH_TOKEN = "YOUTUBE_OAUTH_REFRESH_TOKEN"
KEY_OAUTH_ACCESS_TOKEN = "YOUTUBE_OAUTH_ACCESS_TOKEN"

# Timeouts
KEY_LLM_TIMEOUT_SECONDS = "LLM_TIMEOUT_SECONDS"
KEY_REQUEST_TIMEOUT_SECONDS = "REQUEST_TIMEOUT_SECONDS"

# Optional keyword-metrics source (Requirement 4)
KEY_KEYWORD_SOURCE_API_BASE_URL = "KEYWORD_SOURCE_API_BASE_URL"
KEY_KEYWORD_SOURCE_API_KEY = "KEYWORD_SOURCE_API_KEY"
KEY_KEYWORD_SOURCE_MAX_KEYWORDS = "KEYWORD_SOURCE_MAX_KEYWORDS"

# Optional template-performance strategy (Requirement 5)
KEY_TEMPLATE_STRATEGY = "TEMPLATE_STRATEGY"
KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE = "TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE"
KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS = "TEMPLATE_STRATEGY_LOOKBACK_DAYS"

# Email delivery (Requirement 7)
KEY_SMTP_HOST = "SMTP_HOST"
KEY_SMTP_PORT = "SMTP_PORT"
KEY_SMTP_USERNAME = "SMTP_USERNAME"
KEY_SMTP_PASSWORD = "SMTP_PASSWORD"
KEY_EMAIL_SENDER = "EMAIL_SENDER"
KEY_EMAIL_RECIPIENT = "EMAIL_RECIPIENT"

# Slack delivery (Requirement 8)
KEY_SLACK_TOKEN = "SLACK_TOKEN"
KEY_SLACK_CHANNEL = "SLACK_CHANNEL"
KEY_SLACK_API_BASE_URL = "SLACK_API_BASE_URL"

# Notion delivery (Requirement 9)
KEY_NOTION_TOKEN = "NOTION_TOKEN"
KEY_NOTION_DATABASE_ID = "NOTION_DATABASE_ID"
KEY_NOTION_API_VERSION = "NOTION_API_VERSION"
KEY_NOTION_API_BASE_URL = "NOTION_API_BASE_URL"

# Domain Configuration fields (Requirement 14.3)
KEY_OWNED_CHANNEL_ID = "OWNED_CHANNEL_ID"
KEY_OWNED_CHANNEL_CREDENTIALS_REF = "OWNED_CHANNEL_CREDENTIALS_REF"
KEY_SELECTED_CATEGORY = "SELECTED_CATEGORY"
KEY_COMPETITORS = "COMPETITORS"
KEY_SCHEDULE_INTERVAL = "SCHEDULE_INTERVAL"
KEY_SCHEDULE_RUN_TIME = "SCHEDULE_RUN_TIME"
KEY_DELIVERY_DESTINATIONS = "DELIVERY_DESTINATIONS"


# ---------------------------------------------------------------------------
# Documented per-key defaults (Requirement 10.3)
#
# Only keys whose absence still yields a meaningful default appear here. These
# are the lowest-precedence layer: a value supplied by any source overrides the
# default. Defaults are intentionally NOT used to decide whether an optional
# component is enabled - that decision is based on values actually supplied by a
# source (or a selected delivery destination) - so a default such as a Slack API
# base URL never silently enables an unconfigured component.
# ---------------------------------------------------------------------------

DEFAULTS: Mapping[str, str] = {
    KEY_LLM_TIMEOUT_SECONDS: "60",
    KEY_REQUEST_TIMEOUT_SECONDS: "30",
    KEY_SMTP_PORT: "587",
    KEY_SLACK_API_BASE_URL: "https://slack.com/api",
    KEY_NOTION_API_BASE_URL: "https://api.notion.com",
    KEY_NOTION_API_VERSION: "2022-06-28",
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS: "25",
    KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE: "5",
    KEY_OWNED_CHANNEL_CREDENTIALS_REF: "youtube_api_key",
}


# Keys that, when supplied by a source, indicate an optional component is in use.
_OAUTH_KEYS = (
    KEY_OAUTH_CLIENT_ID,
    KEY_OAUTH_CLIENT_SECRET,
    KEY_OAUTH_REFRESH_TOKEN,
    KEY_OAUTH_ACCESS_TOKEN,
)
_KEYWORD_KEYS = (
    KEY_KEYWORD_SOURCE_API_BASE_URL,
    KEY_KEYWORD_SOURCE_API_KEY,
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS,
)
_TEMPLATE_KEYS = (
    KEY_TEMPLATE_STRATEGY,
    KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE,
    KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS,
)
_EMAIL_KEYS = (
    KEY_SMTP_HOST,
    KEY_SMTP_USERNAME,
    KEY_SMTP_PASSWORD,
    KEY_EMAIL_SENDER,
    KEY_EMAIL_RECIPIENT,
)
_SLACK_KEYS = (KEY_SLACK_TOKEN, KEY_SLACK_CHANNEL)
_NOTION_KEYS = (KEY_NOTION_TOKEN, KEY_NOTION_DATABASE_ID)


class ConfigLoader:
    """Assembles :class:`Settings` from precedence-ordered sources (Req. 10).

    Construct with the sources in **decreasing** precedence (highest first), as
    the design specifies: ``[OverridesSource, EnvSource, DotEnvSource,
    ConfigFileSource]``. :meth:`load` resolves every documented key across those
    sources, applies documented per-key defaults, and maps each value to its
    :class:`Settings` field.
    """

    __slots__ = ("_sources",)

    def __init__(self, sources: Sequence[ConfigurationSource]) -> None:
        # Highest precedence first; a defensive copy keeps the loader pure.
        self._sources = tuple(sources)

    # -- precedence resolution (Requirement 10.1, 10.2, 10.3) ---------------

    def _supplied(self) -> dict[str, str]:
        """Merge the sources by precedence, **without** applying defaults.

        Sources are stored highest-precedence first, so a key is taken from the
        first source that supplies it. Implemented by writing the lowest
        precedence first and letting higher-precedence sources overwrite, which
        leaves the highest-precedence value in place (10.1, 10.2).
        """
        merged: dict[str, str] = {}
        for source in reversed(self._sources):
            for key, value in source.values().items():
                merged[str(key)] = str(value)
        return merged

    def resolve(self) -> dict[str, str]:
        """Return every documented value resolved by precedence, with defaults.

        For each key, the value from the highest-precedence source that supplies
        it; for a key absent from every source, the documented default from
        :data:`DEFAULTS` (10.3). A key supplied by no source and carrying no
        default is simply absent from the result.
        """
        # Defaults sit below every source: spread them first, then the merged
        # supplied values so any supplied key overrides its default.
        return {**DEFAULTS, **self._supplied()}

    # -- field mapping (Requirement 10.5) -----------------------------------

    def load(self) -> Settings:
        """Assemble :class:`Settings` by mapping each resolved value to its field.

        Total by design: never raises on missing or un-coercible input. Absent
        required secrets/strings become empty placeholders and un-coercible
        numerics fall back to their documented defaults, leaving
        ``ConfigLoader.validate`` (task 2.6) to report problems by key.
        """
        supplied = self._supplied()
        resolved = {**DEFAULTS, **supplied}

        return Settings(
            auth=self._build_auth(supplied, resolved),
            llm_timeout_seconds=_as_float(resolved, KEY_LLM_TIMEOUT_SECONDS, 60.0),
            request_timeout_seconds=_as_float(
                resolved, KEY_REQUEST_TIMEOUT_SECONDS, 30.0
            ),
            keyword_source=self._build_keyword_source(supplied, resolved),
            template_strategy=self._build_template_strategy(supplied, resolved),
            email=self._build_email(supplied, resolved),
            slack=self._build_slack(supplied, resolved),
            notion=self._build_notion(supplied, resolved),
            authorized_channels=self._build_authorized_channels(resolved),
            selected_category=_as_category(resolved.get(KEY_SELECTED_CATEGORY)),
            monitored_competitors=_as_csv_tuple(resolved.get(KEY_COMPETITORS)),
            schedule=_build_schedule(resolved),
            delivery_destinations=_as_destinations(
                resolved.get(KEY_DELIVERY_DESTINATIONS)
            ),
        )

    # -- startup validation (Requirement 11) --------------------------------

    def validate(self, settings: Settings) -> ValidationReport:
        """Report every missing or malformed required value, by key (Req. 11).

        Validates the assembled :class:`Settings` for startup readiness: every
        required configuration value for each **enabled** component, and for each
        **selected** delivery destination, must be present and *well-formed* -
        non-empty and conforming to the documented type/format for its key
        (11.1). All problems are collected and returned together rather than
        failing on the first, so the Creator can fix everything at once (11.2).

        Which components are checked is driven by what the ``Settings`` actually
        enables:

        - The YouTube Data API key and the request/LLM timeouts back the always-on
          data source, LLM provider, and transports, so they are always required.
        - ``auth.oauth`` is checked only when OAuth is configured (Analytics
          authorized).
        - The optional keyword source and template strategy are checked only when
          their settings group is present.
        - A delivery destination is checked only when it appears in
          ``delivery_destinations``; an unselected destination's values are treated
          as not required (11.3, 11.4).

        Every problem identifies its value by documented configuration *key* only;
        no :class:`~config.secrets.Secret` value ever appears in the report
        (11.5, 12.5) - the report carries keys and expected-form descriptions, not
        values.
        """
        problems: list[ConfigProblem] = []

        # Always-on components: the public Data API key and the timeouts that
        # back the data source, LLM provider, and HTTP transports.
        _require_secret(settings.auth.youtube_api_key, KEY_YOUTUBE_API_KEY, problems)
        _require_positive_number(
            settings.llm_timeout_seconds, KEY_LLM_TIMEOUT_SECONDS, problems
        )
        _require_positive_number(
            settings.request_timeout_seconds, KEY_REQUEST_TIMEOUT_SECONDS, problems
        )

        # OAuth is required only when Analytics access is configured (13.2).
        if settings.auth.oauth is not None:
            self._validate_oauth(settings.auth.oauth, problems)

        # Optional data-point sources: validated only when enabled (Req. 4, 5).
        if settings.keyword_source is not None:
            self._validate_keyword_source(settings.keyword_source, problems)
        if settings.template_strategy is not None:
            self._validate_template_strategy(settings.template_strategy, problems)

        # Delivery destinations: only those selected in Settings (11.3, 11.4).
        self._validate_delivery(settings, problems)

        return ValidationReport(problems=tuple(problems))

    @staticmethod
    def _validate_oauth(oauth: OAuthCredentials, problems: list[ConfigProblem]) -> None:
        _require_str(oauth.client_id, KEY_OAUTH_CLIENT_ID, problems)
        _require_secret(oauth.client_secret, KEY_OAUTH_CLIENT_SECRET, problems)

    @staticmethod
    def _validate_keyword_source(
        keyword_source: KeywordSourceSettings, problems: list[ConfigProblem]
    ) -> None:
        _require_url(
            keyword_source.api_base_url, KEY_KEYWORD_SOURCE_API_BASE_URL, problems
        )
        _require_positive_int(
            keyword_source.max_keywords, KEY_KEYWORD_SOURCE_MAX_KEYWORDS, problems
        )

    @staticmethod
    def _validate_template_strategy(
        template_strategy: TemplateStrategySettings, problems: list[ConfigProblem]
    ) -> None:
        _require_str(template_strategy.strategy, KEY_TEMPLATE_STRATEGY, problems)
        _require_positive_int(
            template_strategy.min_sample_size,
            KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE,
            problems,
        )
        # lookback_days is optional; when supplied it must be a positive integer.
        if template_strategy.lookback_days is not None:
            _require_positive_int(
                template_strategy.lookback_days,
                KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS,
                problems,
            )

    @staticmethod
    def _validate_delivery(
        settings: Settings, problems: list[ConfigProblem]
    ) -> None:
        selected = settings.delivery_destinations
        if DeliveryDestination.EMAIL in selected:
            _validate_email(settings.email, problems)
        if DeliveryDestination.SLACK in selected:
            _validate_slack(settings.slack, problems)
        if DeliveryDestination.NOTION in selected:
            _validate_notion(settings.notion, problems)

    # -- component builders --------------------------------------------------

    @staticmethod
    def _build_auth(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> AuthSettings:
        oauth: OAuthCredentials | None = None
        if _any_present(supplied, _OAUTH_KEYS):
            oauth = OAuthCredentials(
                client_id=_as_str(resolved, KEY_OAUTH_CLIENT_ID),
                client_secret=_as_secret(resolved, KEY_OAUTH_CLIENT_SECRET),
                refresh_token=_as_opt_secret(
                    supplied, resolved, KEY_OAUTH_REFRESH_TOKEN
                ),
                access_token=_as_opt_secret(
                    supplied, resolved, KEY_OAUTH_ACCESS_TOKEN
                ),
            )
        return AuthSettings(
            youtube_api_key=_as_secret(resolved, KEY_YOUTUBE_API_KEY),
            oauth=oauth,
        )

    @staticmethod
    def _build_keyword_source(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> KeywordSourceSettings | None:
        if not _any_present(supplied, _KEYWORD_KEYS):
            return None
        return KeywordSourceSettings(
            api_base_url=_as_str(resolved, KEY_KEYWORD_SOURCE_API_BASE_URL),
            api_key=_as_opt_secret(supplied, resolved, KEY_KEYWORD_SOURCE_API_KEY),
            max_keywords=_as_int(resolved, KEY_KEYWORD_SOURCE_MAX_KEYWORDS, 25),
        )

    @staticmethod
    def _build_template_strategy(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> TemplateStrategySettings | None:
        if not _any_present(supplied, _TEMPLATE_KEYS):
            return None
        return TemplateStrategySettings(
            strategy=_as_str(resolved, KEY_TEMPLATE_STRATEGY),
            min_sample_size=_as_int(
                resolved, KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE, 5
            ),
            lookback_days=_as_opt_int(
                supplied, resolved, KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS
            ),
        )

    @staticmethod
    def _build_email(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> EmailSettings | None:
        selected = DeliveryDestination.EMAIL in _as_destinations(
            resolved.get(KEY_DELIVERY_DESTINATIONS)
        )
        if not selected and not _any_present(supplied, _EMAIL_KEYS):
            return None
        return EmailSettings(
            host=_as_str(resolved, KEY_SMTP_HOST),
            port=_as_int(resolved, KEY_SMTP_PORT, 587),
            username=_as_str(resolved, KEY_SMTP_USERNAME),
            password=_as_secret(resolved, KEY_SMTP_PASSWORD),
            sender=_as_str(resolved, KEY_EMAIL_SENDER),
            recipient=_as_str(resolved, KEY_EMAIL_RECIPIENT),
        )

    @staticmethod
    def _build_slack(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> SlackSettings | None:
        selected = DeliveryDestination.SLACK in _as_destinations(
            resolved.get(KEY_DELIVERY_DESTINATIONS)
        )
        if not selected and not _any_present(supplied, _SLACK_KEYS):
            return None
        return SlackSettings(
            token=_as_secret(resolved, KEY_SLACK_TOKEN),
            channel=_as_str(resolved, KEY_SLACK_CHANNEL),
            api_base_url=_as_str(
                resolved, KEY_SLACK_API_BASE_URL, "https://slack.com/api"
            ),
        )

    @staticmethod
    def _build_notion(
        supplied: Mapping[str, str], resolved: Mapping[str, str]
    ) -> NotionSettings | None:
        selected = DeliveryDestination.NOTION in _as_destinations(
            resolved.get(KEY_DELIVERY_DESTINATIONS)
        )
        if not selected and not _any_present(supplied, _NOTION_KEYS):
            return None
        return NotionSettings(
            token=_as_secret(resolved, KEY_NOTION_TOKEN),
            database_id=_as_str(resolved, KEY_NOTION_DATABASE_ID),
            api_version=_as_str(
                resolved, KEY_NOTION_API_VERSION, "2022-06-28"
            ),
            api_base_url=_as_str(
                resolved, KEY_NOTION_API_BASE_URL, "https://api.notion.com"
            ),
        )

    @staticmethod
    def _build_authorized_channels(
        resolved: Mapping[str, str],
    ) -> tuple[AuthorizedChannel, ...]:
        channel_id = (resolved.get(KEY_OWNED_CHANNEL_ID) or "").strip()
        if not channel_id:
            return ()
        credentials_ref = _as_str(
            resolved, KEY_OWNED_CHANNEL_CREDENTIALS_REF, "youtube_api_key"
        )
        return (
            AuthorizedChannel(
                channel_id=channel_id,
                credentials_ref=credentials_ref,
                connected=False,
            ),
        )


# ---------------------------------------------------------------------------
# Coercion helpers
#
# Each helper is total: it returns a usable value for any input, deferring the
# reporting of missing/malformed values to ConfigLoader.validate (task 2.6).
# `supplied` (sources only) drives "is this present?" decisions; `resolved`
# (sources + defaults) drives value extraction.
# ---------------------------------------------------------------------------


def _present(values: Mapping[str, str], key: str) -> bool:
    """Whether ``key`` is supplied with a non-empty value."""
    return bool((values.get(key) or "").strip())


def _any_present(values: Mapping[str, str], keys: Iterable[str]) -> bool:
    """Whether any of ``keys`` is supplied with a non-empty value."""
    return any(_present(values, key) for key in keys)


def _as_str(values: Mapping[str, str], key: str, default: str = "") -> str:
    """Return the string value for ``key``, or ``default`` when blank/absent."""
    value = values.get(key)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _as_secret(values: Mapping[str, str], key: str) -> Secret:
    """Wrap ``key``'s value in a :class:`Secret` (empty placeholder if absent).

    The :class:`CredentialReference` name is the lower-cased documented key, so
    a missing secret still carries the handle ``validate`` and redaction use.
    """
    raw = (values.get(key) or "").strip()
    return Secret(raw, CredentialReference(key.lower()))


def _as_opt_secret(
    supplied: Mapping[str, str], resolved: Mapping[str, str], key: str
) -> Secret | None:
    """A :class:`Secret` when the key is supplied, else ``None``."""
    if not _present(supplied, key):
        return None
    return _as_secret(resolved, key)


def _as_int(values: Mapping[str, str], key: str, default: int) -> int:
    """Coerce ``key`` to ``int``; fall back to ``default`` when un-coercible."""
    try:
        return int((values.get(key) or "").strip())
    except (TypeError, ValueError):
        return default


def _as_opt_int(
    supplied: Mapping[str, str], resolved: Mapping[str, str], key: str
) -> int | None:
    """An ``int`` when the key is supplied and coercible, else ``None``."""
    if not _present(supplied, key):
        return None
    try:
        return int((resolved.get(key) or "").strip())
    except (TypeError, ValueError):
        return None


def _as_float(values: Mapping[str, str], key: str, default: float) -> float:
    """Coerce ``key`` to ``float``; fall back to ``default`` when un-coercible."""
    try:
        return float((values.get(key) or "").strip())
    except (TypeError, ValueError):
        return default


def _as_category(raw: str | None) -> ChannelCategory | None:
    """Map a raw category value to a supported :class:`ChannelCategory`.

    Blank or unsupported values map to ``None`` (fall back to the channel's
    detected category), matching the documented ``.env`` behavior.
    """
    value = (raw or "").strip().lower()
    if not value:
        return None
    try:
        return ChannelCategory(value)
    except ValueError:
        return None


def _as_csv_tuple(raw: str | None) -> tuple[str, ...]:
    """Split a comma-separated value into a tuple of trimmed, non-empty items."""
    if not raw:
        return ()
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _as_destinations(raw: str | None) -> tuple[DeliveryDestination, ...]:
    """Map a comma-separated value to recognized :class:`DeliveryDestination`s.

    Unrecognized tokens are dropped here (a tolerant load); reporting a malformed
    destination value is left to ``validate``. Order and de-duplication follow
    first appearance.
    """
    if not raw:
        return ()
    destinations: list[DeliveryDestination] = []
    for token in raw.split(","):
        value = token.strip().lower()
        if not value:
            continue
        try:
            destination = DeliveryDestination(value)
        except ValueError:
            continue
        if destination not in destinations:
            destinations.append(destination)
    return tuple(destinations)


def _build_schedule(resolved: Mapping[str, str]) -> Schedule | None:
    """Build a :class:`Schedule` when either schedule field is supplied.

    Returns ``None`` when neither an interval nor a run time is configured
    (manual-only operation). Schedule completeness is evaluated downstream by the
    composition root, not here.
    """
    interval = (resolved.get(KEY_SCHEDULE_INTERVAL) or "").strip() or None
    run_time = (resolved.get(KEY_SCHEDULE_RUN_TIME) or "").strip() or None
    if interval is None and run_time is None:
        return None
    return Schedule(recurrence_interval=interval, run_time=run_time)


# ---------------------------------------------------------------------------
# Validation helpers (Requirement 11)
#
# Each helper inspects one already-mapped Settings field and appends a
# ConfigProblem (key + expected-form description, never a value) when the field
# is missing or malformed. "missing" means absent/empty; "malformed: <form>"
# means present but not conforming to the documented type/format. Secret values
# are inspected only via reveal() to test emptiness and are never placed in a
# problem (11.5, 12.5).
# ---------------------------------------------------------------------------

# A pragmatic e-mail address shape: non-empty local part, "@", a dotted domain.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _missing(key: str, problems: list[ConfigProblem]) -> None:
    """Record ``key`` as a missing (absent/empty) required value."""
    problems.append(ConfigProblem(key=key, issue="missing"))


def _malformed(key: str, expected: str, problems: list[ConfigProblem]) -> None:
    """Record ``key`` as present but not matching the documented ``expected`` form."""
    problems.append(ConfigProblem(key=key, issue=f"malformed: {expected}"))


def _require_str(value: str, key: str, problems: list[ConfigProblem]) -> None:
    """Require a non-empty string value."""
    if not (value or "").strip():
        _missing(key, problems)


def _require_secret(secret: Secret, key: str, problems: list[ConfigProblem]) -> None:
    """Require a :class:`Secret` whose revealed value is non-empty.

    The value is read only to test emptiness; it is never placed in the report
    (11.5, 12.5).
    """
    if not secret.reveal().strip():
        _missing(key, problems)


def _require_url(value: str, key: str, problems: list[ConfigProblem]) -> None:
    """Require a non-empty value that looks like an ``http(s)`` URL."""
    text = (value or "").strip()
    if not text:
        _missing(key, problems)
    elif not (text.startswith("http://") or text.startswith("https://")):
        _malformed(key, "an http(s) URL", problems)


def _require_email(value: str, key: str, problems: list[ConfigProblem]) -> None:
    """Require a non-empty value shaped like an e-mail address."""
    text = (value or "").strip()
    if not text:
        _missing(key, problems)
    elif not _EMAIL_RE.match(text):
        _malformed(key, "an e-mail address", problems)


def _require_positive_number(
    value: float, key: str, problems: list[ConfigProblem]
) -> None:
    """Require a finite number strictly greater than zero (timeouts)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _malformed(key, "a positive number", problems)
    elif not math.isfinite(value) or value <= 0:
        _malformed(key, "a positive number", problems)


def _require_positive_int(value: int, key: str, problems: list[ConfigProblem]) -> None:
    """Require an integer strictly greater than zero."""
    if not isinstance(value, int) or isinstance(value, bool):
        _malformed(key, "a positive integer", problems)
    elif value <= 0:
        _malformed(key, "a positive integer", problems)


def _require_port(value: int, key: str, problems: list[ConfigProblem]) -> None:
    """Require an integer TCP port in the range 1-65535."""
    if not isinstance(value, int) or isinstance(value, bool):
        _malformed(key, "a port in 1-65535", problems)
    elif not (1 <= value <= 65535):
        _malformed(key, "a port in 1-65535", problems)


def _validate_email(email: EmailSettings | None, problems: list[ConfigProblem]) -> None:
    """Validate the e-mail destination (Requirement 7) when it is selected.

    A selected destination with no settings group reports every required key as
    missing, so the Creator sees the full set of values to supply (11.3).
    """
    if email is None:
        for key in _EMAIL_KEYS + (KEY_SMTP_PORT,):
            _missing(key, problems)
        return
    _require_str(email.host, KEY_SMTP_HOST, problems)
    _require_port(email.port, KEY_SMTP_PORT, problems)
    _require_str(email.username, KEY_SMTP_USERNAME, problems)
    _require_secret(email.password, KEY_SMTP_PASSWORD, problems)
    _require_email(email.sender, KEY_EMAIL_SENDER, problems)
    _require_email(email.recipient, KEY_EMAIL_RECIPIENT, problems)


def _validate_slack(slack: SlackSettings | None, problems: list[ConfigProblem]) -> None:
    """Validate the Slack destination (Requirement 8) when it is selected."""
    if slack is None:
        for key in (KEY_SLACK_TOKEN, KEY_SLACK_CHANNEL, KEY_SLACK_API_BASE_URL):
            _missing(key, problems)
        return
    _require_secret(slack.token, KEY_SLACK_TOKEN, problems)
    _require_str(slack.channel, KEY_SLACK_CHANNEL, problems)
    _require_url(slack.api_base_url, KEY_SLACK_API_BASE_URL, problems)


def _validate_notion(
    notion: NotionSettings | None, problems: list[ConfigProblem]
) -> None:
    """Validate the Notion destination (Requirement 9) when it is selected."""
    if notion is None:
        for key in (
            KEY_NOTION_TOKEN,
            KEY_NOTION_DATABASE_ID,
            KEY_NOTION_API_VERSION,
            KEY_NOTION_API_BASE_URL,
        ):
            _missing(key, problems)
        return
    _require_secret(notion.token, KEY_NOTION_TOKEN, problems)
    _require_str(notion.database_id, KEY_NOTION_DATABASE_ID, problems)
    _require_str(notion.api_version, KEY_NOTION_API_VERSION, problems)
    _require_url(notion.api_base_url, KEY_NOTION_API_BASE_URL, problems)
