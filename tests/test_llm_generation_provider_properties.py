"""Hypothesis property test for the LLM generation provider failure path (task 8.3).

This module hosts Property 12 for
:class:`~generation.llm_generation_provider.LLMGenerationProvider`. The
conformance check and happy-path/raw-artifact behaviour live elsewhere
(``tests/test_llm_generation_provider.py`` for conformance, Property 11 for the
raw-artifact pass-through); this module is the universal layer that asserts the
*failure* contract holds across arbitrary ideas, operations, and failure modes.

Property 12 (design.md -> *LLMGenerationProvider*): *for any* generation request
that fails, does not complete within the configured timeout, returns zero items,
or returns only empty/whitespace content, ``LLMGenerationProvider`` SHALL raise a
``GenerationError`` identifying the failed item and the affected ``ContentIdea``
identifier and SHALL NOT return a partial artifact; and *for any* title/thumbnail
count less than 1 it SHALL raise such a ``GenerationError`` without issuing any
request to the LLM.

The provider is driven entirely through the deterministic spy ``LLMClient`` from
``tests/edge_fakes.py``: scripted responses/exceptions reproduce every failure
mode, and the spy's call count proves whether a request was issued -- so the
property runs with no real network access (16.3).

Validates: Requirements 6.6, 6.8
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from domain.models import ChannelCategory, ContentIdea, TimeWindow, ViralTemplate
from generation.llm_client import LLMClientError
from generation.llm_generation_provider import LLMGenerationProvider
from generation.provider import (
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    OP_THUMBNAILS,
    OP_TITLES,
    GenerationError,
)

from .edge_fakes import SpyLLMClient

# Operations that take a candidate ``count`` (and so also have the count<1 rule).
_COUNTED_OPS = (OP_TITLES, OP_THUMBNAILS)
# Single-artifact operations (outline/script/description) -- no ``count``.
_SINGLE_OPS = (OP_OUTLINE, OP_SCRIPT, OP_DESCRIPTION)

# Failure modes that occur *after* a request is issued (count>=1 for counted ops).
_REQUESTING_FAILURES = ("request_failure", "timeout", "zero_items", "blank_only")
# The count<1 rule: a request must never be issued.
_INVALID_COUNT = "invalid_count"

_REQUEST_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the property's scope:
#   * ``_ideas`` produces arbitrary, valid ContentIdeas; ``idea_id`` is always
#     non-empty so the "identifies the affected idea" assertion is meaningful,
#     and ``category`` ranges over every value and ``None``.
#   * ``_blank_texts`` produces only empty/whitespace-only strings (the
#     empty-or-whitespace content failure mode); drawing from a whitespace-only
#     alphabet with ``min_size=0`` covers the empty string and runs of spaces,
#     tabs, and newlines.
#   * ``_scenarios`` pairs an operation with a failure mode valid for it: the
#     count<1 rule applies only to the counted operations, while every operation
#     can fail by request failure, timeout, zero items, or blank-only content.
# ---------------------------------------------------------------------------

_categories = st.one_of(st.none(), st.sampled_from(list(ChannelCategory)))
_finite_floats = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)
# Empty or whitespace-only content: the "returns only empty/whitespace" mode.
_blank_texts = st.text(alphabet=" \t\n\r\f\v", min_size=0, max_size=6)


@st.composite
def _templates(draw: st.DrawFn) -> tuple[ViralTemplate, ...]:
    """Between 1 and 5 associated viral templates (the model invariant)."""
    count = draw(st.integers(min_value=1, max_value=5))
    category = draw(st.sampled_from(list(ChannelCategory)))
    return tuple(
        ViralTemplate(
            template_id=f"t{i}",
            name="tier-list ranking",
            category=category,
            observed_performance=draw(_finite_floats),
        )
        for i in range(count)
    )


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """An arbitrary, valid ContentIdea with a non-empty identifier."""
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=24)),
        title_concept=draw(st.text(min_size=0, max_size=120)),
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=draw(_categories),
        templates=draw(_templates()),
        observed_metric_value=draw(_finite_floats),
    )


@dataclass(frozen=True)
class _Scenario:
    """A single failure scenario: which operation, how it fails, and its count."""

    operation: str
    failure_mode: str
    count: int | None
    blank_items: tuple[str, ...]


@st.composite
def _scenarios(draw: st.DrawFn) -> _Scenario:
    operation = draw(st.sampled_from(_COUNTED_OPS + _SINGLE_OPS))
    if operation in _COUNTED_OPS:
        failure_mode = draw(st.sampled_from((_INVALID_COUNT, *_REQUESTING_FAILURES)))
    else:
        failure_mode = draw(st.sampled_from(_REQUESTING_FAILURES))

    if operation in _COUNTED_OPS:
        if failure_mode == _INVALID_COUNT:
            # Any count strictly below 1, including zero and negatives.
            count = draw(st.integers(min_value=-5, max_value=0))
        else:
            count = draw(st.integers(min_value=1, max_value=8))
    else:
        count = None

    # A non-empty run of blank items, used only by the blank-only mode.
    blank_items = tuple(draw(st.lists(_blank_texts, min_size=1, max_size=6)))
    return _Scenario(
        operation=operation,
        failure_mode=failure_mode,
        count=count,
        blank_items=blank_items,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _configure_spy(scenario: _Scenario) -> SpyLLMClient:
    """Build a spy ``LLMClient`` scripted to reproduce the scenario's failure."""
    if scenario.failure_mode == "request_failure":
        return SpyLLMClient(fail_with=LLMClientError("upstream rejected the request"))
    if scenario.failure_mode == "timeout":
        # A non-completing request surfaces as the client raising (design 6.6).
        return SpyLLMClient(fail_with=TimeoutError("request deadline exceeded"))
    if scenario.failure_mode == "zero_items":
        return SpyLLMClient(responses=[()])
    if scenario.failure_mode == "blank_only":
        return SpyLLMClient(responses=[scenario.blank_items])
    # _INVALID_COUNT: the provider must never reach the client, so no script is
    # needed; an unscripted call would synthesize output and fail the assertion.
    return SpyLLMClient()


