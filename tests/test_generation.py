"""Example/edge-case tests for the generation provider stub (task 12.1).

Covers the deterministic in-memory ``InMemoryGenerationProvider`` that backs
the Concept_Generator (Requirement 8) and Script_Generator (Requirement 10) in
tests. The focus here is the stub's contract:

- determinism (same input -> same output, no hidden state),
- configurable cardinality (honours requested count; ceilings force
  under-production for the 8.5 path),
- controllable failure injection (raises ``GenerationError`` identifying the
  item and idea, for the 8.5 / 10.5 branches),
- raw artifacts respect the structural bounds the domain models require.

These are unit/example tests; they intentionally avoid mocks and exercise the
real stub implementation.
"""

from __future__ import annotations

import pytest

from generation import (
    GENERATION_OPERATIONS,
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    OP_THUMBNAILS,
    OP_TITLES,
    GenerationError,
    GenerationProvider,
    InMemoryGenerationProvider,
    ThumbnailDraft,
)
from domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _idea(
    idea_id: str = "idea-1",
    title: str = "Tier List Showdown",
    category: ChannelCategory | None = ChannelCategory.GAMING,
) -> ContentIdea:
    template = ViralTemplate(
        template_id="t1",
        name="tier-list ranking",
        category=ChannelCategory.GAMING,
        observed_performance=1000.0,
    )
    return ContentIdea(
        idea_id=idea_id,
        title_concept=title,
        rationale="metric value 4321 observed within the window",
        time_window=TimeWindow.WEEKLY,
        category=category,
        templates=(template,),
        observed_metric_value=4321.0,
    )


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_stub_satisfies_generation_provider_protocol():
    provider = InMemoryGenerationProvider()
    assert isinstance(provider, GenerationProvider)


def test_generation_operations_constant_is_complete():
    assert GENERATION_OPERATIONS == {
        OP_TITLES,
        OP_THUMBNAILS,
        OP_OUTLINE,
        OP_SCRIPT,
        OP_DESCRIPTION,
    }


# ---------------------------------------------------------------------------
# Determinism (same input -> same output)
# ---------------------------------------------------------------------------


def test_titles_are_deterministic_across_calls():
    provider = InMemoryGenerationProvider()
    idea = _idea()
    assert provider.generate_titles(idea, 5) == provider.generate_titles(idea, 5)


def test_thumbnails_are_deterministic_across_calls():
    provider = InMemoryGenerationProvider()
    idea = _idea()
    assert provider.generate_thumbnails(idea, 3) == provider.generate_thumbnails(idea, 3)


def test_script_artifacts_are_deterministic_across_calls():
    provider = InMemoryGenerationProvider()
    idea = _idea()
    assert provider.generate_outline(idea) == provider.generate_outline(idea)
    assert provider.generate_script(idea) == provider.generate_script(idea)
    assert provider.generate_description(idea) == provider.generate_description(idea)


def test_two_instances_same_config_produce_identical_output():
    idea = _idea()
    a = InMemoryGenerationProvider()
    b = InMemoryGenerationProvider()
    assert a.generate_titles(idea, 4) == b.generate_titles(idea, 4)
    assert a.generate_description(idea) == b.generate_description(idea)


def test_different_ideas_produce_different_output():
    provider = InMemoryGenerationProvider()
    a = provider.generate_titles(_idea("a", "Alpha"), 3)
    b = provider.generate_titles(_idea("b", "Beta"), 3)
    assert a != b


# ---------------------------------------------------------------------------
# Configurable cardinality
# ---------------------------------------------------------------------------


def test_generate_titles_honours_requested_count():
    provider = InMemoryGenerationProvider()
    assert len(provider.generate_titles(_idea(), 3)) == 3
    assert len(provider.generate_titles(_idea(), 7)) == 7


def test_generate_thumbnails_honours_requested_count():
    provider = InMemoryGenerationProvider()
    assert len(provider.generate_thumbnails(_idea(), 1)) == 1
    assert len(provider.generate_thumbnails(_idea(), 4)) == 4


def test_zero_count_returns_empty():
    provider = InMemoryGenerationProvider()
    assert provider.generate_titles(_idea(), 0) == ()
    assert provider.generate_thumbnails(_idea(), 0) == ()


def test_max_titles_ceiling_forces_underproduction():
    # A ceiling below the requested count forces fewer titles than requested,
    # which lets a consumer exercise its "cannot produce required set" path (8.5).
    provider = InMemoryGenerationProvider(max_titles=2)
    assert len(provider.generate_titles(_idea(), 5)) == 2


