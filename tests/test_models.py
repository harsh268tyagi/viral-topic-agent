"""Tests for the core domain data models (task 1.2).

Confirms the models:
- import cleanly,
- are frozen (immutable),
- have field-by-field value equality (supports round-trip integrity, 15.4),
- are hashable (a consequence of being frozen with tuple collections).
"""

import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from domain import models as m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ALL_ENUMS = [
    m.ChannelCategory,
    m.TimeWindow,
    m.Confidence,
    m.VideoFormat,
    m.DeliveryDestination,
    m.StepStatus,
    m.AuthStatus,
]

ALL_DATACLASSES = [
    m.VideoStats,
    m.BaselineResult,
    m.ChannelProfile,
    m.ViralTemplate,
    m.ContentIdea,
    m.DiscoveryResult,
    m.FilterResult,
    m.ScoredIdea,
    m.CompetitorSpike,
    m.CompetitorReport,
    m.Outlier,
    m.OutlierResult,
    m.TitleConcept,
    m.ThumbnailConcept,
    m.ConceptSet,
    m.PublishRecommendation,
    m.ScriptBundle,
    m.KeywordMetric,
    m.KeywordGap,
    m.KeywordGapResult,
    m.FormatResult,
    m.DigestSection,
    m.DigestReport,
    m.DeliveryOutcome,
    m.Schedule,
    m.StepResult,
    m.RunSummary,
    m.AuthorizedChannel,
    m.AuthorizationGrant,
    m.AuthorizationResult,
    m.Configuration,
]


def _sample_video_stats() -> m.VideoStats:
    return m.VideoStats(
        video_id="v1",
        view_count=1000,
        published_at="2024-01-01T00:00:00Z",
        format=m.VideoFormat.SHORT,
    )


def _sample_template() -> m.ViralTemplate:
    return m.ViralTemplate(
        template_id="t1",
        name="tier-list ranking",
        category=m.ChannelCategory.GAMING,
        observed_performance=12345.0,
    )


def _sample_idea() -> m.ContentIdea:
    return m.ContentIdea(
        idea_id="i1",
        title_concept="Ranking every boss",
        rationale="Trending metric value 9000 in the weekly window",
        time_window=m.TimeWindow.WEEKLY,
        category=m.ChannelCategory.GAMING,
        templates=(_sample_template(),),
        observed_metric_value=9000.0,
    )


# ---------------------------------------------------------------------------
# Import / completeness
# ---------------------------------------------------------------------------


def test_all_required_enums_present():
    """Every enum named in task 1.2 is exported and is an Enum subclass."""
    from enum import Enum

    for enum_cls in ALL_ENUMS:
        assert issubclass(enum_cls, Enum)
        assert enum_cls.__name__ in m.__all__


def test_all_required_dataclasses_present():
    """Every dataclass named in task 1.2 is exported and is a dataclass."""
    for dc in ALL_DATACLASSES:
        assert dataclasses.is_dataclass(dc)
        assert dc.__name__ in m.__all__


def test_enum_values_match_design():
    """Spot-check enum values against the design document."""
    assert m.ChannelCategory.GAMING.value == "gaming"
    assert {c.value for c in m.ChannelCategory} == {
        "gaming",
        "music",
        "entertainment",
        "sports",
    }
    assert m.Confidence.LOW.value == "low_confidence"
    assert m.VideoFormat.LONG_FORM.value == "long_form"
    assert m.StepStatus.SKIPPED.value == "skipped"
    assert m.AuthStatus.AUTHORIZATION_TIMEOUT.value == "authorization-timeout"


# ---------------------------------------------------------------------------
# Frozen / immutability
# ---------------------------------------------------------------------------


def test_all_dataclasses_are_frozen():
    """Every domain dataclass is declared frozen."""
    for dc in ALL_DATACLASSES:
        assert dc.__dataclass_params__.frozen, f"{dc.__name__} is not frozen"


def test_frozen_instance_rejects_mutation():
    """Assigning to a field on a frozen instance raises FrozenInstanceError."""
    video = _sample_video_stats()
    with pytest.raises(dataclasses.FrozenInstanceError):
        video.view_count = 2  # type: ignore[misc]


def test_frozen_instances_are_hashable():
    """Frozen dataclasses with tuple collections are hashable and set-usable."""
    idea = _sample_idea()
    # Hashable instances can live in sets / dict keys.
    assert len({idea, _sample_idea()}) == 1


# ---------------------------------------------------------------------------
# Field-by-field value equality
# ---------------------------------------------------------------------------


def test_equality_is_field_by_field():
    """Two independently built instances with equal fields are equal."""
    assert _sample_video_stats() == _sample_video_stats()
    assert _sample_idea() == _sample_idea()


def test_inequality_when_a_field_differs():
    """Changing a single field breaks equality."""
    base = _sample_video_stats()
    changed = dataclasses.replace(base, view_count=base.view_count + 1)
    assert base != changed


def test_tuple_collections_compare_element_by_element():
    """Collection fields use tuples, so equality compares contents, not identity."""
    idea_a = _sample_idea()
    # Build a separate templates tuple with equal-but-distinct ViralTemplate objects.
    idea_b = dataclasses.replace(idea_a, templates=(_sample_template(),))
    assert idea_a.templates is not idea_b.templates
    assert idea_a == idea_b


def test_collection_fields_are_tuples_not_lists():
    """Nested models expose tuple collections (immutability + value equality)."""
    idea = _sample_idea()
    assert isinstance(idea.templates, tuple)

    config = m.Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=("c1", "c2"),
        schedule=None,
        delivery_destinations=(m.DeliveryDestination.EMAIL,),
    )
    assert isinstance(config.authorized_channels, tuple)
    assert isinstance(config.monitored_competitors, tuple)
    assert isinstance(config.delivery_destinations, tuple)


def test_nested_configuration_equality():
    """A deeply nested Configuration compares equal field-by-field."""

    def build() -> m.Configuration:
        return m.Configuration(
            authorized_channels=(
                m.AuthorizedChannel(
                    channel_id="ch1",
                    credentials_ref="ref-1",
                    connected=True,
                ),
            ),
            selected_category=m.ChannelCategory.MUSIC,
            monitored_competitors=("comp1",),
            schedule=m.Schedule(recurrence_interval="daily", run_time="08:00"),
            delivery_destinations=(
                m.DeliveryDestination.SLACK,
                m.DeliveryDestination.NOTION,
            ),
        )

    assert build() == build()
    assert hash(build()) == hash(build())


@settings(max_examples=100)
@given(
    video_id=st.text(min_size=1, max_size=20),
    view_count=st.integers(min_value=0, max_value=10_000_000),
    published_at=st.text(min_size=1, max_size=30),
)
def test_video_stats_equality_property(video_id, view_count, published_at):
    """For any field values, two VideoStats built alike are equal and hash alike."""
    a = m.VideoStats(video_id=video_id, view_count=view_count, published_at=published_at)
    b = m.VideoStats(video_id=video_id, view_count=view_count, published_at=published_at)
    assert a == b
    assert hash(a) == hash(b)
