"""Hypothesis property test for Script_Generator description length (task 14.3).

This module hosts Property 19 for :meth:`ScriptGenerator.generate`. Concrete
examples for the padding (too-short) and truncation (too-long) branches live in
``tests/test_script_generator.py``; this module is the universal layer that
asserts the description-length contract across arbitrary provider output.

Property 19 (design.md -> Property 19 / Requirement 10.3): *for any*
successfully generated script bundle, the video description SHALL be between 100
and 5000 characters inclusive.

To exercise the full input space of raw provider descriptions (arbitrarily
short, in-range, and arbitrarily long), the test injects a
``GenerationProvider`` whose ``generate_description`` returns a Hypothesis-drawn
string while delegating the other artifacts to the deterministic
``InMemoryGenerationProvider`` stub (no mocks). The ScriptGenerator is
responsible for coercing that raw output into the 100..5000 bound (10.3).

# Feature: viral-topic-agent, Property 19: Generated description length is within bounds
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import given, settings
from hypothesis import strategies as st

from generation import InMemoryGenerationProvider, ThumbnailDraft
from domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)
from generation.script_generator import (
    MAX_DESCRIPTION_CHARS,
    MIN_DESCRIPTION_CHARS,
    ScriptGenerator,
)


# ---------------------------------------------------------------------------
# A provider whose raw description is supplied by the test.
#
# Outline/script/titles/thumbnails delegate to the real deterministic stub so
# the rest of the pipeline behaves normally; only the description varies. This
# lets the property feed arbitrary raw descriptions (shorter than the minimum,
# in range, and longer than the maximum) through the generator's bounding logic.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawDescriptionProvider:
    raw_description: str
    _delegate: InMemoryGenerationProvider = field(
        default_factory=InMemoryGenerationProvider
    )

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
        return self.raw_description


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Raw provider descriptions across the whole length spectrum: well below the
# minimum (incl. empty), straddling both bounds, and well above the maximum.
_raw_descriptions = st.text(min_size=0, max_size=MAX_DESCRIPTION_CHARS + 200)


@st.composite
def _ideas(draw: st.DrawFn) -> ContentIdea:
    """A valid ContentIdea with one associated template."""
    category = draw(st.one_of(st.none(), st.sampled_from(list(ChannelCategory))))
    template = ViralTemplate(
        template_id="t0",
        name="tier-list ranking",
        category=category if category is not None else ChannelCategory.GAMING,
        observed_performance=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )
    return ContentIdea(
        idea_id=draw(st.text(min_size=1, max_size=20)),
        title_concept=draw(st.text(min_size=1, max_size=40)),
        rationale="observed metric value within the window",
        time_window=draw(st.sampled_from(list(TimeWindow))),
        category=category,
        templates=(template,),
        observed_metric_value=draw(
            st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)
        ),
    )


# ---------------------------------------------------------------------------
# Property 19
# ---------------------------------------------------------------------------


@settings(max_examples=200)
@given(idea=_ideas(), raw_description=_raw_descriptions, keyword_count=st.integers(min_value=0, max_value=30))
def test_description_length_within_bounds(
    idea: ContentIdea, raw_description: str, keyword_count: int
) -> None:
    """A successfully generated description is within [100, 5000] chars.

    Validates: Requirements 10.3
    """
    keywords = [f"keyword{i}" for i in range(keyword_count)]
    gen = _RawDescriptionProvider(raw_description=raw_description)

    result = ScriptGenerator().generate(idea, keywords, gen)

    # Generation succeeds (no failing operations injected), so the bundle exists.
    assert result.is_ok()
    bundle = result.unwrap()

    assert MIN_DESCRIPTION_CHARS <= len(bundle.description) <= MAX_DESCRIPTION_CHARS
