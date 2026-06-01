"""Hypothesis property test for raw-artifact pass-through (task 8.2).

This module hosts Property 11 for
:class:`~generation.llm_generation_provider.LLMGenerationProvider`. The failure
contract lives in ``tests/test_llm_generation_provider_properties.py`` (Property
12) and the protocol-conformance check in
``tests/test_llm_generation_provider.py``; this module is the universal layer
that asserts the *success* contract -- that the provider returns the LLM's raw
artifacts unmodified across arbitrary ideas and arbitrary produced output.

Property 11 (design.md -> *Correctness Properties*): *for any* successful LLM
output, ``LLMGenerationProvider`` SHALL return one artifact per produced item --
one title string per title candidate, one ``ThumbnailDraft`` (with a non-empty
visual description and a text overlay) per thumbnail concept, and the produced
artifact string for outline/script/description -- *without* enforcing the domain
title-distinctness, title-length, thumbnail-overlay-length, or
description-length constraints.

The provider is driven entirely through the deterministic spy ``LLMClient`` from
``tests/edge_fakes.py``: each invocation is scripted with the exact tuple of
"produced" items, so the test reproduces every successful-output shape with no
real network access (16.3). Because the produced items are scripted independent
of the requested ``count``, the test also demonstrates the "one artifact per
*produced* item" guarantee rather than "one per *requested* item".

Validates: Requirements 6.2, 6.3, 6.4, 6.5
"""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from domain.models import ChannelCategory, ContentIdea, TimeWindow, ViralTemplate
from generation.llm_generation_provider import (
    THUMBNAIL_FIELD_DELIMITER,
    LLMGenerationProvider,
)
from generation.provider import ThumbnailDraft

from .edge_fakes import SpyLLMClient

_REQUEST_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Generators
#
# Smart strategies constrained to the property's scope -- *successful* LLM
# output that may freely violate the domain constraints the provider must not
# enforce (distinctness, lengths):
#
#   * ``_ideas`` produces arbitrary, valid ContentIdeas with a non-empty
#     ``idea_id``; the idea only seeds the prompt, so its exact shape never
#     affects the pass-through assertions.
#   * ``_artifact_text`` is arbitrary text (including the empty string, runs of
#     whitespace, duplicates across a list, and strings far shorter or longer
#     than any domain bound) -- the space in which "no constraint enforcement"
#     is observable.
#   * ``_nonblank_text`` guarantees a non-whitespace character so a produced
#     item is *usable* (the provider treats an all-blank response as a failure,
#     which is Property 12's territory, not Property 11's).
#   * ``_produced_titles`` always contains at least one non-blank item, so the
#     output is "successful" and the whole produced tuple is returned verbatim.
#   * ``_produced_thumbnail_contents`` yields only non-blank contents (so every
#     draft has a non-empty visual description, 6.3) and biases toward the
#     ``visual || overlay`` delimiter with overlays that may exceed the domain
#     30-char bound (so overlay-length non-enforcement is exercised, 6.5).
# ---------------------------------------------------------------------------

_categories = st.one_of(st.none(), st.sampled_from(list(ChannelCategory)))
_finite_floats = st.floats(
    min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False
)
# Arbitrary text spanning the empty string, whitespace, and lengths on both
# sides of every domain bound (e.g. > 100 chars for titles, < 100 for
# descriptions): the input space where constraint non-enforcement is visible.
_artifact_text = st.text(min_size=0, max_size=200)
# At least one non-whitespace character, so the produced item is non-blank.
_nonblank_text = st.text(min_size=1, max_size=200).filter(lambda s: bool(s.strip()))


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


@st.composite
def _produced_titles(draw: st.DrawFn) -> tuple[str, ...]:
    """A successful title response: arbitrary items with >=1 non-blank one.

    Arbitrary items may be empty, whitespace, duplicated, or longer than the
    domain 1..100 char bound; the guaranteed non-blank anchor keeps the whole
    response from being treated as a failure, so the provider returns the entire
    tuple verbatim.
    """
    items = draw(st.lists(_artifact_text, min_size=0, max_size=5))
    anchor = draw(_nonblank_text)
    position = draw(st.integers(min_value=0, max_value=len(items)))
    items.insert(position, anchor)
    return tuple(items)


