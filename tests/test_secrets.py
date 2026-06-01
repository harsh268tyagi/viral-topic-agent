"""Tests for the secret-protection primitives (task 1.5).

Covers :class:`CredentialReference`, :class:`Secret`, and the module-level
:func:`redact` helper in ``config/secrets.py``.

These tests pin the guarantees of Requirement 12: a ``Secret`` never exposes
its value through ordinary rendering (``repr``/``str``/f-strings, 12.1), the
value is reachable only through the explicit :meth:`Secret.reveal` call, and
:func:`redact` scrubs any secret value that might otherwise appear in a
free-form reason string (12.3).
"""

import pytest

from config.secrets import CredentialReference, Secret, redact


# ---------------------------------------------------------------------------
# CredentialReference
# ---------------------------------------------------------------------------


def test_credential_reference_exposes_its_name():
    ref = CredentialReference("youtube_api_key")
    assert ref.name == "youtube_api_key"


def test_credential_reference_is_frozen():
    ref = CredentialReference("smtp_password")
    with pytest.raises(Exception):
        ref.name = "tampered"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Secret — value never leaks through ordinary rendering (12.1)
# ---------------------------------------------------------------------------


def test_secret_repr_renders_placeholder_not_value():
    secret = Secret("super-secret-token", CredentialReference("slack_token"))
    assert repr(secret) == "<Secret slack_token>"
    assert "super-secret-token" not in repr(secret)


def test_secret_str_renders_placeholder_not_value():
    secret = Secret("super-secret-token", CredentialReference("slack_token"))
    assert str(secret) == "<Secret slack_token>"
    assert "super-secret-token" not in str(secret)


def test_secret_fstring_interpolation_renders_placeholder_not_value():
    secret = Secret("super-secret-token", CredentialReference("slack_token"))
    rendered = f"using {secret} for delivery"
    assert rendered == "using <Secret slack_token> for delivery"
    assert "super-secret-token" not in rendered


def test_secret_format_renders_placeholder_not_value():
    secret = Secret("super-secret-token", CredentialReference("notion_token"))
    assert "{}".format(secret) == "<Secret notion_token>"
    assert "super-secret-token" not in "{}".format(secret)


def test_secret_placeholder_uses_the_reference_name():
    secret = Secret("value-a", CredentialReference("oauth_refresh_token"))
    assert repr(secret) == "<Secret oauth_refresh_token>"


# ---------------------------------------------------------------------------
# Secret — value reachable only through reveal()
# ---------------------------------------------------------------------------


def test_secret_reveal_returns_the_actual_value_explicitly():
    secret = Secret("super-secret-token", CredentialReference("slack_token"))
    assert secret.reveal() == "super-secret-token"


def test_secret_reference_property_returns_the_reference():
    ref = CredentialReference("smtp_password")
    secret = Secret("hunter2", ref)
    assert secret.reference is ref
    assert secret.reference.name == "smtp_password"


def test_secret_reveal_preserves_empty_value():
    secret = Secret("", CredentialReference("unset_key"))
    assert secret.reveal() == ""


# ---------------------------------------------------------------------------
# redact() — scrubs secret values from free-form text (12.3)
# ---------------------------------------------------------------------------


def test_redact_replaces_secret_value_with_placeholder():
    secret = Secret("abc123", CredentialReference("youtube_api_key"))
    text = "request failed with key abc123 rejected"
    assert redact(text, [secret]) == (
        "request failed with key <Secret youtube_api_key> rejected"
    )
    assert "abc123" not in redact(text, [secret])


def test_redact_replaces_every_occurrence_of_a_secret_value():
    secret = Secret("abc123", CredentialReference("youtube_api_key"))
    text = "abc123 then abc123 again"
    result = redact(text, [secret])
    assert result == "<Secret youtube_api_key> then <Secret youtube_api_key> again"
    assert "abc123" not in result


def test_redact_handles_multiple_distinct_secrets():
    api_key = Secret("abc123", CredentialReference("youtube_api_key"))
    password = Secret("hunter2", CredentialReference("smtp_password"))
    text = "key=abc123 password=hunter2"
    result = redact(text, [api_key, password])
    assert result == "key=<Secret youtube_api_key> password=<Secret smtp_password>"
    assert "abc123" not in result
    assert "hunter2" not in result


def test_redact_replaces_longer_value_first_for_overlapping_substrings():
    # The short secret's value ("secret") is a substring of the long secret's
    # value ("secretvalue123").
    short = Secret("secret", CredentialReference("short_cred"))
    long = Secret("secretvalue123", CredentialReference("long_cred"))
    text = "auth secretvalue123 used"
    # Pass the short secret first to prove ordering is by value length, not by
    # input order: the longer value is replaced first so it wins its full span
    # and its raw value never appears (had the short value gone first it would
    # have shredded the longer value into "<...>value123").
    result = redact(text, [short, long])
    assert "secretvalue123" not in result
    assert result == "auth <Secret long_cred> used"


def test_redact_ignores_empty_secret_values():
    empty = Secret("", CredentialReference("unset_key"))
    text = "nothing to redact here"
    # An empty value must not match between every character.
    assert redact(text, [empty]) == "nothing to redact here"


def test_redact_ignores_empty_secret_but_still_redacts_real_ones():
    empty = Secret("", CredentialReference("unset_key"))
    real = Secret("abc123", CredentialReference("youtube_api_key"))
    text = "key abc123 here"
    result = redact(text, [empty, real])
    assert result == "key <Secret youtube_api_key> here"
    assert "abc123" not in result


def test_redact_leaves_text_unchanged_when_no_secret_value_present():
    secret = Secret("abc123", CredentialReference("youtube_api_key"))
    text = "no credential appears in this message"
    assert redact(text, [secret]) == text


def test_redact_with_no_secrets_returns_text_unchanged():
    assert redact("some reason string", []) == "some reason string"