def _invoke(provider: LLMGenerationProvider, scenario: _Scenario, idea: ContentIdea):
    """Call the operation under test; returns its value or raises (we expect raise)."""
    if scenario.operation == OP_TITLES:
        return provider.generate_titles(idea, scenario.count)
    if scenario.operation == OP_THUMBNAILS:
        return provider.generate_thumbnails(idea, scenario.count)
    if scenario.operation == OP_OUTLINE:
        return provider.generate_outline(idea)
    if scenario.operation == OP_SCRIPT:
        return provider.generate_script(idea)
    return provider.generate_description(idea)


# ---------------------------------------------------------------------------
# Property 12
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 12: Generation failure conditions raise without partial output or a wasted request
@settings(max_examples=200)
@given(idea=_ideas(), scenario=_scenarios())
def test_generation_failures_raise_without_partial_output_or_wasted_request(
    idea: ContentIdea, scenario: _Scenario
) -> None:
    """Every failure mode raises a ``GenerationError`` naming the item and idea.

    For a request failure, timeout, zero items, or only-empty/whitespace content
    the provider raises rather than returning a partial artifact (6.6). For a
    title/thumbnail count below 1 it raises without issuing any request (6.8).

    Validates: Requirements 6.6, 6.8
    """
    spy = _configure_spy(scenario)
    provider = LLMGenerationProvider(
        spy, request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
    )

    # (6.6, 6.8) the call raises -- so no partial artifact is ever returned.
    with pytest.raises(GenerationError) as exc_info:
        _invoke(provider, scenario, idea)

    error = exc_info.value
    # The error identifies the failed item and the affected idea identifier.
    assert error.item == scenario.operation
    assert error.idea_id == idea.idea_id

    if scenario.failure_mode == _INVALID_COUNT:
        # (6.8) a count below 1 raises *without* issuing any request to the LLM.
        assert spy.call_count == 0
    else:
        # (6.6) these modes arise only after a request was attempted; the spy
        # confirms the provider issued exactly the request(s) it then failed on,
        # never returning a partial artifact for the failed item.
        assert spy.call_count >= 1
