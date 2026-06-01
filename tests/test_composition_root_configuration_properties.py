"""Hypothesis property test for the built Configuration (task 11.3).

This module validates a single universal property of the
:class:`~app.composition_root.CompositionRoot`'s configuration assembly
(``src/app/composition_root.py`` -> ``CompositionRoot._build_configuration``):

- Property 18 (14.3): the built ``Configuration`` faithfully reflects the
  ``Settings``. For any validated :class:`~config.settings.Settings`, the
  :class:`~domain.models.Configuration` the ``CompositionRoot`` builds carries
  the authorized channels, the selected :class:`~domain.models.ChannelCategory`,
  the monitored competitor channels, the :class:`~domain.models.Schedule`, and
  the delivery destinations equal to the corresponding values in those
  ``Settings``.

The property is exercised directly against ``_build_configuration`` -- the seam
the design calls out for building the domain ``Configuration`` (14.3) -- on a
``CompositionRoot`` wired entirely with injected fakes (a real but empty
:class:`~config.config_loader.ConfigLoader`, a
:class:`~tests.edge_fakes.FakeHttpTransport`, a :class:`~tests.edge_fakes.SpyLLMClient`,
and a :class:`~infrastructure.clock.FakeClock`), so no real network access is
required (16.3, 16.4) and ``_build_configuration`` is a pure function of the
``Settings`` it is given.

Generators are constrained to the field space the design's ``settings`` strategy
describes: authorized-channel and competitor sets spanning empty through several
entries, an optionally-selected category, a delivery-destination set that is any
unique subset of the supported destinations, and a schedule that is absent,
partial, or complete -- so the property covers empty and maximal sets with and
without a complete schedule. The authentication settings and timeouts (which the
configuration does not carry) are held fixed at valid values.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from app.composition_root import CompositionRoot
from config.config_loader import ConfigLoader
from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings, Settings
from domain.models import (
    AuthorizedChannel,
    ChannelCategory,
    DeliveryDestination,
    Schedule,
)
from infrastructure.clock import FakeClock
from tests.edge_fakes import FakeHttpTransport, SpyLLMClient

# ---------------------------------------------------------------------------
# Generators (constrained to the Settings field space, Property 18)
# ---------------------------------------------------------------------------

# A clean, non-empty identifier alphabet for channel ids / credential refs /
# competitor handles / schedule fields, so generated strings are distinct and
# carry no surprising whitespace that would obscure a faithful round-trip.
_TOKEN = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_",
    min_size=1,
    max_size=12,
)

_CATEGORIES = list(ChannelCategory)
_DESTINATIONS = list(DeliveryDestination)


@st.composite
def _authorized_channels(draw: st.DrawFn) -> tuple[AuthorizedChannel, ...]:
    """Draw 0..5 :class:`AuthorizedChannel`s (empty through a small maximal set)."""
    count = draw(st.integers(min_value=0, max_value=5))
    channels: list[AuthorizedChannel] = []
    for _ in range(count):
        channels.append(
            AuthorizedChannel(
                channel_id=draw(_TOKEN),
                credentials_ref=draw(_TOKEN),
                connected=draw(st.booleans()),
                credentials_expired=draw(st.booleans()),
            )
        )
    return tuple(channels)


def _competitors() -> st.SearchStrategy[tuple[str, ...]]:
    """Draw 0..5 competitor channel handles."""
    return st.lists(_TOKEN, min_size=0, max_size=5).map(tuple)


def _selected_category() -> st.SearchStrategy[ChannelCategory | None]:
    """Draw an optionally-selected supported category (``None`` = unselected)."""
    return st.one_of(st.none(), st.sampled_from(_CATEGORIES))


def _delivery_destinations() -> st.SearchStrategy[tuple[DeliveryDestination, ...]]:
    """Draw any unique subset of the supported delivery destinations."""
    return st.lists(
        st.sampled_from(_DESTINATIONS), min_size=0, max_size=len(_DESTINATIONS), unique=True
    ).map(tuple)


def _schedules() -> st.SearchStrategy[Schedule | None]:
    """Draw an absent, partial, or complete schedule.

    Covers the design's "with and without a complete schedule": ``None``
    (manual-only), a schedule missing either field (partial), or a schedule with
    both a recurrence interval and a run time (complete).
    """
    opt_token = st.one_of(st.none(), _TOKEN)
    return st.one_of(
        st.none(),
        st.builds(Schedule, recurrence_interval=opt_token, run_time=opt_token),
    )


@st.composite
def _settings(draw: st.DrawFn) -> Settings:
    """Assemble a valid :class:`Settings` spanning the Property 18 field space.

    The five fields the ``Configuration`` reflects vary across their full ranges;
    the authentication settings and timeouts (which the ``Configuration`` does
    not carry) are held fixed at valid values, keeping the example focused on the
    faithful-reflection property.
    """
    return Settings(
        auth=AuthSettings(
            youtube_api_key=Secret("api-key-value", CredentialReference("youtube_api_key"))
        ),
        llm_timeout_seconds=60.0,
        request_timeout_seconds=30.0,
        authorized_channels=draw(_authorized_channels()),
        selected_category=draw(_selected_category()),
        monitored_competitors=draw(_competitors()),
        schedule=draw(_schedules()),
        delivery_destinations=draw(_delivery_destinations()),
    )


def _composition_root() -> CompositionRoot:
    """Build a :class:`CompositionRoot` wired entirely with injected fakes.

    ``_build_configuration`` consults only the ``Settings`` it is handed, so the
    loader and ports are never exercised here; they are supplied as fakes so no
    real network access is possible (16.3, 16.4).
    """
    return CompositionRoot(
        ConfigLoader([]),
        http_transport=FakeHttpTransport(),
        llm_client=SpyLLMClient(),
        clock=FakeClock(),
    )


# ---------------------------------------------------------------------------
# Property 18
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 18: The built Configuration faithfully reflects the Settings
# Validates: Requirements 14.3
@settings(max_examples=200)
@given(config_settings=_settings())
def test_built_configuration_faithfully_reflects_settings(config_settings):
    """For any validated ``Settings``, the built ``Configuration`` carries the
    authorized channels, selected category, monitored competitors, schedule, and
    delivery destinations equal to the corresponding ``Settings`` values (14.3)."""
    root = _composition_root()

    configuration = root._build_configuration(config_settings)

    assert configuration.authorized_channels == config_settings.authorized_channels
    assert configuration.selected_category == config_settings.selected_category
    assert configuration.monitored_competitors == config_settings.monitored_competitors
    assert configuration.schedule == config_settings.schedule
    assert configuration.delivery_destinations == config_settings.delivery_destinations
