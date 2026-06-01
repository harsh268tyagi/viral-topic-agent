"""Unit tests for ConfigLoader field mapping and the validation happy path (task 2.8).

These are example-based unit tests (the precedence and validation *property*
tests live in ``test_config_precedence_properties.py`` (task 2.5) and
``test_config_validation_properties.py`` (task 2.7); this module does not
duplicate them). They pin two concrete behaviors of
``src/config/config_loader.py``:

(a) **Field mapping (Requirement 10.5).** For each documented ``KEY_*``
    configuration key, a value supplied through a real
    :class:`~config.sources.ConfigurationSource` is mapped to the correct
    :class:`~config.settings.Settings` field (secrets revealed, numerics
    coerced, comma-separated values split into the right value-objects).

(b) **Validation happy path (Requirement 11 — valid config allows the run).**
    A *complete* valid configuration — every always-on required value plus a
    selected delivery destination fully configured — produces a ``Settings``
    for which ``ConfigLoader.validate(settings).ok`` is ``True``, confirming the
    Composition_Root is allowed to run.

All inputs are injected through the real ``OverridesSource`` wired into a real
``ConfigLoader``; no environment or file system is touched.
"""

from __future__ import annotations

from config.config_loader import (
    KEY_COMPETITORS,
    KEY_DELIVERY_DESTINATIONS,
    KEY_EMAIL_RECIPIENT,
    KEY_EMAIL_SENDER,
    KEY_KEYWORD_SOURCE_API_BASE_URL,
    KEY_KEYWORD_SOURCE_API_KEY,
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS,
    KEY_LLM_TIMEOUT_SECONDS,
    KEY_NOTION_API_BASE_URL,
    KEY_NOTION_API_VERSION,
    KEY_NOTION_DATABASE_ID,
    KEY_NOTION_TOKEN,
    KEY_OAUTH_ACCESS_TOKEN,
    KEY_OAUTH_CLIENT_ID,
    KEY_OAUTH_CLIENT_SECRET,
    KEY_OAUTH_REFRESH_TOKEN,
    KEY_OWNED_CHANNEL_CREDENTIALS_REF,
    KEY_OWNED_CHANNEL_ID,
    KEY_REQUEST_TIMEOUT_SECONDS,
    KEY_SCHEDULE_INTERVAL,
    KEY_SCHEDULE_RUN_TIME,
    KEY_SELECTED_CATEGORY,
    KEY_SLACK_API_BASE_URL,
    KEY_SLACK_CHANNEL,
    KEY_SLACK_TOKEN,
    KEY_SMTP_HOST,
    KEY_SMTP_PASSWORD,
    KEY_SMTP_PORT,
    KEY_SMTP_USERNAME,
    KEY_TEMPLATE_STRATEGY,
    KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS,
    KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE,
    KEY_YOUTUBE_API_KEY,
    ConfigLoader,
)
from config.sources import OverridesSource
from domain.models import ChannelCategory, DeliveryDestination


def _loader(overrides: dict[str, str]) -> ConfigLoader:
    """A ConfigLoader fed by a single real OverridesSource (no env/file I/O)."""
    return ConfigLoader([OverridesSource(overrides)])


