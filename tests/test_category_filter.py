"""Unit and edge-case tests for the Category_Filter (task 7.4).

Covers the concrete branches of Requirement 4:

- 4.1  Selected supported category -> only matching ideas + templates.
- 4.2  No selection but detected category -> apply detected.
- 4.3  Supported categories are exactly gaming, music, entertainment, sports.
- 4.4  No matching ideas but matching templates -> return the templates.
- 4.5  No matching ideas and no matching templates -> empty + no_matches.
- 4.6  Unsupported selected category -> unsupported-category error identifying it.
- 4.7  No selection and none detected -> category_unavailable, no filtering.

Property 7 (Hypothesis) lives in task 7.5 and is intentionally not included here.
"""

from __future__ import annotations

import enum

import pytest

from viral_topic_agent.analysis.category_filter import CategoryFilter
from viral_topic_agent.domain.models import (
    ChannelCategory,
    ContentIdea,
    TimeWindow,
    ViralTemplate,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _template(template_id: str, category: ChannelCategory) -> ViralTemplate:
    return ViralTemplate(
        template_id=template_id,
        name=f"template-{template_id}",
        category=category,
        observed_performance=1000.0,
    )


def _idea(idea_id: str, category: ChannelCategory | None) -> ContentIdea:
    return ContentIdea(
        idea_id=idea_id,
        title_concept=f"title-{idea_id}",
        rationale="metric value 5000 observed in the weekly window",
        time_window=TimeWindow.WEEKLY,
        category=category,
        templates=(_template(f"t-{idea_id}", category or ChannelCategory.GAMING),),
        observed_metric_value=5000.0,
    )


@pytest.fixture
def cf() -> CategoryFilter:
    return CategoryFilter()


# ---------------------------------------------------------------------------
# 4.3 - supported set is exactly gaming, music, entertainment, sports
# ---------------------------------------------------------------------------


def test_supported_categories_are_exactly_the_four(cf: CategoryFilter):
    assert CategoryFilter.SUPPORTED == {
        ChannelCategory.GAMING,
        ChannelCategory.MUSIC,
        ChannelCategory.ENTERTAINMENT,
        ChannelCategory.SPORTS,
    }
    # And those are exactly the members of the enum.
    assert set(ChannelCategory) == CategoryFilter.SUPPORTED


# ---------------------------------------------------------------------------
# 4.1 - selected category returns only matching ideas + templates
# ---------------------------------------------------------------------------


def test_selected_category_returns_only_matching_items(cf: CategoryFilter):
    gaming_idea = _idea("g1", ChannelCategory.GAMING)
    music_idea = _idea("m1", ChannelCategory.MUSIC)
    gaming_tpl = _template("gt", ChannelCategory.GAMING)
    music_tpl = _template("mt", ChannelCategory.MUSIC)

    result = cf.filter(
        ideas=[gaming_idea, music_idea],
        templates=[gaming_tpl, music_tpl],
        selected=ChannelCategory.GAMING,
        detected=None,
    )

    assert result.ideas == (gaming_idea,)
    assert result.templates == (gaming_tpl,)
    assert result.applied_category == ChannelCategory.GAMING
    assert result.no_matches is False
    assert result.category_unavailable is False
    assert result.error is None
    # No non-matching item leaks through.
    assert all(i.category == ChannelCategory.GAMING for i in result.ideas)
    assert all(t.category == ChannelCategory.GAMING for t in result.templates)


def test_selected_category_takes_precedence_over_detected(cf: CategoryFilter):
    gaming_idea = _idea("g1", ChannelCategory.GAMING)
    sports_idea = _idea("s1", ChannelCategory.SPORTS)

    result = cf.filter(
        ideas=[gaming_idea, sports_idea],
        templates=[],
        selected=ChannelCategory.SPORTS,
        detected=ChannelCategory.GAMING,
    )

    # Selected (sports) wins over detected (gaming).
    assert result.applied_category == ChannelCategory.SPORTS
    assert result.ideas == (sports_idea,)


# ---------------------------------------------------------------------------
# 4.2 - no selection but detected category -> apply detected
# ---------------------------------------------------------------------------


def test_detected_category_applied_when_nothing_selected(cf: CategoryFilter):
    music_idea = _idea("m1", ChannelCategory.MUSIC)
    ent_idea = _idea("e1", ChannelCategory.ENTERTAINMENT)
    music_tpl = _template("mt", ChannelCategory.MUSIC)
    ent_tpl = _template("et", ChannelCategory.ENTERTAINMENT)

    result = cf.filter(
        ideas=[music_idea, ent_idea],
        templates=[music_tpl, ent_tpl],
        selected=None,
        detected=ChannelCategory.MUSIC,
    )

    assert result.applied_category == ChannelCategory.MUSIC
    assert result.ideas == (music_idea,)
    assert result.templates == (music_tpl,)
    assert result.category_unavailable is False
    assert result.no_matches is False


# ---------------------------------------------------------------------------
# 4.4 - no matching ideas but matching templates -> return templates
# ---------------------------------------------------------------------------


def test_no_matching_ideas_but_matching_templates_returns_templates(cf: CategoryFilter):
    # Only music ideas, but a gaming template is present.
    music_idea = _idea("m1", ChannelCategory.MUSIC)
    gaming_tpl = _template("gt", ChannelCategory.GAMING)

    result = cf.filter(
        ideas=[music_idea],
        templates=[gaming_tpl],
        selected=ChannelCategory.GAMING,
        detected=None,
    )

    assert result.ideas == ()
    assert result.templates == (gaming_tpl,)
    # Templates matched, so this is NOT a no-matches result.
    assert result.no_matches is False
    assert result.applied_category == ChannelCategory.GAMING
    assert result.error is None


# ---------------------------------------------------------------------------
# 4.5 - no matching ideas and no matching templates -> empty + no_matches
# ---------------------------------------------------------------------------


def test_no_matches_returns_empty_with_indicator(cf: CategoryFilter):
    music_idea = _idea("m1", ChannelCategory.MUSIC)
    music_tpl = _template("mt", ChannelCategory.MUSIC)

    result = cf.filter(
        ideas=[music_idea],
        templates=[music_tpl],
        selected=ChannelCategory.SPORTS,  # nothing is sports
        detected=None,
    )

    assert result.ideas == ()
    assert result.templates == ()
    assert result.no_matches is True
    assert result.applied_category == ChannelCategory.SPORTS
    assert result.category_unavailable is False
    assert result.error is None


def test_no_matches_on_completely_empty_inputs(cf: CategoryFilter):
    result = cf.filter(
        ideas=[],
        templates=[],
        selected=ChannelCategory.GAMING,
        detected=None,
    )

    assert result.ideas == ()
    assert result.templates == ()
    assert result.no_matches is True
    assert result.applied_category == ChannelCategory.GAMING


# ---------------------------------------------------------------------------
# 4.6 - unsupported selected category -> error identifying it, no filtering
# ---------------------------------------------------------------------------


class _ExtendedCategory(enum.Enum):
    """A category enum value outside the supported set, for defensive 4.6 testing."""

    COOKING = "cooking"


def test_unsupported_selected_category_returns_identifying_error(cf: CategoryFilter):
    gaming_idea = _idea("g1", ChannelCategory.GAMING)
    gaming_tpl = _template("gt", ChannelCategory.GAMING)

    result = cf.filter(
        ideas=[gaming_idea],
        templates=[gaming_tpl],
        selected=_ExtendedCategory.COOKING,  # type: ignore[arg-type]
        detected=ChannelCategory.GAMING,
    )

    assert result.error is not None
    assert "unsupported-category" in result.error
    # The error identifies the offending category by its value.
    assert "cooking" in result.error
    # No filtering performed: empty result, no other indicators.
    assert result.ideas == ()
    assert result.templates == ()
    assert result.no_matches is False
    assert result.category_unavailable is False
    assert result.applied_category is None


# ---------------------------------------------------------------------------
# 4.7 - no selection and none detected -> category_unavailable, no filtering
# ---------------------------------------------------------------------------


def test_category_unavailable_when_no_selection_and_no_detected(cf: CategoryFilter):
    gaming_idea = _idea("g1", ChannelCategory.GAMING)
    music_idea = _idea("m1", ChannelCategory.MUSIC)
    gaming_tpl = _template("gt", ChannelCategory.GAMING)
    music_tpl = _template("mt", ChannelCategory.MUSIC)

    ideas = [gaming_idea, music_idea]
    templates = [gaming_tpl, music_tpl]

    result = cf.filter(
        ideas=ideas,
        templates=templates,
        selected=None,
        detected=None,
    )

    assert result.category_unavailable is True
    assert result.applied_category is None
    assert result.error is None
    assert result.no_matches is False
    # No filtering applied: outputs equal inputs (order preserved).
    assert result.ideas == tuple(ideas)
    assert result.templates == tuple(templates)
