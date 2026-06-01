"""Shared, pure rendering of a :class:`DigestReport` (Requirements 7.4, 8.4, 9.4).

The three real deliverers -- :class:`EmailDeliverer`, :class:`SlackDeliverer`,
and :class:`NotionDeliverer` (tasks 9.3, 9.4, 9.5) -- must each transmit a
report that includes **all three** report sections and a **no-items indicator**
for every section that contains zero items (Requirements 7.4, 8.4, 9.4). Rather
than re-implement that guarantee three times (and risk one deliverer drifting),
the guarantee is implemented **once** here, in a pure function, and shared by
every deliverer. This module is the single target of Property 13 (task 9.2):

    *For any* ``DigestReport``, the shared rendering used by the email, Slack,
    and Notion deliverers SHALL include all three report sections and SHALL
    include the no-items indicator for exactly those sections that contain zero
    items.

Design references (``.kiro/specs/real-provider-integration/design.md`` ->
*DigestRenderer*): "A pure function from ``DigestReport`` to a rendered payload
... that guarantees all three sections and the per-empty-section ``no-items``
indicator. Shared by all three deliverers; the single target of the rendering
properties."

Purity & layering
-----------------
:func:`render_digest` performs no I/O, reads no clock, and mutates nothing: the
same :class:`DigestReport` always renders to an equal :class:`RenderedDigest`.
The result carries the destination-neutral *content* (a subject plus the three
rendered sections); each deliverer maps that content onto its own transport
shape (a MIME message for email, blocks for Slack, properties for Notion)
without having to re-derive the three-sections-plus-indicators guarantee.

Requirements traceability: 7.4, 8.4, 9.4.
"""

from __future__ import annotations

from dataclasses import dataclass

from delivery.digest_service import (
    SECTION_COMPETITOR_SPIKES,
    SECTION_OUTLIERS,
    SECTION_SCORED_IDEAS,
)
from domain.models import DigestReport, DigestSection

__all__ = [
    "DIGEST_SUBJECT",
    "NO_ITEMS_INDICATOR",
    "SECTION_HEADINGS",
    "RenderedSection",
    "RenderedDigest",
    "render_digest",
]


# Subject / title shared by every destination's rendering.
DIGEST_SUBJECT = "Viral Topic Agent Digest"

# The marker placed in a section that contains zero items (7.4, 8.4, 9.4).
NO_ITEMS_INDICATOR = "No items"

# Human-readable heading for each known section item type. Unknown item types
# fall back to a title-cased form of the raw identifier so a section is never
# dropped from the rendering.
SECTION_HEADINGS = {
    SECTION_SCORED_IDEAS: "Scored Ideas",
    SECTION_COMPETITOR_SPIKES: "Competitor Spikes",
    SECTION_OUTLIERS: "Outliers",
}


# ---------------------------------------------------------------------------
# Rendered payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedSection:
    """One rendered report section in destination-neutral form.

    ``no_items`` is ``True`` exactly when the source section contained zero
    items, and ``no_items_indicator`` is non-``None`` on exactly those same
    sections (it holds :data:`NO_ITEMS_INDICATOR`). ``lines`` is the body the
    deliverer renders under ``heading``: one formatted line per item, or a
    single no-items indicator line when the section is empty.
    """

    item_type: str
    heading: str
    no_items: bool
    no_items_indicator: str | None
    lines: tuple[str, ...]


@dataclass(frozen=True)
class RenderedDigest:
    """The destination-neutral rendered payload of a :class:`DigestReport`.

    ``sections`` always holds exactly three :class:`RenderedSection`s, in the
    report's section order, so every consuming deliverer transmits all three
    sections (7.4, 8.4, 9.4). :attr:`body` is a ready-to-send plain-text
    rendering that every deliverer can reuse or adapt.
    """

    subject: str
    sections: tuple[RenderedSection, RenderedSection, RenderedSection]

    @property
    def body(self) -> str:
        """A plain-text rendering: the subject followed by each section block."""
        blocks: list[str] = [self.subject]
        for section in self.sections:
            blocks.append(
                "\n".join((section.heading, *section.lines))
            )
        return "\n\n".join(blocks)


# ---------------------------------------------------------------------------
# Per-item-type line formatting
# ---------------------------------------------------------------------------


def _format_scored_idea(item: object) -> str:
    """Render a ``ScoredIdea`` as a single concise line."""
    idea = getattr(item, "idea", None)
    title = getattr(idea, "title_concept", None) or "(untitled idea)"
    score = getattr(item, "score", None)
    confidence = getattr(item, "confidence", None)
    confidence_label = getattr(confidence, "value", confidence)
    score_label = "no score" if score is None else f"score {score}"
    return f"- {title} — {score_label} ({confidence_label})"


def _format_competitor_spike(item: object) -> str:
    """Render a ``CompetitorSpike`` as a single concise line."""
    channel_id = getattr(item, "channel_id", "(unknown channel)")
    video_id = getattr(item, "video_id", "(unknown video)")
    view_count = getattr(item, "view_count", 0)
    spike_factor = getattr(item, "spike_factor", 0.0)
    return (
        f"- {channel_id} / {video_id} — {view_count:,} views "
        f"({spike_factor:.1f}x baseline)"
    )


def _format_outlier(item: object) -> str:
    """Render an ``Outlier`` as a single concise line."""
    video_id = getattr(item, "video_id", "(unknown video)")
    view_count = getattr(item, "view_count", 0)
    outlier_factor = getattr(item, "outlier_factor", 0.0)
    return f"- {video_id} — {view_count:,} views ({outlier_factor:.1f}x baseline)"


# Dispatch table from a section item type to its per-item formatter. A section
# whose item type is unknown falls back to ``str(item)`` so it still renders.
_ITEM_FORMATTERS = {
    SECTION_SCORED_IDEAS: _format_scored_idea,
    SECTION_COMPETITOR_SPIKES: _format_competitor_spike,
    SECTION_OUTLIERS: _format_outlier,
}


def _render_section(section: DigestSection) -> RenderedSection:
    """Render one :class:`DigestSection`, inserting the no-items indicator.

    "Empty" is decided by the section's actual item count rather than by trusting
    the ``no_items`` flag, so the rendering guarantee holds for any report.
    """
    is_empty = len(section.items) == 0
    heading = SECTION_HEADINGS.get(
        section.item_type, section.item_type.replace("_", " ").title()
    )

    if is_empty:
        return RenderedSection(
            item_type=section.item_type,
            heading=heading,
            no_items=True,
            no_items_indicator=NO_ITEMS_INDICATOR,
            lines=(NO_ITEMS_INDICATOR,),
        )

    formatter = _ITEM_FORMATTERS.get(section.item_type, lambda item: f"- {item}")
    lines = tuple(formatter(item) for item in section.items)
    return RenderedSection(
        item_type=section.item_type,
        heading=heading,
        no_items=False,
        no_items_indicator=None,
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Public pure entry point
# ---------------------------------------------------------------------------


def render_digest(report: DigestReport) -> RenderedDigest:
    """Render ``report`` into a destination-neutral :class:`RenderedDigest`.

    Pure: equal reports render to equal results, with no I/O or hidden state.
    The result always contains all three report sections and a no-items
    indicator for exactly those sections that contain zero items (7.4, 8.4,
    9.4).
    """
    rendered = tuple(_render_section(section) for section in report.sections)
    # ``DigestReport.sections`` is typed as exactly three sections; preserve that
    # shape on the rendered payload so consumers can rely on it.
    return RenderedDigest(subject=DIGEST_SUBJECT, sections=rendered)