# A complete set of documented keys with pairwise-distinct, well-formed values,
# so a misrouted value would be detectable. Comma-separated and numeric keys use
# values that exercise the documented coercion for their field.
_ALL_KEYS: dict[str, str] = {
    # Authentication (Requirement 13)
    KEY_YOUTUBE_API_KEY: "yt-data-api-key",
    KEY_OAUTH_CLIENT_ID: "oauth-client-id",
    KEY_OAUTH_CLIENT_SECRET: "oauth-client-secret",
    KEY_OAUTH_REFRESH_TOKEN: "oauth-refresh-token",
    KEY_OAUTH_ACCESS_TOKEN: "oauth-access-token",
    # Timeouts
    KEY_LLM_TIMEOUT_SECONDS: "45",
    KEY_REQUEST_TIMEOUT_SECONDS: "20",
    # Optional keyword-metrics source (Requirement 4)
    KEY_KEYWORD_SOURCE_API_BASE_URL: "https://keywords.example.com",
    KEY_KEYWORD_SOURCE_API_KEY: "keyword-api-key",
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS: "10",
    # Optional template-performance strategy (Requirement 5)
    KEY_TEMPLATE_STRATEGY: "median",
    KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE: "7",
    KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS: "30",
    # Email delivery (Requirement 7)
    KEY_SMTP_HOST: "smtp.example.com",
    KEY_SMTP_PORT: "2525",
    KEY_SMTP_USERNAME: "smtp-user",
    KEY_SMTP_PASSWORD: "smtp-password",
    KEY_EMAIL_SENDER: "agent@example.com",
    KEY_EMAIL_RECIPIENT: "creator@example.com",
    # Slack delivery (Requirement 8)
    KEY_SLACK_TOKEN: "slack-token",
    KEY_SLACK_CHANNEL: "#digests",
    KEY_SLACK_API_BASE_URL: "https://slack.test/api",
    # Notion delivery (Requirement 9)
    KEY_NOTION_TOKEN: "notion-token",
    KEY_NOTION_DATABASE_ID: "notion-db-123",
    KEY_NOTION_API_VERSION: "2022-11-28",
    KEY_NOTION_API_BASE_URL: "https://notion.test",
    # Domain Configuration fields (Requirement 14.3)
    KEY_OWNED_CHANNEL_ID: "UC_owned_channel",
    KEY_OWNED_CHANNEL_CREDENTIALS_REF: "owned-creds-ref",
    KEY_SELECTED_CATEGORY: "gaming",
    KEY_COMPETITORS: "UC_compA,UC_compB",
    KEY_SCHEDULE_INTERVAL: "daily",
    KEY_SCHEDULE_RUN_TIME: "08:00",
    KEY_DELIVERY_DESTINATIONS: "email,slack,notion",
}


# ---------------------------------------------------------------------------
# (a) Field mapping — documented keys map to the correct Settings fields (10.5)
# ---------------------------------------------------------------------------


def test_auth_keys_map_to_auth_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.auth.youtube_api_key.reveal() == "yt-data-api-key"
    # Any OAuth key present enables the OAuth group (Analytics authorized).
    assert settings.auth.oauth is not None
    assert settings.auth.oauth.client_id == "oauth-client-id"
    assert settings.auth.oauth.client_secret.reveal() == "oauth-client-secret"
    assert settings.auth.oauth.refresh_token is not None
    assert settings.auth.oauth.refresh_token.reveal() == "oauth-refresh-token"
    assert settings.auth.oauth.access_token is not None
    assert settings.auth.oauth.access_token.reveal() == "oauth-access-token"


def test_timeout_keys_map_to_float_fields():
    settings = _loader(_ALL_KEYS).load()
    assert settings.llm_timeout_seconds == 45.0
    assert settings.request_timeout_seconds == 20.0


def test_keyword_source_keys_map_to_keyword_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.keyword_source is not None
    assert settings.keyword_source.api_base_url == "https://keywords.example.com"
    assert settings.keyword_source.api_key is not None
    assert settings.keyword_source.api_key.reveal() == "keyword-api-key"
    assert settings.keyword_source.max_keywords == 10


def test_template_strategy_keys_map_to_template_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.template_strategy is not None
    assert settings.template_strategy.strategy == "median"
    assert settings.template_strategy.min_sample_size == 7
    assert settings.template_strategy.lookback_days == 30


def test_email_keys_map_to_email_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.email is not None
    assert settings.email.host == "smtp.example.com"
    assert settings.email.port == 2525
    assert settings.email.username == "smtp-user"
    assert settings.email.password.reveal() == "smtp-password"
    assert settings.email.sender == "agent@example.com"
    assert settings.email.recipient == "creator@example.com"


def test_slack_keys_map_to_slack_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.slack is not None
    assert settings.slack.token.reveal() == "slack-token"
    assert settings.slack.channel == "#digests"
    assert settings.slack.api_base_url == "https://slack.test/api"


def test_notion_keys_map_to_notion_settings():
    settings = _loader(_ALL_KEYS).load()
    assert settings.notion is not None
    assert settings.notion.token.reveal() == "notion-token"
    assert settings.notion.database_id == "notion-db-123"
    assert settings.notion.api_version == "2022-11-28"
    assert settings.notion.api_base_url == "https://notion.test"


