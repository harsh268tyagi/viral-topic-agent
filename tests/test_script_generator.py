"""Example/edge-case tests for script generation (task 14.1).

Covers the concrete branches of ``ScriptGenerator.generate`` for Requirement 10:

- produces outline, script draft, SEO tags, and description (10.1),
- SEO tags include every analyzer keyword and total 5..30 (10.2),
- description is bounded to 100..5000 chars (10.3),
- no keywords supplied -> artifacts produced + ``seo_tags_unavailable`` (10.4),
- a failed item -> ``Err(ScriptError)`` naming the item and retaining the idea
  for retry (10.5).

The Hypothesis property tests (Property 18 for SEO-tag bounds, Property 19 for
description length) live in separate tasks (14.2, 14.3); this module focuses on
examples and boundary cases. Tests exercise the real ``ScriptGenerator`` against
the deterministic ``InMemoryGenerationProvider`` stub (no mocks).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from viral_topic_agent.generation import (
    OP_DESCRIPTION,
    OP_OUTLINE,
    OP_SCRIPT,
    GenerationProvider,
    InMemoryGenerationProvider,
    ThumbnailDraft,
)
from viral_topic_agent.domain.models import (
    ChannelCategory,
    ContentIdea,
    ScriptBundle,
    TimeWindow,
    ViralTemplate,
)
from viral_topic_agent.generation.script_generator import (
    MAX_DESCRIPTION_CHARS,
    MAX_SEO_TAGS,
    MIN_DESCRIPTION_CHARS,
    MIN_SEO_TAGS,
    ScriptError,
    ScriptGenerator,
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


def _keywords(n: int) -> list[str]:
    return [f"keyword{i}" for i in range(n)]


@dataclass(frozen=True)
class _FixedDescriptionProvider:
    """A GenerationProvider whose description is a fixed string.

    Lets a test exercise the ScriptGenerator's description padding/truncation
    (10.3) without monkeypatching the frozen ``InMemoryGenerationProvider``.
    Outline/script/thumbnails/titles delegate to the real stub so the rest of
    the pipeline behaves normally.
    """

    description: str
    _delegate: InMemoryGenerationProvider = InMemoryGenerationProvider()

    def generate_titles(self, idea: ContentIdea, count: int) -> tuple[str, ...]:
        return self._delegate.generate_titles(idea, count)

    def generate_thumbnails(
        self, idea: ContentIdea, count: int
    ) -> tuple[ThumbnailDraft, ...]:
        return self._delegate.generate_thumbnails(idea, count)

    def generate_outline(self, idea: ContentIdea) -> str:
        return self._delegate.generate_outline(idea)

    def generate_script(self, idea: ContentIdea) -> str:
        return self._delegate.generate_script(idea)

    def generate_description(self, idea: ContentIdea) -> str:
        return self.description


# ---------------------------------------------------------------------------
# Happy path: all four artifacts produced (10.1)
# ---------------------------------------------------------------------------


def test_generate_produces_all_artifacts():
    result = ScriptGenerator().generate(
        _idea(), _keywords(6), InMemoryGenerationProvider()
    )

    assert result.is_ok()
    bundle = result.unwrap()
    assert isinstance(bundle, ScriptBundle)
    assert bundle.idea_id == "idea-1"
    assert bundle.outline.strip()
    assert bundle.script.strip()
    assert bundle.description.strip()
    assert len(bundle.seo_tags) >= MIN_SEO_TAGS
    assert bundle.seo_tags_unavailable is False


def test_generate_is_deterministic():
    gen = InMemoryGenerationProvider()
    idea = _idea()
    a = ScriptGenerator().generate(idea, _keywords(8), gen).unwrap()
    b = ScriptGenerator().generate(idea, _keywords(8), gen).unwrap()
    assert a == b


# ---------------------------------------------------------------------------
# SEO tags include every analyzer keyword and total 5..30 (10.2)
# ---------------------------------------------------------------------------


def test_seo_tags_include_every_supplied_keyword():
    keywords = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    for kw in keywords:
        assert kw in bundle.seo_tags


def test_seo_tags_within_bounds_when_many_keywords():
    bundle = (
        ScriptGenerator()
        .generate(_idea(), _keywords(20), InMemoryGenerationProvider())
        .unwrap()
    )
    assert MIN_SEO_TAGS <= len(bundle.seo_tags) <= MAX_SEO_TAGS
    assert bundle.seo_tags_unavailable is False


def test_few_keywords_are_augmented_to_minimum():
    # Two analyzer keywords -> augmented up to the 5-tag minimum, keywords kept.
    keywords = ["speedrun", "glitch"]
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    assert len(bundle.seo_tags) >= MIN_SEO_TAGS
    assert len(bundle.seo_tags) <= MAX_SEO_TAGS
    for kw in keywords:
        assert kw in bundle.seo_tags
    assert bundle.seo_tags_unavailable is False


def test_single_keyword_is_augmented_and_retained():
    bundle = (
        ScriptGenerator()
        .generate(_idea(), ["onlyone"], InMemoryGenerationProvider())
        .unwrap()
    )
    assert "onlyone" in bundle.seo_tags
    assert len(bundle.seo_tags) >= MIN_SEO_TAGS


def test_exactly_five_keywords_not_augmented():
    keywords = ["a", "b", "c", "d", "e"]
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    assert len(bundle.seo_tags) == MIN_SEO_TAGS
    assert set(bundle.seo_tags) == set(keywords)


def test_duplicate_keywords_are_deduped():
    keywords = ["dup", "Dup", "DUP", "unique"]
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    # "dup" appears once (case-insensitive dedupe), "unique" once.
    lowered = [t.casefold() for t in bundle.seo_tags]
    assert lowered.count("dup") == 1
    assert "unique" in bundle.seo_tags


def test_seo_tags_have_no_duplicates():
    bundle = (
        ScriptGenerator()
        .generate(_idea(), _keywords(3), InMemoryGenerationProvider())
        .unwrap()
    )
    lowered = [t.casefold() for t in bundle.seo_tags]
    assert len(lowered) == len(set(lowered))


def test_blank_keywords_are_ignored():
    # Whitespace-only / empty keywords don't count toward the supplied set.
    keywords = ["  ", "", "realtag"]
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    assert "realtag" in bundle.seo_tags
    assert "" not in bundle.seo_tags
    assert all(t.strip() for t in bundle.seo_tags)
    assert bundle.seo_tags_unavailable is False


def test_excess_keywords_preserve_superset_invariant():
    # Requirement 10.2 has two clauses: "include every analyzer keyword" and
    # "total 5..30". When more than MAX_SEO_TAGS distinct keywords are supplied
    # these conflict; the design data model (``seo_tags # 5..30, superset of
    # analyzer keywords``) treats the superset as authoritative, so analyzer
    # keywords are never dropped. The 5..30 bound governs the normal input space
    # (<= 30 supplied keywords); see test below.
    keywords = _keywords(50)
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    for kw in keywords:
        assert kw in bundle.seo_tags


def test_thirty_keywords_stay_within_upper_bound():
    # Exactly the maximum supplied -> all kept, count equals the cap.
    keywords = _keywords(MAX_SEO_TAGS)
    bundle = (
        ScriptGenerator()
        .generate(_idea(), keywords, InMemoryGenerationProvider())
        .unwrap()
    )
    assert len(bundle.seo_tags) == MAX_SEO_TAGS
    for kw in keywords:
        assert kw in bundle.seo_tags


# ---------------------------------------------------------------------------
# Description length bounds (10.3)
# ---------------------------------------------------------------------------


def test_description_within_bounds():
    bundle = (
        ScriptGenerator()
        .generate(_idea(), _keywords(6), InMemoryGenerationProvider())
        .unwrap()
    )
    assert MIN_DESCRIPTION_CHARS <= len(bundle.description) <= MAX_DESCRIPTION_CHARS


def test_short_provider_description_is_padded():
    gen = _FixedDescriptionProvider(description="tiny")
    bundle = ScriptGenerator().generate(_idea(), _keywords(6), gen).unwrap()
    assert len(bundle.description) >= MIN_DESCRIPTION_CHARS


def test_long_provider_description_is_truncated():
    gen = _FixedDescriptionProvider(description="x" * 10_000)
    bundle = ScriptGenerator().generate(_idea(), _keywords(6), gen).unwrap()
    assert len(bundle.description) == MAX_DESCRIPTION_CHARS


# ---------------------------------------------------------------------------
# No keywords -> artifacts produced + seo_tags_unavailable (10.4)
# ---------------------------------------------------------------------------


def test_no_keywords_sets_seo_tags_unavailable():
    result = ScriptGenerator().generate(_idea(), [], InMemoryGenerationProvider())

    assert result.is_ok()
    bundle = result.unwrap()
    # Outline / script / description are still produced (10.4).
    assert bundle.outline.strip()
    assert bundle.script.strip()
    assert MIN_DESCRIPTION_CHARS <= len(bundle.description) <= MAX_DESCRIPTION_CHARS
    # SEO tags marked unavailable with an empty set.
    assert bundle.seo_tags_unavailable is True
    assert bundle.seo_tags == ()


def test_all_blank_keywords_treated_as_no_keywords():
    bundle = (
        ScriptGenerator()
        .generate(_idea(), ["   ", "", "\t"], InMemoryGenerationProvider())
        .unwrap()
    )
    assert bundle.seo_tags_unavailable is True
    assert bundle.seo_tags == ()


# ---------------------------------------------------------------------------
# Failure paths -> Err(ScriptError) identifying item + retaining idea (10.5)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failing_op", [OP_OUTLINE, OP_SCRIPT, OP_DESCRIPTION])
def test_generation_failure_returns_identifying_error(failing_op):
    idea = _idea("retry-me")
    gen = InMemoryGenerationProvider(failing_operations=frozenset({failing_op}))

    result = ScriptGenerator().generate(idea, _keywords(6), gen)

    assert result.is_err()
    err = result.unwrap_err()
    assert isinstance(err, ScriptError)
    # Names the failed item (10.5).
    assert err.failed_item == failing_op
    # Retains the selected idea for retry (10.5).
    assert err.idea is idea
    assert err.idea.idea_id == "retry-me"


def test_outline_failure_short_circuits_no_partial_bundle():
    gen = InMemoryGenerationProvider(failing_operations=frozenset({OP_OUTLINE}))
    result = ScriptGenerator().generate(_idea(), _keywords(6), gen)
    assert result.is_err()
    # No bundle is produced on failure.
    with pytest.raises(Exception):
        result.unwrap()


def test_failure_path_independent_of_keywords():
    # Even with no keywords supplied, a generation failure still surfaces (10.5
    # takes precedence over the 10.4 path for the failed artifact).
    gen = InMemoryGenerationProvider(failing_operations=frozenset({OP_SCRIPT}))
    result = ScriptGenerator().generate(_idea(), [], gen)
    assert result.is_err()
    assert result.unwrap_err().failed_item == OP_SCRIPT
