"""Example/edge-case tests for the Concept_Generator (task 12.2).

Covers ``ConceptGenerator.generate`` for Requirement 8:

- >= 3 distinct title concepts (8.1), each 1..100 chars inclusive (8.3),
- >= 1 thumbnail concept with a visual description and an overlay <= 30 chars (8.2),
- concepts tagged with the idea's category when present (8.4),
- inability to produce the required set -> no partial concepts + an error
  identifying the idea (8.5).

These are unit/example tests; the Hypothesis property test for the structural
constraints (Property 15) lives in a separate task (12.3). They use the real
``InMemoryGenerationProvider`` plus a few small in-test fakes to exercise the
normalization branches (truncation, de-duplication, dropping invalid drafts)
without mocks.
"""

from __future__ import annotations

import pytest

from generation.concept_generator import (
    MAX_OVERLAY_CHARS,
    MAX_TITLE_CHARS,
    MIN_THUMBNAILS,
    MIN_TITLES,
    REASON_GENERATION_FAILED,
    REASON_INSUFFICIENT_TITLES,
    REASON_NO_THUMBNAIL,
    ConceptError,
    ConceptGenerator,
)
from generation import (
    OP_THUMBNAILS,
    OP_TITLES,
    GenerationError,
    InMemoryGenerationProvider,
    ThumbnailDraft,
)
from domain.models import (
    ChannelCategory,
    ConceptSet,
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


class _FakeProvider:
    """A minimal hand-rolled provider that returns fixed raw artifacts.

    Lets a test feed exactly the raw titles/thumbnails it wants so the
    normalization branches (truncation, de-duplication, dropping invalid
    drafts) can be exercised directly. Only the two methods the
    Concept_Generator uses are implemented.
    """

    def __init__(
        self,
        titles: tuple[str, ...] = (),
        thumbnails: tuple[ThumbnailDraft, ...] = (),
    ) -> None:
        self._titles = titles
        self._thumbnails = thumbnails

    def generate_titles(self, idea: ContentIdea, count: int) -> tuple[str, ...]:
        return self._titles

    def generate_thumbnails(
        self, idea: ContentIdea, count: int
    ) -> tuple[ThumbnailDraft, ...]:
        return self._thumbnails


_GOOD_THUMB = ThumbnailDraft(
    visual_description="A bold gaming scene", text_overlay="WOW"
)


# ---------------------------------------------------------------------------
# Happy path (8.1, 8.2, 8.3, 8.4)
# ---------------------------------------------------------------------------


def test_generate_returns_concept_set_with_in_memory_provider():
    result = ConceptGenerator().generate(_idea(), InMemoryGenerationProvider())

    assert result.is_ok()
    concepts = result.unwrap()
    assert isinstance(concepts, ConceptSet)
    assert concepts.idea_id == "idea-1"


def test_produces_at_least_three_distinct_titles():
    concepts = ConceptGenerator().generate(_idea(), InMemoryGenerationProvider()).unwrap()

    assert len(concepts.titles) >= MIN_TITLES
    texts = [t.text for t in concepts.titles]
    assert len(set(texts)) == len(texts)  # distinct


def test_each_title_within_1_to_100_chars():
    concepts = ConceptGenerator().generate(
        _idea(title="X" * 500), InMemoryGenerationProvider()
    ).unwrap()

    assert all(1 <= len(t.text) <= MAX_TITLE_CHARS for t in concepts.titles)


def test_produces_at_least_one_thumbnail_with_bounded_overlay():
    concepts = ConceptGenerator().generate(_idea(), InMemoryGenerationProvider()).unwrap()

    assert len(concepts.thumbnails) >= MIN_THUMBNAILS
    for thumb in concepts.thumbnails:
        assert thumb.visual_description
        assert len(thumb.text_overlay) <= MAX_OVERLAY_CHARS


def test_concept_set_tagged_with_idea_category_when_present():
    concepts = ConceptGenerator().generate(
        _idea(category=ChannelCategory.MUSIC), InMemoryGenerationProvider()
    ).unwrap()

    assert concepts.category is ChannelCategory.MUSIC


def test_concept_set_category_none_when_idea_has_none():
    concepts = ConceptGenerator().generate(
        _idea(category=None), InMemoryGenerationProvider()
    ).unwrap()

    assert concepts.category is None


# ---------------------------------------------------------------------------
# Normalization branches (8.1, 8.3, 8.2)
# ---------------------------------------------------------------------------


def test_duplicate_raw_titles_are_deduplicated_then_fail_if_too_few():
    # Three raw titles but only one distinct value -> fewer than MIN_TITLES.
    provider = _FakeProvider(
        titles=("Same Title", "Same Title", "Same Title"),
        thumbnails=(_GOOD_THUMB,),
    )
    result = ConceptGenerator().generate(_idea("dupes"), provider)

    assert result.is_err()
    assert result.unwrap_err().reason == REASON_INSUFFICIENT_TITLES


def test_distinct_titles_survive_deduplication():
    provider = _FakeProvider(
        titles=("Alpha", "Alpha", "Beta", "Gamma", "Beta"),
        thumbnails=(_GOOD_THUMB,),
    )
    concepts = ConceptGenerator().generate(_idea(), provider).unwrap()

    texts = [t.text for t in concepts.titles]
    assert texts == ["Alpha", "Beta", "Gamma"]


def test_long_raw_titles_are_truncated_to_100_chars():
    provider = _FakeProvider(
        titles=("A" * 200, "B" * 150, "C" * 101),
        thumbnails=(_GOOD_THUMB,),
    )
    concepts = ConceptGenerator().generate(_idea(), provider).unwrap()

    assert all(len(t.text) == MAX_TITLE_CHARS for t in concepts.titles)


def test_blank_and_whitespace_titles_are_dropped():
    # Two valid titles plus blanks -> only 2 distinct valid -> insufficient.
    provider = _FakeProvider(
        titles=("Valid One", "   ", "", "Valid Two", "\t\n"),
        thumbnails=(_GOOD_THUMB,),
    )
    result = ConceptGenerator().generate(_idea("blanks"), provider)

    assert result.is_err()
    assert result.unwrap_err().reason == REASON_INSUFFICIENT_TITLES


def test_overlay_truncated_to_30_chars():
    provider = _FakeProvider(
        titles=("Alpha", "Beta", "Gamma"),
        thumbnails=(
            ThumbnailDraft(visual_description="Scene", text_overlay="X" * 80),
        ),
    )
    concepts = ConceptGenerator().generate(_idea(), provider).unwrap()

    assert len(concepts.thumbnails[0].text_overlay) == MAX_OVERLAY_CHARS


def test_thumbnail_without_overlay_is_dropped():
    provider = _FakeProvider(
        titles=("Alpha", "Beta", "Gamma"),
        thumbnails=(
            ThumbnailDraft(visual_description="Scene only", text_overlay="   "),
        ),
    )
    result = ConceptGenerator().generate(_idea("no-overlay"), provider)

    assert result.is_err()
    assert result.unwrap_err().reason == REASON_NO_THUMBNAIL


def test_thumbnail_without_visual_is_dropped():
    provider = _FakeProvider(
        titles=("Alpha", "Beta", "Gamma"),
        thumbnails=(ThumbnailDraft(visual_description="  ", text_overlay="HYPE"),),
    )
    result = ConceptGenerator().generate(_idea("no-visual"), provider)

    assert result.is_err()
    assert result.unwrap_err().reason == REASON_NO_THUMBNAIL


def test_valid_thumbnail_kept_even_when_some_drafts_invalid():
    provider = _FakeProvider(
        titles=("Alpha", "Beta", "Gamma"),
        thumbnails=(
            ThumbnailDraft(visual_description="", text_overlay="DROP"),
            ThumbnailDraft(visual_description="Keep me", text_overlay="KEEP"),
        ),
    )
    concepts = ConceptGenerator().generate(_idea(), provider).unwrap()

    assert len(concepts.thumbnails) == 1
    assert concepts.thumbnails[0].visual_description == "Keep me"


# ---------------------------------------------------------------------------
# Failure path: no partial output, error identifies the idea (8.5)
# ---------------------------------------------------------------------------


def test_too_few_distinct_titles_returns_error_no_partial():
    # Provider ceiling forces under-production of titles.
    provider = InMemoryGenerationProvider(max_titles=MIN_TITLES - 1)
    result = ConceptGenerator().generate(_idea("scarce"), provider)

    assert result.is_err()
    error = result.unwrap_err()
    assert isinstance(error, ConceptError)
    assert error.idea_id == "scarce"
    assert error.reason == REASON_INSUFFICIENT_TITLES


def test_no_valid_thumbnail_returns_error_no_partial():
    provider = InMemoryGenerationProvider(max_thumbnails=0)
    result = ConceptGenerator().generate(_idea("nothumbs"), provider)

    assert result.is_err()
    error = result.unwrap_err()
    assert error.idea_id == "nothumbs"
    assert error.reason == REASON_NO_THUMBNAIL


def test_provider_failure_on_titles_returns_error_identifying_idea():
    provider = InMemoryGenerationProvider(failing_operations=frozenset({OP_TITLES}))
    result = ConceptGenerator().generate(_idea("boom"), provider)

    assert result.is_err()
    error = result.unwrap_err()
    assert error.idea_id == "boom"
    assert error.reason == REASON_GENERATION_FAILED
    assert error.failed_item == OP_TITLES


def test_provider_failure_on_thumbnails_returns_error_identifying_idea():
    provider = InMemoryGenerationProvider(
        failing_operations=frozenset({OP_THUMBNAILS})
    )
    result = ConceptGenerator().generate(_idea("boom2"), provider)

    assert result.is_err()
    error = result.unwrap_err()
    assert error.idea_id == "boom2"
    assert error.reason == REASON_GENERATION_FAILED
    assert error.failed_item == OP_THUMBNAILS


def test_error_result_carries_no_concept_set():
    provider = InMemoryGenerationProvider(failing_operations=frozenset({OP_TITLES}))
    result = ConceptGenerator().generate(_idea(), provider)

    # The Err variant exposes no success value (no partial ConceptSet).
    with pytest.raises(Exception):
        _ = result.value
