"""Hypothesis property test for configuration round-trip integrity (task 3.2).

This module hosts Property 29 for configuration persistence. Concrete,
hand-checked round-trip examples and all of the named-error branches live in
``tests/test_config_store.py``; this module is the universal layer that asserts
the lossless round-trip contract across arbitrary *valid* ``Configuration``
values.

Property 29 (design.md -> ``ConfigurationStore`` / Requirement 15.4): *for all*
valid ``Configuration`` values ``cfg``,
``deserialize_config(serialize_config(cfg))`` SHALL produce a ``Configuration``
that is field-by-field equal to ``cfg`` across the five persisted settings
(authorized channels, selected category, monitored competitors, schedule,
delivery destinations).

# Feature: viral-topic-agent, Property 29: Configuration serialization round-trips losslessly

Validates: Requirements 15.1, 15.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from persistence.config_store import deserialize_config, serialize_config
from domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    Configuration,
    DeliveryDestination,
    Schedule,
)

# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the *valid* Configuration input space:
#
# - Strings exclude surrogate code points (category "Cs"). A lone surrogate is a
#   legal Python ``str`` but is not representable in JSON / UTF-8 persisted
#   storage, so it is outside the space of values that can be persisted at all.
#   Every other unicode character (including emoji, CJK, and control chars) is
#   allowed, so the round-trip is still exercised over a broad alphabet.
# - ``selected_category`` and ``delivery_destinations`` draw only from the real
#   enum members, the only values the codec accepts.
# - ``schedule`` covers ``None`` plus every combination of present/absent
#   interval and run time, since the codec persists each field independently and
#   a partial schedule must still round-trip.
# ---------------------------------------------------------------------------

_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=40,
)


@st.composite
def _authorized_channels(draw: st.DrawFn) -> tuple[AuthorizedChannel, ...]:
    """Up to 50 authorized channels (the documented cap)."""
    channels = draw(
        st.lists(
            st.builds(
                AuthorizedChannel,
                channel_id=_text,
                credentials_ref=_text,
                connected=st.booleans(),
                credentials_expired=st.booleans(),
            ),
            min_size=0,
            max_size=5,
        )
    )
    return tuple(channels)


_schedules = st.one_of(
    st.none(),
    st.builds(
        Schedule,
        recurrence_interval=st.one_of(st.none(), _text),
        run_time=st.one_of(st.none(), _text),
    ),
)


@st.composite
def _configurations(draw: st.DrawFn) -> Configuration:
    """An arbitrary valid Configuration across all five persisted settings."""
    return Configuration(
        authorized_channels=draw(_authorized_channels()),
        selected_category=draw(
            st.one_of(st.none(), st.sampled_from(list(ChannelCategory)))
        ),
        monitored_competitors=tuple(
            draw(st.lists(_text, min_size=0, max_size=5))
        ),
        schedule=draw(_schedules),
        delivery_destinations=tuple(
            draw(st.lists(st.sampled_from(list(DeliveryDestination)), min_size=0, max_size=3))
        ),
    )


# ---------------------------------------------------------------------------
# Property 29
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 29: Configuration serialization round-trips losslessly
@settings(max_examples=200)
@given(config=_configurations())
def test_serialization_round_trips_losslessly(config: Configuration) -> None:
    """deserialize(serialize(cfg)) is field-by-field equal to cfg (15.1, 15.4).

    Validates: Requirements 15.1, 15.4
    """
    restored = deserialize_config(serialize_config(config))

    # Whole-object value equality (frozen dataclasses compare field-by-field)...
    assert restored == config

    # ...and each of the five persisted settings explicitly, per 15.4.
    assert restored.authorized_channels == config.authorized_channels
    assert restored.selected_category == config.selected_category
    assert restored.monitored_competitors == config.monitored_competitors
    assert restored.schedule == config.schedule
    assert restored.delivery_destinations == config.delivery_destinations
