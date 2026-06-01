"""Hypothesis property test for ``.env`` parsing (task 2.3).

This module validates a single universal property of the pure ``.env`` parser
``parse_dotenv`` (``src/config/sources.py``):

- Property 15 (10.4, 10.6): parsing follows the comment, blank-line,
  ``KEY=VALUE``, and malformed-line rules. Comment lines (first non-whitespace
  character ``#``) and blank lines (empty or whitespace only) are ignored; every
  other line is split at its *first* ``=`` with the key and value trimmed of
  surrounding whitespace; a non-blank, non-comment line with no ``=`` is reported
  as malformed by its 1-based line number and contributes no value; and the last
  well-formed assignment wins for a duplicate key.

The parser is pure (no I/O, no network), so each example is built entirely
in-memory: a list of synthetic line specifications is rendered to ``.env`` text
*and* independently folded into an expected ``(values, malformed_lines)`` model.
The test asserts ``parse_dotenv`` reproduces that model exactly.

Generators are constrained to the parser's input space:

- comment lines carry optional leading whitespace, a ``#``, then arbitrary
  trailing text (which may itself contain ``=`` and ``#``) so the property
  confirms a ``#``-led line is ignored regardless of its contents;
- blank lines are empty or whitespace only;
- ``KEY=VALUE`` lines pad the key and value with whitespace to be trimmed and
  let the value contain ``=`` so the property confirms the split happens at the
  *first* ``=`` only; keys are drawn from a small pool so duplicates recur and
  the last-assignment-wins rule is exercised;
- malformed lines are non-blank, contain no ``=``, and do not begin with ``#``.

All generated content excludes every character ``str.splitlines`` treats as a
line boundary, and lines are joined with ``"\n"``, so a generated line's index
matches the 1-based line number the parser derives via ``splitlines`` -- the one
exception being a trailing empty blank line, which ``splitlines`` drops but which
contributes no value and shifts no earlier line number, leaving the expected
model unchanged.
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from config.sources import parse_dotenv

# ---------------------------------------------------------------------------
# Alphabets / helpers
# ---------------------------------------------------------------------------

# Characters that str.splitlines() treats as line boundaries must never appear
# inside a generated line, otherwise rendering the lines with "\n".join would
# desynchronise the parser's line numbering from the model's. The alphabets
# below are built only from safe, non-boundary characters (note: \t is NOT a
# line boundary, so it is allowed and is useful for exercising trimming).
_PUNCT = "=#@!$%^&*()_+-./:;<>?[]{}|~'\""
_TEXT_ALPHABET = string.ascii_letters + string.digits + " \t" + _PUNCT
# Malformed content excludes '=' (so the line stays malformed) and '#' (so it is
# never mistaken for a comment when it is the first non-whitespace character).
_MALFORMED_ALPHABET = string.ascii_letters + string.digits + " \t_-./@:;"

# Surrounding whitespace used to pad keys/values and to lead comment/malformed
# lines; the parser trims it, so the model expects it to vanish.
_WS = st.sampled_from(["", " ", "  ", "\t", " \t ", "\t "])
# Blank lines: empty or whitespace only.
_BLANK = st.sampled_from(["", " ", "  ", "\t", " \t", "\t \t"])

# Keys are valid identifiers (no whitespace, no '=', no leading '#') drawn from a
# small pool so duplicate keys recur within a document and last-wins is tested.
_KEY = st.sampled_from(["A", "B", "KEY", "KEY_1", "PATH", "db_url", "x", "Y2"])

# A canonical value: any safe text with surrounding whitespace stripped, so the
# value may contain '=', '#', and internal spaces but never leading/trailing
# whitespace. Re-padding it and trimming therefore round-trips to this value.
_VALUE_CORE = st.text(alphabet=_TEXT_ALPHABET, max_size=24).map(str.strip)

_COMMENT_TEXT = st.text(alphabet=_TEXT_ALPHABET, max_size=24)
_MALFORMED_CORE = st.text(alphabet=_MALFORMED_ALPHABET, min_size=1, max_size=24).filter(
    lambda s: s.strip() != ""
)


@st.composite
def _line(draw: st.DrawFn) -> tuple[str, str, str | None, str | None]:
    """Draw one ``.env`` line as ``(kind, raw_text, key, value)``.

    ``kind`` is one of ``"comment"``, ``"blank"``, ``"assign"``, ``"malformed"``.
    For an ``"assign"`` line ``key``/``value`` carry the trimmed pair the parser
    must recover; for every other kind they are ``None``.
    """
    kind = draw(st.sampled_from(["comment", "blank", "assign", "malformed"]))

    if kind == "blank":
        return "blank", draw(_BLANK), None, None

    if kind == "comment":
        # First non-whitespace character is '#'; trailing text is arbitrary and
        # may contain '=' or further '#', all of which must be ignored.
        raw = f"{draw(_WS)}#{draw(_COMMENT_TEXT)}"
        return "comment", raw, None, None

    if kind == "assign":
        key = draw(_KEY)
        value = draw(_VALUE_CORE)
        # Pad key and value with whitespace to be trimmed; the value's own '='
        # characters must survive because the split is at the FIRST '=' only.
        raw = f"{draw(_WS)}{key}{draw(_WS)}={draw(_WS)}{value}{draw(_WS)}"
        return "assign", raw, key, value

    # malformed: non-blank, no '=', does not begin with '#'.
    raw = f"{draw(_WS)}{draw(_MALFORMED_CORE)}"
    return "malformed", raw, None, None


# ---------------------------------------------------------------------------
# Property 15
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 15: .env parsing follows the comment, blank-line, KEY=VALUE, and malformed-line rules
# Validates: Requirements 10.4, 10.6
@settings(max_examples=200)
@given(lines=st.lists(_line(), max_size=20))
def test_parse_dotenv_follows_comment_blank_keyvalue_and_malformed_rules(lines):
    """For any ``.env`` content, ``parse_dotenv`` ignores comments and blanks,
    splits other lines at the first ``=`` with key/value trimmed (last
    assignment winning per key), and reports each non-blank, non-comment,
    no-``=`` line as malformed by its 1-based line number with no value
    (10.4, 10.6)."""
    text = "\n".join(raw for _, raw, _, _ in lines)

    # Independently fold the generated lines into the expected model, mirroring
    # the documented rules without invoking the parser's own logic.
    expected_values: dict[str, str] = {}
    expected_malformed: list[int] = []
    for line_number, (kind, _raw, key, value) in enumerate(lines, start=1):
        if kind == "assign":
            # Last assignment to a duplicate key wins.
            expected_values[key] = value  # type: ignore[index]
        elif kind == "malformed":
            expected_malformed.append(line_number)
        # comment / blank lines contribute nothing.

    result = parse_dotenv(text)

    assert dict(result.values) == expected_values
    assert result.malformed_lines == tuple(expected_malformed)
