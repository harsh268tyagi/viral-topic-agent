"""Hypothesis property test for the shared DigestRenderer (task 9.2).

This module hosts the single universal correctness property for the shared,
pure ``render_digest`` (design.md -> *DigestRenderer*). Concrete, hand-checked
rendering examples and the per-deliverer render-and-verify behaviour live with
the deliverers' unit tests; this module is the universal layer for the shared
renderer that every deliverer depends on.

Property 13 (rendering): *for any* ``DigestReport``, the shared rendering used
by the email, Slack, and Notion deliverers SHALL include all three report
sections and SHALL include the no-items indicator for exactly those sections
that contain zero items.

Validates: Requirements 7.2, 7.4, 8.2, 8.4, 9.2, 9.4
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from delivery.digest_renderer import NO_ITEMS_INDICATOR, render_digest
from delivery.digest_service import DigestService
from domain.models import (
    ChannelCategory,
    CompetitorSpike,
    Confidence,
    ContentIdea,
    DigestReport,
    Outlier,
    ScoredIdea,
    TimeWindow,
    ViralTemplate,
)

# ---------------------------------------------------------------------------
# Item builders (one distinct item per index so the one-line-per-item rendering
# is observable; each section's items are independent of the others).
# ---------------------------------------------------------------------------

_TEMPLATE = ViralTemplate(
    template_id="t0",
    name="tier-list ranking",
    category=ChannelCategory.GAMING,
    observed_performance=1000.0,
)


def _scored_idea(idea_id: str) -> ScoredIdea:
    idea = ContentIdea(
        idea_id=idea_id,
        title_concept="concept",
        rationale="observed metric value within the window",
        time_window=TimeWindow.WEEKLY,
        category=ChannelCategory.GAMING,
        templates=(_TEMPLATE,),
        observed_metric_value=5000.0,
    )
    return ScoredIdea(idea=idea, score=50, confidence=Confidence.NORMAL)


def _spike(video_id: str) -> CompetitorSpike:
    return CompetitorSpike(
        channel_id=f"comp-{video_id}",
        video_id=video_id,
        view_count=9000,
        spike_factor=3.5,
    )


def _outlier(video_id: str) -> Outlier:
    return Outlier(video_id=video_id, view_count=50000, outlier_factor=6.0)


# ---------------------------------------------------------------------------
# Strategy
#
# Counts span zero so both the populated and the empty (no-items) branch of
# every section are exercised, and the three counts vary independently so every
# combination of which sections are empty is reachable. The compiled report is
# the canonical way to obtain a valid three-section ``DigestReport``.
# ---------------------------------------------------------------------------

_counts = st.integers(min_value=0, max_value=5)


@st.composite
def _digest_reports(draw: st.DrawFn) -> DigestReport:
    scored = [_scored_idea(f"i{i}") for i in range(draw(_counts))]
    spikes = [_spike(f"v{i}") for i in range(draw(_counts))]
    outliers = [_outlier(f"o{i}") for i in range(draw(_counts))]
    return DigestService().compile(scored, spikes, outliers)


# ---------------------------------------------------------------------------
# Property 13
# ---------------------------------------------------------------------------


# Feature: real-provider-integration, Property 13: Digest rendering always produces three sections with correct no-items indicators
@settings(max_examples=100)
@given(report=_digest_reports())
def test_render_digest_has_three_sections_with_no_items_indicator_iff_empty(
    report: DigestReport,
) -> None:
    """Three rendered sections; the no-items indicator appears iff a section is empty.

    Validates: Requirements 7.2, 7.4, 8.2, 8.4, 9.2, 9.4
    """
    rendered = render_digest(report)

    # (7.4, 8.4, 9.4) the rendering always carries exactly three sections, in the
    # report's section order, so every deliverer transmits all three.
    assert len(rendered.sections) == 3
    assert tuple(s.item_type for s in rendered.sections) == tuple(
        s.item_type for s in report.sections
    )

    for source, rendered_section in zip(report.sections, rendered.sections):
        is_empty = len(source.items) == 0

        # (7.4, 8.4, 9.4) the no-items indicator is present on exactly the
        # sections that contain zero items.
        assert rendered_section.no_items == is_empty

        if is_empty:
            # An empty section shows the no-items indicator and nothing else.
            assert rendered_section.no_items_indicator == NO_ITEMS_INDICATOR
            assert NO_ITEMS_INDICATOR in rendered_section.lines
        else:
            # A populated section never shows the indicator and renders exactly
            # one line per item.
            assert rendered_section.no_items_indicator is None
            assert NO_ITEMS_INDICATOR not in rendered_section.lines
            assert len(rendered_section.lines) == len(source.items)
