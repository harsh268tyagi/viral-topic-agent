"""Hypothesis property test for run-summary completeness (task 19.5).

Property 28 (design.md -> Automation_Scheduler / Requirement 14.6): *for any*
executed run, the recorded :class:`~viral_topic_agent.models.RunSummary` SHALL
list every step with a status of succeeded, failed, or skipped, and SHALL carry
a run start time and a completion time with ``start <= completion``.

The property quantifies over an arbitrary set of failing steps (including the
empty set, i.e. an all-succeed run), so every combination of succeeded / failed
/ skipped statuses across the pipeline is exercised. A :class:`TickingClock`
whose reading strictly increases makes the ``start <= completion`` assertion
non-trivial.

# Feature: viral-topic-agent, Property 28: Run summary is complete and time-consistent

Validates: Requirements 14.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from orchestration.automation_scheduler import STEP_DEPENDENCIES, STEP_ORDER
from domain.models import StepStatus

from .scheduler_support import TickingClock, make_config, make_scheduler

_VALID_STATUSES = {StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED}


def _expected_statuses(fail_steps: set[str]) -> dict[str, StepStatus]:
    """Independently derive each step's expected status for a forward pass.

    Mirrors the scheduler's rule (without invoking it): in mandated order, a step
    is SKIPPED when any dependency did not succeed; otherwise it is FAILED when it
    is a requested failing step, else SUCCEEDED. Because ``STEP_ORDER`` is a
    topological order, dependencies are always decided before their dependents.
    """
    statuses: dict[str, StepStatus] = {}
    not_succeeded: set[str] = set()
    for step in STEP_ORDER:
        if any(dep in not_succeeded for dep in STEP_DEPENDENCIES[step]):
            statuses[step] = StepStatus.SKIPPED
            not_succeeded.add(step)
        elif step in fail_steps:
            statuses[step] = StepStatus.FAILED
            not_succeeded.add(step)
        else:
            statuses[step] = StepStatus.SUCCEEDED
    return statuses


# ---------------------------------------------------------------------------
# Property 28
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 28: Run summary is complete and time-consistent
@settings(max_examples=200)
@given(fail_steps=st.sets(st.sampled_from(STEP_ORDER)))
def test_run_summary_is_complete_and_time_consistent(
    fail_steps: set[str],
) -> None:
    """The run summary lists every step with a valid status, and the start time
    does not exceed the completion time (14.6)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder, fail_steps=fail_steps)

    summary = scheduler.run(make_config(), TickingClock(start=0.0, step=1.0), manual=True)

    # Every step appears exactly once, in the mandated order.
    listed = [s.step for s in summary.steps]
    assert listed == list(STEP_ORDER)
    assert len(listed) == len(set(listed)) == len(STEP_ORDER)

    # Every listed step carries one of the three valid statuses.
    for step_result in summary.steps:
        assert step_result.status in _VALID_STATUSES

    # Start and completion times are present and consistent (start <= end). The
    # TickingClock advances on each read, so completion is strictly later, which
    # also confirms completion is read after start.
    assert summary.started_at is not None
    assert summary.completed_at is not None
    assert float(summary.started_at) <= float(summary.completed_at)

    # An executed run is not an overlap-skipped summary.
    assert summary.overlap_skipped is False

    # The statuses match an independently-derived forward pass over the
    # dependency graph: a requested failing step is FAILED only when it actually
    # ran (none of its dependencies failed first), otherwise it is SKIPPED.
    statuses = {s.step: s.status for s in summary.steps}
    assert statuses == _expected_statuses(fail_steps)