@st.composite
def _produced_thumbnail_contents(draw: st.DrawFn) -> tuple[str, ...]:
    """A successful thumbnail response: 1..6 non-blank raw contents.

    Each content is non-blank (so the parsed visual description is non-empty,
    6.3) and may carry the ``visual || overlay`` delimiter with an overlay that
    can exceed the domain 30-char overlay bound (so overlay-length
    non-enforcement is exercised, 6.5).
    """
    count = draw(st.integers(min_value=1, max_value=6))
    contents: list[str] = []
    for _ in range(count):
        visual = draw(_nonblank_text)
        if draw(st.booleans()):
            overlay = draw(st.text(min_size=0, max_size=60))
            contents.append(f"{visual}{THUMBNAIL_FIELD_DELIMITER}{overlay}")
        else:
            contents.append(visual)
    return tuple(contents)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Built:
    provider: LLMGenerationProvider
    spy: SpyLLMClient


def _provider_returning(produced: tuple[str, ...]) -> _Built:
    """A provider over a spy scripted to produce exactly ``produced`` once."""
    spy = SpyLLMClient(responses=[produced])
    provider = LLMGenerationProvider(
        spy, request_timeout_seconds=_REQUEST_TIMEOUT_SECONDS
    )
    return _Built(provider=provider, spy=spy)


def _expected_thumbnail(content: str) -> ThumbnailDraft:
    """The draft the provider must produce for one raw thumbnail completion.

    Mirrors the provider's documented parsing: split at the first delimiter; the
    visual description is the part before it, falling back to the whole content
    when that part is blank, so it stays non-empty; the overlay is the remainder
    passed through raw.
    """
    visual_part, _, overlay_part = content.partition(THUMBNAIL_FIELD_DELIMITER)
    visual = visual_part if visual_part.strip() else content
    return ThumbnailDraft(visual_description=visual, text_overlay=overlay_part)


# ---------------------------------------------------------------------------
# Property 11
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 11: Generation returns the LLM's raw artifacts unmodified
@settings(max_examples=200)
@given(
    idea=_ideas(),
    count=st.integers(min_value=1, max_value=8),
    produced_titles=_produced_titles(),
    produced_thumbnails=_produced_thumbnail_contents(),
    produced_outline=_nonblank_text,
    produced_script=_nonblank_text,
    produced_description=_nonblank_text,
)
def test_generation_returns_raw_artifacts_unmodified(
    idea: ContentIdea,
    count: int,
    produced_titles: tuple[str, ...],
    produced_thumbnails: tuple[str, ...],
    produced_outline: str,
    produced_script: str,
    produced_description: str,
) -> None:
    """The provider returns one raw artifact per produced item, unmodified.

    Titles pass through verbatim (one string per produced candidate), thumbnails
    map one-to-one to drafts with a non-empty visual description and a raw
    overlay, and outline/script/description return the produced string exactly --
    none of the domain distinctness or length constraints are enforced.

    Validates: Requirements 6.2, 6.3, 6.4, 6.5
    """
    # -- titles (6.2): one string per produced candidate, returned verbatim ----
    titles = _provider_returning(produced_titles).provider.generate_titles(idea, count)
    # One artifact per *produced* item, in order, with no modification: this
    # covers non-enforcement of title-distinctness (duplicates survive) and
    # title-length (over-100-char and empty items survive) (6.5).
    assert titles == produced_titles

    # -- thumbnails (6.3): one draft per produced concept, faithfully parsed ---
    drafts = _provider_returning(produced_thumbnails).provider.generate_thumbnails(
        idea, count
    )
    assert len(drafts) == len(produced_thumbnails)
    for content, draft in zip(produced_thumbnails, drafts):
        assert draft == _expected_thumbnail(content)
        # Each draft carries a non-empty visual description (6.3) and the raw
        # overlay is passed through with no length enforcement (6.5).
        assert draft.visual_description.strip() != ""

    # -- single artifacts (6.4): the produced string is returned exactly -------
    outline = _provider_returning((produced_outline,)).provider.generate_outline(idea)
    assert outline == produced_outline

    script = _provider_returning((produced_script,)).provider.generate_script(idea)
    assert script == produced_script

    description = _provider_returning(
        (produced_description,)
    ).provider.generate_description(idea)
    # Returned verbatim regardless of the domain 100..5000 char bound (6.5).
    assert description == produced_description
