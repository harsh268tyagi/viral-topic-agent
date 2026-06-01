"""Hypothesis property test for startup validation (task 2.7).

This module validates a single universal property of ``ConfigLoader.validate``
(``src/config/config_loader.py``), the startup-readiness gate described in
``.kiro/specs/real-provider-integration/design.md`` (-> *ConfigLoader, Settings,
ConfigurationSource* and *Startup flow*):

- Property 16 (11.1, 11.2, 11.3, 11.4, 12.5): startup validation reports exactly
  the missing or malformed required keys and blocks all external requests. For
  any configuration in which a subset of the required keys for the *enabled*
  components and *selected* delivery destinations is missing or malformed,
  ``ConfigLoader.validate`` produces a report whose identified keys are exactly
  that subset (treating the keys of unselected delivery destinations as not
  required), prevents the ``AutomationScheduler`` from running, and causes no
  external request to be issued.

How the property is exercised
-----------------------------
Each example starts from a *complete, valid* configuration assembled through the
real :class:`~config.sources.OverridesSource` wired into a real
:class:`~config.config_loader.ConfigLoader` (no environment or file-system I/O,
no network). The scenario generator then:

1. Randomly enables the always-on component (data source / LLM / transports) plus
   an arbitrary subset of the optional components (OAuth, keyword source,
   template strategy) and an arbitrary subset of the three delivery destinations.
2. For each *required* key of each enabled component / selected destination,
   randomly decides whether to corrupt it and, if so, how -- either *missing*
   (emptied, for keys with no documented default) or *malformed* (a present but
   ill-formed value, e.g. a non-positive timeout, an out-of-range port, a
   non-URL, or a non-e-mail address). The expected report is folded
   independently of ``validate`` from exactly the keys it corrupts.
3. Optionally injects an *invalid* configuration for an **unselected**
   destination, which ``validate`` must ignore entirely (11.4).

The generator is constrained to ``validate``'s input space: corruptions are
chosen per key from value-shapes the documented coercion/validation actually
treats as missing or malformed (a key carrying a documented default cannot be
made *missing* by removal, so such keys are only corrupted to a present-but-
malformed value), and the optional components are kept *enabled* by retaining a
non-required anchor value (an OAuth token, the keyword API key, the template
look-back) so corrupting their required keys never silently disables the group.

The property asserts the reported key set equals the corrupted subset exactly,
that each problem's issue kind matches the injected corruption, that no
:class:`~config.secrets.Secret` value appears in the report (11.5, 12.5), and
that the report is *not ok* exactly when a required value is missing/malformed --
the single signal the ``CompositionRoot`` keys off to refuse to construct
anything, run the scheduler, or issue any external request (11.2). ``validate``
holds no transport, so reporting provably precedes any I/O.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from config.config_loader import (
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
    KEY_REQUEST_TIMEOUT_SECONDS,
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

# ---------------------------------------------------------------------------
# Base valid values for every component (so an uncorrupted enabled component
# validates cleanly and only the deliberately-corrupted keys are ever reported).
# ---------------------------------------------------------------------------

# Always-on: the public Data API key plus the request/LLM timeouts that back the
# data source, LLM provider, and HTTP transports (always required, 11.1).
_BASE_ALWAYS_ON: dict[str, str] = {
    KEY_YOUTUBE_API_KEY: "yt-data-api-key",
    KEY_LLM_TIMEOUT_SECONDS: "45",
    KEY_REQUEST_TIMEOUT_SECONDS: "20",
}

# OAuth (Analytics authorized). The access/refresh tokens are NOT required by
# validate but are kept present as anchors so emptying client_id/client_secret
# never disables the group (which is enabled by ANY OAuth key being present).
_BASE_OAUTH: dict[str, str] = {
    KEY_OAUTH_CLIENT_ID: "oauth-client-id",
    KEY_OAUTH_CLIENT_SECRET: "oauth-client-secret",
    KEY_OAUTH_ACCESS_TOKEN: "oauth-access-token",
    KEY_OAUTH_REFRESH_TOKEN: "oauth-refresh-token",
}

# Optional keyword-metrics source. The API key is an unvalidated anchor that
# keeps the group enabled even when its required keys are corrupted.
_BASE_KEYWORD: dict[str, str] = {
    KEY_KEYWORD_SOURCE_API_BASE_URL: "https://keywords.example.com",
    KEY_KEYWORD_SOURCE_API_KEY: "keyword-api-key",
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS: "10",
}

# Optional template-performance strategy. The look-back is an unvalidated-when-
# valid anchor (a positive int) keeping the group enabled.
_BASE_TEMPLATE: dict[str, str] = {
    KEY_TEMPLATE_STRATEGY: "median",
    KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE: "7",
    KEY_TEMPLATE_STRATEGY_LOOKBACK_DAYS: "30",
}

_BASE_EMAIL: dict[str, str] = {
    KEY_SMTP_HOST: "smtp.example.com",
    KEY_SMTP_PORT: "2525",
    KEY_SMTP_USERNAME: "smtp-user",
    KEY_SMTP_PASSWORD: "smtp-password",
    KEY_EMAIL_SENDER: "agent@example.com",
    KEY_EMAIL_RECIPIENT: "creator@example.com",
}

_BASE_SLACK: dict[str, str] = {
    KEY_SLACK_TOKEN: "slack-token",
    KEY_SLACK_CHANNEL: "#digests",
    KEY_SLACK_API_BASE_URL: "https://slack.test/api",
}

_BASE_NOTION: dict[str, str] = {
    KEY_NOTION_TOKEN: "notion-token",
    KEY_NOTION_DATABASE_ID: "notion-db-123",
    KEY_NOTION_API_VERSION: "2022-11-28",
    KEY_NOTION_API_BASE_URL: "https://notion.test",
}

# Secret base values that must never surface in a validation report (11.5, 12.5).
_SECRET_VALUES: tuple[str, ...] = (
    "yt-data-api-key",
    "oauth-client-secret",
    "oauth-access-token",
    "oauth-refresh-token",
    "keyword-api-key",
    "smtp-password",
    "slack-token",
    "notion-token",
)


# ---------------------------------------------------------------------------
# Corruptible required keys per component, each with the value-shapes that the
# documented coercion/validation treats as exactly that key's problem.
#
# A corruption is ("missing", value) or ("malformed", value):
#   - "missing"   : the value loads as absent/empty -> reported "missing". Only
#                   keys WITHOUT a documented default support this (a defaulted
#                   key falls back to its default and is never missing).
#   - "malformed" : a present but ill-formed value -> reported "malformed: ...".
#                   Used for keys with a format/range check (timeouts, port,
#                   URLs, e-mails, positive ints). The value stays present so the
#                   component remains enabled.
# Keys with a documented default AND only a non-empty check (e.g.
# NOTION_API_VERSION) cannot be made missing or malformed and so are not listed.
# ---------------------------------------------------------------------------

_Corruptions = list[tuple[str, str]]
_Candidate = tuple[str, _Corruptions]

_ALWAYS_ON_CANDIDATES: list[_Candidate] = [
    (KEY_YOUTUBE_API_KEY, [("missing", "")]),
    (KEY_LLM_TIMEOUT_SECONDS, [("malformed", "0"), ("malformed", "-1"), ("malformed", "-5")]),
    (KEY_REQUEST_TIMEOUT_SECONDS, [("malformed", "0"), ("malformed", "-2")]),
]

_OAUTH_CANDIDATES: list[_Candidate] = [
    (KEY_OAUTH_CLIENT_ID, [("missing", "")]),
    (KEY_OAUTH_CLIENT_SECRET, [("missing", "")]),
]

_KEYWORD_CANDIDATES: list[_Candidate] = [
    (
        KEY_KEYWORD_SOURCE_API_BASE_URL,
        [("missing", ""), ("malformed", "not-a-url"), ("malformed", "example.com")],
    ),
    (KEY_KEYWORD_SOURCE_MAX_KEYWORDS, [("malformed", "0"), ("malformed", "-1")]),
]

_TEMPLATE_CANDIDATES: list[_Candidate] = [
    (KEY_TEMPLATE_STRATEGY, [("missing", "")]),
    (KEY_TEMPLATE_STRATEGY_MIN_SAMPLE_SIZE, [("malformed", "0"), ("malformed", "-2")]),
]

_EMAIL_CANDIDATES: list[_Candidate] = [
    (KEY_SMTP_HOST, [("missing", "")]),
    (KEY_SMTP_PORT, [("malformed", "0"), ("malformed", "70000"), ("malformed", "-1")]),
    (KEY_SMTP_USERNAME, [("missing", "")]),
    (KEY_SMTP_PASSWORD, [("missing", "")]),
    (KEY_EMAIL_SENDER, [("missing", ""), ("malformed", "notanemail"), ("malformed", "a@b")]),
    (KEY_EMAIL_RECIPIENT, [("missing", ""), ("malformed", "no-at"), ("malformed", "a@b")]),
]

_SLACK_CANDIDATES: list[_Candidate] = [
    (KEY_SLACK_TOKEN, [("missing", "")]),
    (KEY_SLACK_CHANNEL, [("missing", "")]),
    (KEY_SLACK_API_BASE_URL, [("malformed", "not-a-url"), ("malformed", "example.com")]),
]

_NOTION_CANDIDATES: list[_Candidate] = [
    (KEY_NOTION_TOKEN, [("missing", "")]),
    (KEY_NOTION_DATABASE_ID, [("missing", "")]),
    (KEY_NOTION_API_BASE_URL, [("malformed", "not-a-url"), ("malformed", "example.com")]),
]

# Destination name -> (base values, corruptible candidates), used both for
# selected destinations and for the unselected-destination ignore check (11.4).
_DESTINATIONS: dict[str, tuple[dict[str, str], list[_Candidate]]] = {
    "email": (_BASE_EMAIL, _EMAIL_CANDIDATES),
    "slack": (_BASE_SLACK, _SLACK_CANDIDATES),
    "notion": (_BASE_NOTION, _NOTION_CANDIDATES),
}


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[dict[str, str], dict[str, str]]:
    """Draw ``(overrides, expected)``.

    ``overrides`` is the raw key->value mapping fed to a real ``OverridesSource``;
    ``expected`` maps each deliberately-corrupted *required* key to its
    corruption kind (``"missing"`` / ``"malformed"``). Only enabled components and
    selected destinations contribute required keys.
    """
    overrides: dict[str, str] = dict(_BASE_ALWAYS_ON)
    expected: dict[str, str] = {}

    def corrupt_some(candidates: list[_Candidate]) -> None:
        """For each required key, maybe replace its value with a corruption."""
        for key, options in candidates:
            if draw(st.booleans()):
                kind, value = draw(st.sampled_from(options))
                overrides[key] = value
                expected[key] = kind

    # Always-on component is always required.
    corrupt_some(_ALWAYS_ON_CANDIDATES)

    # Optional components: enable an arbitrary subset; corrupt a subset of each.
    if draw(st.booleans()):
        overrides.update(_BASE_OAUTH)
        corrupt_some(_OAUTH_CANDIDATES)
    if draw(st.booleans()):
        overrides.update(_BASE_KEYWORD)
        corrupt_some(_KEYWORD_CANDIDATES)
    if draw(st.booleans()):
        overrides.update(_BASE_TEMPLATE)
        corrupt_some(_TEMPLATE_CANDIDATES)

    # Delivery destinations: an arbitrary subset is selected (possibly none).
    selected = draw(
        st.lists(st.sampled_from(["email", "slack", "notion"]), unique=True, max_size=3)
    )
    overrides[KEY_DELIVERY_DESTINATIONS] = ",".join(selected)
    for name in selected:
        base, candidates = _DESTINATIONS[name]
        overrides.update(base)
        corrupt_some(candidates)

    # Unselected destinations: sometimes supply an INVALID config that validate
    # must ignore (11.4). The base values keep the group enabled (so it would be
    # validated if it were selected); the extra corruption is never expected.
    for name, (base, candidates) in _DESTINATIONS.items():
        if name not in selected and draw(st.booleans()):
            overrides.update(base)
            key, options = draw(st.sampled_from(candidates))
            _kind, value = draw(st.sampled_from(options))
            overrides[key] = value

    return overrides, expected


# ---------------------------------------------------------------------------
# Property 16
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 16: Startup validation reports exactly the missing or malformed required keys and blocks all external requests
# Validates: Requirements 11.1, 11.2, 11.3, 11.4, 12.5
@settings(max_examples=200)
@given(scenario=_scenario())
def test_startup_validation_reports_exactly_the_missing_or_malformed_required_keys(
    scenario,
):
    """For any configuration with a subset of required keys missing/malformed,
    ``validate`` reports exactly that subset (unselected destinations not
    required), excludes every secret value, and is *not ok* iff a required value
    is missing/malformed -- the signal that blocks the scheduler and any external
    request (11.1, 11.2, 11.3, 11.4, 12.5)."""
    overrides, expected = scenario

    loader = ConfigLoader([OverridesSource(overrides)])
    report = loader.validate(loader.load())

    reported = {problem.key for problem in report.problems}

    # Each problematic key is reported at most once.
    assert len(report.problems) == len(reported)

    # Exactly the corrupted required keys are reported: nothing missed, nothing
    # spurious, and no key from an unselected destination (11.1, 11.2, 11.3, 11.4).
    assert reported == set(expected)

    # Each problem's issue kind matches the corruption that produced it.
    for problem in report.problems:
        if expected[problem.key] == "missing":
            assert problem.issue == "missing"
        else:
            assert problem.issue.startswith("malformed")

    # No secret value appears anywhere in the report (11.5, 12.5): problems carry
    # documented keys and expected-form descriptions only, never values.
    for problem in report.problems:
        rendered = f"{problem.key} {problem.issue}"
        for secret_value in _SECRET_VALUES:
            assert secret_value not in rendered

    # The report blocks the run exactly when something is missing/malformed. A
    # clean config (no corruption) is the only case that allows the scheduler to
    # run; otherwise the CompositionRoot aborts before constructing anything or
    # issuing any external request. ``validate`` holds no transport, so this
    # report provably precedes any I/O (11.2).
    assert report.ok == (len(expected) == 0)
