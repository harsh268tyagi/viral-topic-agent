"""Hypothesis property test for competitor registration (task 9.2).

Validates Property 11 for
:meth:`~viral_topic_agent.competitor_tracker.CompetitorTracker.add_competitor`.

Property 11 (design.md -> Correctness Properties): *For any* configuration and
competitor id, adding an id not already present SHALL store exactly that id and
leave all other monitored competitors unchanged, and adding an id that is
already present SHALL leave the set unchanged (idempotent). The "within the
limit" qualifier means the order-insensitivity/idempotency claim is exercised
while the configuration holds fewer than ``MAX_COMPETITORS`` competitors (the
at-the-limit rejection is covered by the unit test for 6.8).

"Order-insensitive" is checked by adding the same set of ids in two different
orders and asserting the resulting monitored set is identical; "idempotent" is
checked by re-adding an already-present id and asserting the configuration is
unchanged.

# Feature: viral-topic-agent, Property 11: Adding a competitor is order-insensitive and idempotent within the limit

Validates: Requirements 6.1
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.analysis.competitor_tracker import CompetitorTracker
from viral_topic_agent.domain.models import Configuration

# Keep generated id sets comfortably below the 50-competitor cap so the
# property exercises the "within the limit" regime (6.1), not the rejection
# branch (6.8, covered separately).
_MAX_IDS_UNDER_LIMIT = 20

# Competitor ids drawn from a small alphabet so collisions (re-adds) occur
# naturally and the idempotency branch is exercised, while still allowing many
# distinct ids.
_channel_ids = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789-",
    min_size=1,
    max_size=8,
)


def _empty_config() -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=None,
        delivery_destinations=(),
    )


def _add_all(ids: list[str]) -> Configuration:
    """Add every id in order to a fresh configuration, returning the result."""
    tracker = CompetitorTracker()
    config = _empty_config()
    for channel_id in ids:
        config = tracker.add_competitor(config, channel_id).config
    return config


# Feature: viral-topic-agent, Property 11: Adding a competitor is order-insensitive and idempotent within the limit
@settings(max_examples=200)
@given(
    ids=st.lists(_channel_ids, min_size=0, max_size=_MAX_IDS_UNDER_LIMIT),
    permutation_seed=st.randoms(use_true_random=False),
)
def test_add_competitor_is_order_insensitive_and_idempotent(
    ids: list[str], permutation_seed
) -> None:
    """Adding ids is order-insensitive, idempotent, and stores exactly each id.

    Validates: Requirements 6.1
    """
    tracker = CompetitorTracker()

    # --- Order-insensitivity: a different add order yields the same set. ----
    shuffled = list(ids)
    permutation_seed.shuffle(shuffled)

    config_a = _add_all(ids)
    config_b = _add_all(shuffled)

    # The monitored set is identical regardless of insertion order.
    assert set(config_a.monitored_competitors) == set(config_b.monitored_competitors)
    # It is exactly the set of distinct input ids (each distinct id stored once).
    assert set(config_a.monitored_competitors) == set(ids)
    # No duplicates are ever stored.
    assert len(config_a.monitored_competitors) == len(set(ids))

    # --- Adding a brand-new id stores exactly that id, others unchanged. ----
    # Pick an id guaranteed not to already be present.
    new_id = "zzz-new-competitor"
    assert new_id not in config_a.monitored_competitors
    before = config_a.monitored_competitors
    add_new = tracker.add_competitor(config_a, new_id)
    assert add_new.added is True
    # Exactly the new id was appended; the previously monitored ids are intact.
    assert add_new.config.monitored_competitors == before + (new_id,)

    # --- Idempotency: re-adding an existing id leaves the set unchanged. ----
    if config_a.monitored_competitors:
        existing_id = config_a.monitored_competitors[0]
        re_add = tracker.add_competitor(config_a, existing_id)
        assert re_add.added is False
        assert re_add.config.monitored_competitors == config_a.monitored_competitors
        # The configuration object itself is returned unchanged.
        assert re_add.config == config_a
