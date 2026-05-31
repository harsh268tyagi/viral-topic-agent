"""Hypothesis property test for invalid-schedule rejection (task 19.2).

Concrete schedule branches (valid stored, overlap, manual-only) live in
``tests/test_automation_scheduler.py``. This module hosts the universal
Property 25.

Property 25 (design.md -> Automation_Scheduler / Requirement 14.2): *for any*
submitted :class:`~viral_topic_agent.models.Schedule` that omits the recurrence
interval or the run time, ``set_schedule`` SHALL reject it, SHALL NOT store it
in the :class:`~viral_topic_agent.models.Configuration`, and SHALL return an
error naming every missing required field.

# Feature: viral-topic-agent, Property 25: Invalid schedules are rejected and name the missing field

Validates: Requirements 14.2
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from orchestration.automation_scheduler import (
    FIELD_RECURRENCE_INTERVAL,
    FIELD_RUN_TIME,
    AutomationScheduler,
)
from domain.models import Configuration, Schedule


# ---------------------------------------------------------------------------
# Generators
#
# A schedule field is "present" only when it is a non-blank string; "missing"
# covers ``None`` and whitespace-only strings (both are treated as omitted, per
# the 14.2 "omits" language). The scenario draws an independent state for each
# field and is filtered to those where at least one field is missing, so every
# generated schedule is genuinely invalid and Property 25 never runs vacuously.
# ---------------------------------------------------------------------------

_present = st.text(
    alphabet=st.characters(blacklist_categories=("Cs", "Cc")),
    min_size=1,
    max_size=12,
).filter(lambda s: s.strip() != "")

_blank = st.sampled_from(["", "   ", "\t", "\n"])

_field = st.one_of(st.none(), _blank, _present)


def _is_missing(value: str | None) -> bool:
    return value is None or not str(value).strip()


@st.composite
def _invalid_schedules(draw: st.DrawFn) -> Schedule:
    """An arbitrary Schedule with at least one missing required field."""
    interval = draw(_field)
    run_time = draw(_field)
    # At least one field must be missing for the schedule to be invalid.
    if not (_is_missing(interval) or _is_missing(run_time)):
        # Force a missing field by blanking one of them.
        if draw(st.booleans()):
            interval = draw(st.one_of(st.none(), _blank))
        else:
            run_time = draw(st.one_of(st.none(), _blank))
    return Schedule(recurrence_interval=interval, run_time=run_time)


def _config_with_existing_schedule(draw: st.DrawFn) -> Configuration:
    """A configuration that may already hold a (possibly None) schedule."""
    existing = draw(
        st.one_of(
            st.none(),
            st.just(Schedule(recurrence_interval="weekly", run_time="09:00")),
        )
    )
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=existing,
        delivery_destinations=(),
    )


@st.composite
def _scenario(draw: st.DrawFn) -> tuple[Configuration, Schedule]:
    return _config_with_existing_schedule(draw), draw(_invalid_schedules())


# ---------------------------------------------------------------------------
# Property 25
# ---------------------------------------------------------------------------


# Feature: viral-topic-agent, Property 25: Invalid schedules are rejected and name the missing field
@settings(max_examples=200)
@given(scenario=_scenario())
def test_invalid_schedule_is_rejected_and_names_missing_fields(
    scenario: tuple[Configuration, Schedule],
) -> None:
    """An invalid schedule is rejected, not stored, and names every missing
    required field (14.2)."""
    config, schedule = scenario
    result = AutomationScheduler().set_schedule(config, schedule)

    # Rejected and not stored.
    assert result.stored is False

    # The configuration is returned unchanged: its schedule is exactly what it
    # was before (the invalid schedule is never persisted).
    assert result.config is config
    assert result.config.schedule == config.schedule

    # The result names exactly the fields that were missing.
    expected_missing: list[str] = []
    if _is_missing(schedule.recurrence_interval):
        expected_missing.append(FIELD_RECURRENCE_INTERVAL)
    if _is_missing(schedule.run_time):
        expected_missing.append(FIELD_RUN_TIME)

    assert set(result.missing_fields) == set(expected_missing)
    assert result.missing_fields  # non-empty: the schedule is genuinely invalid

    # The error indication names every missing field.
    assert result.error is not None
    for field_name in expected_missing:
        assert field_name in result.error
