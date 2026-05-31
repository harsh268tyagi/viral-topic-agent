"""Hypothesis property test for failed-step dependency skipping (task 19.4).

Property 27 (design.md -> Automation_Scheduler / Requirement 14.5): *for any*
single failing step, the Automation_Scheduler SHALL record that step as failed,
SHALL skip every step whose input depends (transitively) on the failed step's
output, and SHALL continue to execute every step that does not depend on the
failed step's output.

The property is checked against an independently computed expectation: from the
declared :data:`STEP_DEPENDENCIES` graph we derive, for the chosen failing step,
the set of transitive dependents (which must be skipped) and the complementary
set of independents (which must succeed). The per-step stubs record which steps
actually ran, so we can assert the independents truly executed and the
dependents truly did not.

# Feature: viral-topic-agent, Property 27: A failed step skips only its dependents and continues independents

Validates: Requirements 14.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from viral_topic_agent.orchestration.automation_scheduler import (
    STEP_DEPENDENCIES,
    STEP_ORDER,
)
from viral_topic_agent.domain.models import StepStatus

from .scheduler_support import TickingClock, make_config, make_scheduler


def _transitive_dependents(failed: str) -> set[str]:
    """All steps that depend, directly or transitively, on ``failed``.

    Computed independently of the scheduler from the declared dependency graph:
    a step is a dependent if any of its direct dependencies is the failed step
    or is itself a dependent. Iterates to a fixed point over the DAG.
    """
    dependents: set[str] = set()
    changed = True
    while changed:
        changed = False
        for step, deps in STEP_DEPENDENCIES.items():
            if step in dependents or step == failed:
                continue
            if any(dep == failed or dep in dependents for dep in deps):
                dependents.add(step)
                changed = True
    return dependents


# ---------------------------------------------------------------------------
# Property 27
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 27: A failed step skips only its dependents and continues independents
@settings(max_examples=100)
@given(failed_step=st.sampled_from(STEP_ORDER))
def test_failed_step_skips_only_dependents_and_continues_independents(
    failed_step: str,
) -> None:
    """A single failed step is recorded failed; its transitive dependents are
    skipped; every independent step still runs (14.5)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder, fail_steps=[failed_step])

    summary = scheduler.run(make_config(), TickingClock(), manual=True)
    statuses = {s.step: s.status for s in summary.steps}

    dependents = _transitive_dependents(failed_step)
    independents = set(STEP_ORDER) - dependents - {failed_step}

    # The failed step is recorded as failed.
    assert statuses[failed_step] == StepStatus.FAILED

    # Every transitive dependent is skipped and never executed.
    for step in dependents:
        assert statuses[step] == StepStatus.SKIPPED, step
        assert step not in recorder, step

    # Every independent step succeeds and actually executed.
    for step in independents:
        assert statuses[step] == StepStatus.SUCCEEDED, step
        assert step in recorder, step

    # The failed step itself did run (it was attempted, then failed).
    assert failed_step in recorder

    # Independents that come before the failed step in the order are unaffected,
    # confirming "only dependents are skipped" (no over-skipping).
    assert not (dependents & independents)
