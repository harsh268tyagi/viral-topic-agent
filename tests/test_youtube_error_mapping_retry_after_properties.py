"""Hypothesis property test for Retry-After handling (task 4.3).

This module validates a single universal property of the pure YouTube error
classifier ``classify_response`` (``src/infrastructure/youtube_error_mapping.py``):

- Property 6 (2.3, 2.4): a rate-limit response's ``retry_after_seconds`` is set
  to the header's value exactly when the response carries a numeric
  ``Retry-After`` seconds value, and is left unset otherwise.

The ordered-mapping property (Property 5) is covered by its own module; this one
is scoped only to rate-limit responses and the Retry-After dichotomy.

Every example is built as an in-memory :class:`HttpResponse` (no real network
access, 16.3). Generators are constrained to the input space the property is
scoped to: rate-limit responses (HTTP 429 or HTTP 403 with a quota/rate reason)
carrying a Retry-After header that is absent, a clearly-numeric seconds value,
or clearly non-numeric text. The expected ``retry_after_seconds`` is derived
independently of the implementation from the generated header text.
"""

from __future__ import annotations

import json
import string

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from config.secrets import CredentialReference, Secret
from infrastructure.datasource import RateLimitError
from infrastructure.http_transport import HttpResponse
from infrastructure.youtube_error_mapping import classify_response


# ---------------------------------------------------------------------------
# Helpers / generators
# ---------------------------------------------------------------------------


def _is_floatable(text: str) -> bool:
    """True if ``text`` parses as a Python float (covers ints, decimals, nan/inf)."""
    try:
        float(text)
    except ValueError:
        return False
    return True


_WHITESPACE = st.sampled_from(["", " ", "  ", "\t", " \t "])
_HEADER_NAMES = st.sampled_from(
    ["Retry-After", "retry-after", "RETRY-AFTER", "Retry-after"]
)
_QUOTA_REASONS = st.sampled_from(
    [
        "quotaExceeded",
        "dailyLimitExceeded",
        "rateLimitExceeded",
        "userRateLimitExceeded",
    ]
)


@st.composite
def _numeric_retry_after(draw: st.DrawFn) -> tuple[str, float]:
    """A clearly-numeric, non-negative seconds value.

    Returns ``(raw_header_value, expected_seconds)`` where ``raw_header_value``
    may carry surrounding whitespace (the classifier trims it) and
    ``expected_seconds`` is the float the header denotes.
    """
    if draw(st.booleans()):
        text = str(draw(st.integers(min_value=0, max_value=1_000_000)))
    else:
        value = draw(
            st.floats(
                min_value=0.0,
                max_value=1_000_000.0,
                allow_nan=False,
                allow_infinity=False,
            )
        )
        text = repr(value)  # round-trips: float(repr(value)) == value
    raw = f"{draw(_WHITESPACE)}{text}{draw(_WHITESPACE)}"
    return raw, float(text)


@st.composite
def _non_numeric_retry_after(draw: st.DrawFn) -> str:
    """A Retry-After value that does NOT denote a numeric seconds value.

    Spans the empty string, an HTTP-date (the alternative legal Retry-After
    form, which this feature deliberately does not parse), and arbitrary
    non-numeric text. ``nan``/``inf`` spellings are excluded via ``assume`` so
    the value is unambiguously non-numeric.
    """
    candidate = draw(
        st.one_of(
            st.just(""),
            st.just("Wed, 21 Oct 2015 07:28:00 GMT"),
            st.text(
                alphabet=string.ascii_letters + " :,-/", min_size=0, max_size=24
            ),
        )
    )
    assume(not _is_floatable(candidate.strip()))
    return candidate


@st.composite
def _rate_limit_case(draw: st.DrawFn) -> tuple[HttpResponse, float | None]:
    """A rate-limit :class:`HttpResponse` and its expected ``retry_after_seconds``.

    The response is either HTTP 429 or HTTP 403 carrying a quota/rate reason, so
    ``classify_response`` always classifies it as a rate limit. The Retry-After
    header is absent, numeric, or non-numeric; ``expected`` is the float for the
    numeric case and ``None`` otherwise.
    """
    situation = draw(st.sampled_from(["absent", "numeric", "non_numeric"]))
    headers: dict[str, str] = {}
    expected: float | None = None

    if situation == "numeric":
        raw, expected = draw(_numeric_retry_after())
        headers[draw(_HEADER_NAMES)] = raw
    elif situation == "non_numeric":
        headers[draw(_HEADER_NAMES)] = draw(_non_numeric_retry_after())

    # Optionally include unrelated headers to ensure they do not interfere.
    if draw(st.booleans()):
        headers["Content-Type"] = "application/json"

    if draw(st.sampled_from(["429", "403_quota"])) == "429":
        response = HttpResponse(status=429, headers=headers, body=b"")
    else:
        body = json.dumps(
            {"error": {"errors": [{"reason": draw(_QUOTA_REASONS)}]}}
        ).encode("utf-8")
        response = HttpResponse(status=403, headers=headers, body=body)

    return response, expected


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 6: Rate-limit retry_after_seconds reflects a numeric Retry-After header exactly
# Validates: Requirements 2.3, 2.4
@settings(max_examples=200)
@given(case=_rate_limit_case(), description=st.text(min_size=1, max_size=40))
def test_rate_limit_retry_after_reflects_numeric_header_exactly(case, description):
    """For any rate-limit response, the raised ``RateLimitError`` sets
    ``retry_after_seconds`` to the header's numeric seconds value when present,
    and leaves it unset when the header is absent or non-numeric (2.3, 2.4)."""
    response, expected = case
    # A secret is supplied to confirm redaction never disturbs the numeric
    # extraction; the header carries no secret so the value is unaffected.
    secret = Secret("super-secret-key", CredentialReference("youtube_api_key"))

    error = classify_response(
        request_description=description, response=response, secrets=[secret]
    )

    # The case is always a rate limit, so a RateLimitError is raised.
    assert isinstance(error, RateLimitError)

    if expected is None:
        assert error.retry_after_seconds is None
    else:
        assert error.retry_after_seconds == pytest.approx(expected)