def test_owned_channel_keys_map_to_authorized_channel():
    settings = _loader(_ALL_KEYS).load()
    assert len(settings.authorized_channels) == 1
    channel = settings.authorized_channels[0]
    assert channel.channel_id == "UC_owned_channel"
    assert channel.credentials_ref == "owned-creds-ref"


def test_selected_category_key_maps_to_channel_category():
    settings = _loader(_ALL_KEYS).load()
    assert settings.selected_category is ChannelCategory.GAMING


def test_competitors_key_maps_to_trimmed_tuple():
    settings = _loader(_ALL_KEYS).load()
    assert settings.monitored_competitors == ("UC_compA", "UC_compB")


def test_schedule_keys_map_to_schedule():
    settings = _loader(_ALL_KEYS).load()
    assert settings.schedule is not None
    assert settings.schedule.recurrence_interval == "daily"
    assert settings.schedule.run_time == "08:00"


def test_delivery_destinations_key_maps_to_destination_tuple():
    settings = _loader(_ALL_KEYS).load()
    assert settings.delivery_destinations == (
        DeliveryDestination.EMAIL,
        DeliveryDestination.SLACK,
        DeliveryDestination.NOTION,
    )


def test_secret_fields_do_not_leak_value_when_rendered():
    # The mapped Secret fields render redacted, never the raw value (Req. 12).
    settings = _loader(_ALL_KEYS).load()
    assert "yt-data-api-key" not in repr(settings.auth.youtube_api_key)
    assert "smtp-password" not in str(settings.email.password)


# ---------------------------------------------------------------------------
# (b) Validation happy path — a complete valid config allows the run (Req. 11)
# ---------------------------------------------------------------------------


# Minimal complete-and-valid config: the always-on data-source/LLM values plus a
# single fully-configured, selected delivery destination (email). Unselected
# destinations and unconfigured optional sources are not required (11.4).
_COMPLETE_EMAIL_CONFIG: dict[str, str] = {
    KEY_YOUTUBE_API_KEY: "yt-data-api-key",
    KEY_LLM_TIMEOUT_SECONDS: "60",
    KEY_REQUEST_TIMEOUT_SECONDS: "30",
    KEY_DELIVERY_DESTINATIONS: "email",
    KEY_SMTP_HOST: "smtp.example.com",
    KEY_SMTP_PORT: "587",
    KEY_SMTP_USERNAME: "smtp-user",
    KEY_SMTP_PASSWORD: "smtp-password",
    KEY_EMAIL_SENDER: "agent@example.com",
    KEY_EMAIL_RECIPIENT: "creator@example.com",
}


def test_complete_valid_config_validates_and_allows_the_run():
    loader = _loader(_COMPLETE_EMAIL_CONFIG)
    settings = loader.load()
    report = loader.validate(settings)
    # No problems reported -> the Composition_Root is allowed to run.
    assert report.problems == ()
    assert report.ok is True


def test_complete_valid_config_relies_on_documented_timeout_defaults():
    # Omitting the timeout keys still validates because their documented defaults
    # (60 / 30) are well-formed positive numbers (10.3, 11.1).
    config = {
        key: value
        for key, value in _COMPLETE_EMAIL_CONFIG.items()
        if key not in (KEY_LLM_TIMEOUT_SECONDS, KEY_REQUEST_TIMEOUT_SECONDS)
    }
    loader = _loader(config)
    report = loader.validate(loader.load())
    assert report.ok is True


def test_complete_valid_config_with_all_destinations_validates():
    # A complete config that selects and fully configures every destination also
    # validates, confirming the run is allowed across the full selection (11.3).
    loader = _loader(_ALL_KEYS)
    report = loader.validate(loader.load())
    assert report.ok is True


def test_unselected_destination_values_are_not_required():
    # Only email is selected, so absent Slack/Notion values do not block the run
    # (11.4): the run is still allowed.
    loader = _loader(_COMPLETE_EMAIL_CONFIG)
    settings = loader.load()
    assert settings.slack is None
    assert settings.notion is None
    assert loader.validate(settings).ok is True
