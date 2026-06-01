"""Hypothesis property test for configuration precedence (task 2.5).

This module validates a single universal property of ``ConfigLoader``'s
precedence resolution (``src/config/config_loader.py``):

- Property 14 (10.1, 10.2, 10.3): configuration precedence selects the
  highest-precedence value or the defined default. For any assignment of
  configuration keys across the ordered sources -- explicit overrides, then
  process environment variables, then the ``.env`` file, then
  configuration-file defaults -- ``ConfigLoader`` resolves each key to the value
  from the highest-precedence source that supplies it, and falls back to the
  defined per-key default for a key absent from every source.

The property is exercised through the documented ``ConfigLoader.resolve()``
mapping (the resolved values, including defaults), assembled from the four
**real** source classes in ``src/config/sources.py`` wired in their documented
decreasing-precedence order::

    [OverridesSource, EnvSource, DotEnvSource, ConfigFileSource]

For each example a random assignment of keys to a random subset of the four
layers is generated, every selected layer is given a value tagged with its layer
index so the per-key layer values are distinct, and an expected resolution model
is folded independently of ``ConfigLoader``'s resolution algorithm: the expected
value for a key is the value of the highest-precedence layer that supplies it,
or the documented default (:data:`config.config_loader.DEFAULTS`) when no layer
does, or absent when no layer supplies it and no default is defined.

Generators are constrained to the resolver's input space: keys are drawn from
the documented configuration keys (a mix of keys that carry a documented default
and keys that do not), and values use a clean ``KEY=VALUE``-safe alphabet
(letters and digits) with a trailing ``_<layer-index>`` tag, so that rendering
the ``.env`` layer round-trips through the real ``DotEnvSource`` parser without
engaging the orthogonal comment/blank/malformed parsing rules (those are
Property 15). No real environment or file system is touched: ``EnvSource`` reads
a supplied mapping, ``DotEnvSource`` parses supplied text, and
``ConfigFileSource`` reads a supplied values mapping.
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from config.config_loader import (
    DEFAULTS,
    KEY_EMAIL_SENDER,
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS,
    KEY_LLM_TIMEOUT_SECONDS,
    KEY_NOTION_API_VERSION,
    KEY_NOTION_DATABASE_ID,
    KEY_REQUEST_TIMEOUT_SECONDS,
    KEY_SLACK_API_BASE_URL,
    KEY_SLACK_TOKEN,
    KEY_SMTP_HOST,
    KEY_SMTP_PORT,
    KEY_YOUTUBE_API_KEY,
    ConfigLoader,
)
from config.sources import (
    ConfigFileSource,
    DotEnvSource,
    EnvSource,
    OverridesSource,
)

# ---------------------------------------------------------------------------
# Layers and keys
# ---------------------------------------------------------------------------

# The four precedence layers in DECREASING precedence, matching the order in
# which ConfigLoader is constructed below. Index 0 is the highest precedence.
_LAYERS: tuple[str, ...] = ("overrides", "env", "dotenv", "configfile")

# Documented keys that carry a per-key default in DEFAULTS: when no layer
# supplies them, resolve() must fall back to the documented default (10.3).
_KEYS_WITH_DEFAULT: tuple[str, ...] = (
    KEY_LLM_TIMEOUT_SECONDS,
    KEY_REQUEST_TIMEOUT_SECONDS,
    KEY_SMTP_PORT,
    KEY_SLACK_API_BASE_URL,
    KEY_NOTION_API_VERSION,
    KEY_KEYWORD_SOURCE_MAX_KEYWORDS,
)

# Documented keys with no default: when no layer supplies them, they are absent
# from the resolved mapping entirely.
_KEYS_WITHOUT_DEFAULT: tuple[str, ...] = (
    KEY_YOUTUBE_API_KEY,
    KEY_SMTP_HOST,
    KEY_SLACK_TOKEN,
    KEY_NOTION_DATABASE_ID,
    KEY_EMAIL_SENDER,
)

_ALL_KEYS: tuple[str, ...] = _KEYS_WITH_DEFAULT + _KEYS_WITHOUT_DEFAULT

# Defensive: every "with default" key really does carry a documented default and
# every "without default" key really does not, so the two branches below are
# exercised as intended.
assert all(key in DEFAULTS for key in _KEYS_WITH_DEFAULT)
assert all(key not in DEFAULTS for key in _KEYS_WITHOUT_DEFAULT)

# A clean alphabet for values: letters and digits only, so a value never
# contains '=', '#', whitespace, or a line boundary and therefore round-trips
# through the real DotEnvSource parser unchanged.
_VALUE_ALPHABET = string.ascii_letters + string.digits


@st.composite
def _assignment(draw: st.DrawFn) -> dict[str, dict[str, str]]:
    """Draw ``{key: {layer: value}}`` assigning keys to a random subset of layers.

    Each drawn key is assigned to an arbitrary (possibly empty) subset of the
    four layers; every selected layer receives a value tagged with its layer
    index so the values a key carries across layers are pairwise distinct, which
    makes the precedence selection observable.
    """
    keys = draw(
        st.lists(st.sampled_from(_ALL_KEYS), min_size=1, max_size=6, unique=True)
    )
    assignment: dict[str, dict[str, str]] = {}
    for key in keys:
        present_layers = draw(
            st.lists(st.sampled_from(_LAYERS), max_size=len(_LAYERS), unique=True)
        )
        layer_values: dict[str, str] = {}
        for layer in present_layers:
            core = draw(st.text(alphabet=_VALUE_ALPHABET, min_size=1, max_size=8))
            # Tag with the layer index to guarantee distinct per-layer values
            # for this key, so a wrong-precedence pick would change the string.
            layer_values[layer] = f"{core}_{_LAYERS.index(layer)}"
        assignment[key] = layer_values
    return assignment


def _expected_value(layer_values: dict[str, str], key: str) -> str | None:
    """The independently-derived expected resolution for one key.

    Returns the value of the highest-precedence layer that supplies ``key``;
    failing that, the documented default; failing that, ``None`` (the key is
    expected to be absent from the resolved mapping). This mirrors the documented
    precedence rules without invoking ``ConfigLoader``'s resolution algorithm.
    """
    for layer in _LAYERS:  # _LAYERS is ordered highest precedence first.
        if layer in layer_values:
            return layer_values[layer]
    return DEFAULTS.get(key)


# ---------------------------------------------------------------------------
# Property 14
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 14: Configuration precedence selects the highest-precedence value or the defined default
# Validates: Requirements 10.1, 10.2, 10.3
@settings(max_examples=200)
@given(assignment=_assignment())
def test_configuration_precedence_selects_highest_or_default(assignment):
    """For any assignment of keys across the ordered sources, ``resolve()`` picks
    the value from the highest-precedence source supplying each key, and the
    documented default for a key absent from every source (10.1, 10.2, 10.3)."""
    # Build each layer's raw key->value mapping from the assignment.
    overrides = {k: lv["overrides"] for k, lv in assignment.items() if "overrides" in lv}
    environ = {k: lv["env"] for k, lv in assignment.items() if "env" in lv}
    dotenv_pairs = {k: lv["dotenv"] for k, lv in assignment.items() if "dotenv" in lv}
    configfile = {k: lv["configfile"] for k, lv in assignment.items() if "configfile" in lv}

    # Render the .env layer as KEY=VALUE lines; the clean alphabet guarantees a
    # faithful round-trip through the real DotEnvSource parser.
    dotenv_text = "\n".join(f"{key}={value}" for key, value in dotenv_pairs.items())

    loader = ConfigLoader(
        [
            OverridesSource(overrides),
            EnvSource(environ),
            DotEnvSource(text=dotenv_text),
            ConfigFileSource(values=configfile),
        ]
    )

    resolved = loader.resolve()

    for key, layer_values in assignment.items():
        expected = _expected_value(layer_values, key)
        if expected is None:
            # No layer supplied the key and no documented default exists, so the
            # resolved mapping must not contain it.
            assert key not in resolved
        else:
            assert resolved[key] == expected
