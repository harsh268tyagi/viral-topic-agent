"""Hypothesis property test for the mandated step order (task 19.3).

Property 26 (design.md -> Automation_Scheduler / Requirement 14.3): *for any*
executed run, the Automation_Scheduler SHALL execute the steps in the mandated
order -- channel analysis, trend discovery, category filtering, idea scoring,
competitor tracking, outlier detection, then digest delivery.

This property quantifies over runs whose configuration varies (which channels
are authorized, whether a schedule is configured, whether the trigger is
manual), asserting that whenever the run actually executes its steps, the steps
that ran did so in exactly the relative order prescribed by ``STEP_ORDER``. The
per-step stubs record the real execution order, so the assertion observes the
scheduler's behaviour rather than re-deriving it.

# Feature: viral-topic-agent, Property 26: A run executes steps in the mandated order

Validates: Requirements 14.3
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from orchestration.automation_scheduler import STEP_ORDER
from domain.models import (
    AuthorizedChannel,
    Configuration,
    Schedule,
)

from .scheduler_support import TickingClock, make_scheduler


# ---------------------------------------------------------------------------
# Generators
#
# Vary the run's shape without altering the mandated order:
# - 0..3 authorized channels, each possibly connected (exercises owned-channel
#   resolution), and
# - either a valid schedule (so a non-manual trigger runs) paired with a random
#   manual flag, or no schedule paired with a manual trigger (so the run always
#   executes its steps and the property is non-vacuous).
# ---------------------------------------------------------------------------

_channels = st.lists(
    st.builds(
        AuthorizedChannel,
        channel_id=st.text(
            alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
            min_size=1,
            max_size=8,
        ),
        credentials_ref=st.just("ref"),
        connected=st.booleans(),
    ),
    min_size=0,
    max_size=3,
)


@st.composite
def _executing_scenario(draw: st.DrawFn) -> tuple[Configuration, bool]:
    """A configuration + manual flag guaranteed to execute the steps."""
    channels = tuple(draw(_channels))
    has_schedule = draw(st.booleans())
    if has_schedule:
        schedule = Schedule(recurrence_interval="daily", run_time="08:00")
        manual = draw(st.booleans())  # runs whether or not manual
    else:
        schedule = None
        manual = True  # no schedule -> must be manual to run (14.7)
    config = Configuration(
        authorized_channels=channels,
        selected_category=None,
        monitored_competitors=(),
        schedule=schedule,
        delivery_destinations=(),
    )
    return config, manual


# ---------------------------------------------------------------------------
# Property 26
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 26: A run executes steps in the mandated order
@settings(max_examples=200)
@given(scenario=_executing_scenario())
def test_run_executes_steps_in_mandated_order(
    scenario: tuple[Configuration, bool],
) -> None:
    """Every executed run runs its steps in the mandated relative order (14.3)."""
    config, manual = scenario
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)

    summary = scheduler.run(config, TickingClock(), manual=manual)

    # The run executed (these scenarios always run): a full summary, not an
    # overlap/no-schedule short-circuit.
    assert summary.overlap_skipped is False
    assert len(summary.steps) == len(STEP_ORDER)

    # All seven steps ran exactly once, in the mandated order (no failures here,
    # so none are skipped).
    assert recorder == list(STEP_ORDER)

    # The recorded execution order is consistent with STEP_ORDER's relative
    # ordering (a stronger restatement that tolerates only forward progress).
    positions = [STEP_ORDER.index(step) for step in recorder]
    assert positions == sorted(positions)

    # The summary lists the steps in the mandated order too (14.3 + 14.6).
    assert [s.step for s in summary.steps] == list(STEP_ORDER)
