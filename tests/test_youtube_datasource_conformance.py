"""Conformance test for ``YouTubeDataSource`` against the ``DataSource`` boundary (task 5.8).

Asserts that the concrete, network-backed :class:`~infrastructure.youtube_data_source.YouTubeDataSource`
structurally satisfies the existing :class:`~infrastructure.datasource.DataSource`
protocol *without modifying that protocol* (Requirements 1.1, 16.1, 16.3, 16.4).

``DataSource`` is a ``runtime_checkable`` ``Protocol`` (``src/infrastructure/datasource.py``),
so the structural check is a single ``isinstance`` against the unmodified protocol --
mirroring the existing conformance check for the in-memory recording stub
(``tests/test_datasource.py::test_recording_source_satisfies_datasource_protocol``)
and the deliverer conformance checks (``tests/test_deliverer_conformance.py``). A
method-presence check and a static-typing assignment document the same contract
from the other two angles.

The data source is constructed against the injected fakes from ``tests/edge_fakes.py``
(:class:`~tests.edge_fakes.FakeHttpTransport`), a real
:class:`~infrastructure.auth_manager.AuthManager` over that transport and a
:class:`~infrastructure.clock.FakeClock`, and an :class:`~config.settings.AuthSettings`
carrying a :class:`~config.secrets.Secret` API key, plus ``api_base_url`` and
``request_timeout_seconds`` -- so the structural check needs no real network access
(16.3, 16.4).

This module is deliberately limited to structural conformance; the per-method
behaviors (metadata, videos, recency, pagination, error mapping) are covered by
the property and example tests in tasks 5.1-5.7.
"""

from __future__ import annotations

from config.secrets import CredentialReference, Secret
from config.settings import AuthSettings
from infrastructure.auth_manager import AuthManager
from infrastructure.clock import FakeClock
from infrastructure.datasource import DataSource
from infrastructure.youtube_data_source import YouTubeDataSource

from tests.edge_fakes import FakeHttpTransport


# ---------------------------------------------------------------------------
# Construction helpers (injected fakes + redactable secret settings)
# ---------------------------------------------------------------------------


def _secret(name: str, value: str) -> Secret:
    """A :class:`Secret` wrapping ``value`` behind the non-secret handle ``name``."""
    return Secret(value, CredentialReference(name))


def _youtube_data_source() -> YouTubeDataSource:
    """Build a ``YouTubeDataSource`` over injected fakes only (1.1, 16.3, 16.4).

    The data source is wired with a :class:`FakeHttpTransport`, a real
    :class:`AuthManager` (itself over a :class:`FakeHttpTransport` and a
    :class:`FakeClock`) carrying a :class:`Secret` API key, and the same
    :class:`FakeClock`, so no real network access is required to construct or
    type-check it.
    """
    clock = FakeClock()
    auth_settings = AuthSettings(
        youtube_api_key=_secret("youtube_api_key", "yt-api-key-secret"),
    )
    auth = AuthManager(auth_settings, FakeHttpTransport(), clock)
    return YouTubeDataSource(
        FakeHttpTransport(),
        auth,
        clock,
        api_base_url="https://youtube.example.com/v3",
        request_timeout_seconds=30.0,
    )


# ---------------------------------------------------------------------------
# Runtime structural conformance (isinstance against the runtime_checkable Protocol)
# ---------------------------------------------------------------------------


def test_youtube_data_source_satisfies_datasource_protocol():
    """``YouTubeDataSource`` is an instance of the runtime-checkable ``DataSource`` (1.1, 16.1)."""
    source = _youtube_data_source()
    assert isinstance(source, DataSource)


# ---------------------------------------------------------------------------
# Method-presence conformance (the five DataSource operations)
# ---------------------------------------------------------------------------


def test_youtube_data_source_exposes_all_datasource_methods():
    """``YouTubeDataSource`` exposes every ``DataSource`` operation (1.1)."""
    source = _youtube_data_source()
    for method_name in (
        "get_channel_metadata",
        "get_videos",
        "get_audience_activity",
        "get_keyword_metrics",
        "get_template_performance",
    ):
        assert callable(getattr(source, method_name))


# ---------------------------------------------------------------------------
# Static-typing conformance (assignable to a ``DataSource``-typed binding)
# ---------------------------------------------------------------------------


def test_youtube_data_source_typing_assignment():
    """``YouTubeDataSource`` is usable wherever a ``DataSource`` is expected (16.1).

    The static-typing counterpart to the runtime ``isinstance`` check: it
    documents that the concrete source is assignable to a ``DataSource``-typed
    binding without the protocol being modified.
    """
    source: DataSource = _youtube_data_source()
    assert source is not None