def test_max_thumbnails_ceiling_forces_underproduction():
    provider = InMemoryGenerationProvider(max_thumbnails=0)
    assert provider.generate_thumbnails(_idea(), 3) == ()


def test_negative_count_raises_value_error():
    provider = InMemoryGenerationProvider()
    with pytest.raises(ValueError):
        provider.generate_titles(_idea(), -1)


# ---------------------------------------------------------------------------
# Structural bounds on raw artifacts
# ---------------------------------------------------------------------------


def test_titles_are_distinct():
    provider = InMemoryGenerationProvider()
    titles = provider.generate_titles(_idea(), 8)
    assert len(set(titles)) == len(titles)


def test_titles_within_length_bounds():
    # Even with a very long source title, each title stays within 1..100 chars.
    provider = InMemoryGenerationProvider()
    idea = _idea(title="X" * 500)
    titles = provider.generate_titles(idea, 6)
    assert all(1 <= len(t) <= 100 for t in titles)


def test_thumbnail_overlay_within_30_chars():
    provider = InMemoryGenerationProvider()
    thumbnails = provider.generate_thumbnails(_idea(), 6)
    assert all(isinstance(t, ThumbnailDraft) for t in thumbnails)
    assert all(len(t.text_overlay) <= 30 for t in thumbnails)
    assert all(t.visual_description for t in thumbnails)


def test_thumbnail_reflects_idea_category():
    provider = InMemoryGenerationProvider()
    thumbnails = provider.generate_thumbnails(_idea(category=ChannelCategory.MUSIC), 1)
    assert "music" in thumbnails[0].visual_description


def test_description_within_length_bounds():
    provider = InMemoryGenerationProvider()
    description = provider.generate_description(_idea())
    assert 100 <= len(description) <= 5000


def test_outline_and_script_non_empty():
    provider = InMemoryGenerationProvider()
    idea = _idea()
    assert provider.generate_outline(idea).strip()
    assert provider.generate_script(idea).strip()


def test_empty_title_concept_falls_back_to_idea_id():
    provider = InMemoryGenerationProvider()
    idea = _idea(idea_id="fallback-id", title="   ")
    titles = provider.generate_titles(idea, 3)
    assert all("fallback-id" in t for t in titles)


# ---------------------------------------------------------------------------
# Controllable failure injection (8.5 / 10.5)
# ---------------------------------------------------------------------------


def test_failure_injection_on_titles_raises_identifying_error():
    provider = InMemoryGenerationProvider(failing_operations=frozenset({OP_TITLES}))
    idea = _idea("boom")
    with pytest.raises(GenerationError) as exc:
        provider.generate_titles(idea, 3)
    assert exc.value.item == OP_TITLES
    assert exc.value.idea_id == "boom"


@pytest.mark.parametrize(
    "operation, call",
    [
        (OP_TITLES, lambda p, i: p.generate_titles(i, 3)),
        (OP_THUMBNAILS, lambda p, i: p.generate_thumbnails(i, 1)),
        (OP_OUTLINE, lambda p, i: p.generate_outline(i)),
        (OP_SCRIPT, lambda p, i: p.generate_script(i)),
        (OP_DESCRIPTION, lambda p, i: p.generate_description(i)),
    ],
)
def test_failure_injection_per_operation(operation, call):
    provider = InMemoryGenerationProvider(failing_operations=frozenset({operation}))
    idea = _idea("x")
    with pytest.raises(GenerationError) as exc:
        call(provider, idea)
    assert exc.value.item == operation
    assert exc.value.idea_id == "x"


def test_non_failing_operations_still_succeed_when_one_fails():
    # Injecting a failure on titles must not affect the other operations.
    provider = InMemoryGenerationProvider(failing_operations=frozenset({OP_TITLES}))
    idea = _idea()
    assert provider.generate_outline(idea)
    assert provider.generate_script(idea)
    assert provider.generate_description(idea)
    assert provider.generate_thumbnails(idea, 1)


def test_no_failure_by_default():
    provider = InMemoryGenerationProvider()
    idea = _idea()
    # None of these should raise.
    provider.generate_titles(idea, 3)
    provider.generate_thumbnails(idea, 1)
    provider.generate_outline(idea)
    provider.generate_script(idea)
    provider.generate_description(idea)
