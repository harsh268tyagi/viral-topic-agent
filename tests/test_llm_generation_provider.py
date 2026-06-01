"""Conformance test for the LLM generation provider (task 8.4).

Asserts that the concrete :class:`~generation.llm_generation_provider.LLMGenerationProvider`
structurally satisfies the existing :class:`~generation.provider.GenerationProvider`
protocol *without modifying that protocol* (Requirements 6.1, 16.1).

``GenerationProvider`` is a ``runtime_checkable`` ``Protocol``, so the structural
check is a single ``isinstance`` against the unmodified protocol -- mirroring the
existing conformance checks for the in-memory stub
(``tests/test_generation.py::test_stub_satisfies_generation_provider_protocol``),
the ``DataSource`` protocol (``tests/test_datasource.py``), and the ``Deliverer``
protocol (``tests/test_delivery.py``).

The provider is constructed against the injected spy ``LLMClient`` from
``tests/edge_fakes.py`` so the check needs no real network access (16.3).
"""

from __future__ import annotations

from generation.llm_generation_provider import LLMGenerationProvider
from generation.provider import GenerationProvider

from tests.edge_fakes import SpyLLMClient


def _provider() -> LLMGenerationProvider:
    """Build an ``LLMGenerationProvider`` over a deterministic spy client."""
    return LLMGenerationProvider(SpyLLMClient(), request_timeout_seconds=30.0)


def test_llm_generation_provider_satisfies_generation_provider_protocol():
    """``LLMGenerationProvider`` is an instance of the runtime-checkable protocol (6.1, 16.1)."""
    provider = _provider()
    assert isinstance(provider, GenerationProvider)


def test_llm_generation_provider_exposes_every_protocol_method():
    """All five ``GenerationProvider`` operations are present and callable (6.1)."""
    provider = _provider()
    for method_name in (
        "generate_titles",
        "generate_thumbnails",
        "generate_outline",
        "generate_script",
        "generate_description",
    ):
        assert callable(getattr(provider, method_name))


def test_generation_provider_typing_assignment():
    """A statically-typed ``GenerationProvider`` binding accepts the concrete provider.

    This is the static-typing counterpart to the runtime ``isinstance`` check:
    it documents that the concrete provider is usable wherever a
    ``GenerationProvider`` is expected, without the protocol being modified.
    """
    provider: GenerationProvider = _provider()
    assert provider is not None
