"""Example / branch tests for the Automation_Scheduler (task 19.6).

The universal properties live in the ``test_automation_scheduler_*_properties``
modules. This module covers the concrete scheduler branches:

- a valid schedule (both interval and run time) is stored in the configuration
  (14.1);
- a trigger arriving while a run is in progress is not started concurrently and
  is recorded as skipped (14.4);
- with no schedule configured, the workflow runs only on a manual trigger
  (14.7).

It also covers the happy-path wiring of all seven steps and the digest delivery
hand-off, so the end-to-end ``run`` pipeline is exercised against stubs.

Requirements exercised: 14.1, 14.4, 14.7.
"""

from __future__ import annotations

from orchestration.automation_scheduler import (
    DIGEST_DELIVERY,
    FIELD_RECURRENCE_INTERVAL,
    FIELD_RUN_TIME,
    STEP_ORDER,
    AutomationScheduler,
)
from infrastructure.clock import FakeClock
from domain.models import (
    Configuration,
    Schedule,
    StepStatus,
)

from .scheduler_support import (
    StubDigestService,
    TickingClock,
    make_config,
    make_scheduler,
)


def _empty_config(schedule: Schedule | None = None) -> Configuration:
    return Configuration(
        authorized_channels=(),
        selected_category=None,
        monitored_competitors=(),
        schedule=schedule,
        delivery_destinations=(),
    )


# ---------------------------------------------------------------------------
# 14.1: valid schedule is stored
# ---------------------------------------------------------------------------


def test_valid_schedule_is_stored_in_configuration():
    """A schedule with both interval and run time is stored (14.1)."""
    config = _empty_config(schedule=None)
    schedule = Schedule(recurrence_interval="daily", run_time="08:00")

    result = AutomationScheduler().set_schedule(config, schedule)

    assert result.stored is True
    assert result.missing_fields == ()
    assert result.error is None
    # The returned configuration carries the schedule...
    assert result.config.schedule == schedule
    # ...without mutating the input configuration.
    assert config.schedule is None


def test_valid_schedule_replaces_an_existing_schedule():
    """Storing a new valid schedule overwrites a previously stored one (14.1)."""
    old = Schedule(recurrence_interval="weekly", run_time="09:00")
    config = _empty_config(schedule=old)
    new = Schedule(recurrence_interval="daily", run_time="06:30")

    result = AutomationScheduler().set_schedule(config, new)

    assert result.stored is True
    assert result.config.schedule == new


def test_schedule_missing_interval_is_rejected_naming_the_field():
    """Omitting the recurrence interval is rejected and names the field (14.2)."""
    config = _empty_config()
    schedule = Schedule(recurrence_interval=None, run_time="08:00")

    result = AutomationScheduler().set_schedule(config, schedule)

    assert result.stored is False
    assert result.missing_fields == (FIELD_RECURRENCE_INTERVAL,)
    assert result.config.schedule is None
    assert FIELD_RECURRENCE_INTERVAL in result.error


def test_schedule_missing_run_time_is_rejected_naming_the_field():
    """Omitting the run time is rejected and names the field (14.2)."""
    config = _empty_config()
    schedule = Schedule(recurrence_interval="daily", run_time=None)

    result = AutomationScheduler().set_schedule(config, schedule)

    assert result.stored is False
    assert result.missing_fields == (FIELD_RUN_TIME,)
    assert result.config.schedule is None
    assert FIELD_RUN_TIME in result.error


def test_schedule_missing_both_fields_names_both():
    """Omitting both fields names both in the error (14.2)."""
    config = _empty_config()
    schedule = Schedule(recurrence_interval=None, run_time=None)

    result = AutomationScheduler().set_schedule(config, schedule)

    assert result.stored is False
    assert set(result.missing_fields) == {FIELD_RECURRENCE_INTERVAL, FIELD_RUN_TIME}


# ---------------------------------------------------------------------------
# 14.4: overlapping trigger is recorded as skipped
# ---------------------------------------------------------------------------


class _ReentrantTracker:
    """A competitor-tracker stub that re-enters the scheduler mid-run.

    When ``monitor`` runs (step 5 of 7), it triggers a second ``run`` on the
    same scheduler with the same in-progress guard still set, simulating an
    overlapping trigger. The overlapping run's summary is captured for assertion.
    """

    def __init__(self, recorder: list[str]) -> None:
        self._recorder = recorder
        self.scheduler: AutomationScheduler | None = None
        self.config: Configuration | None = None
        self.overlap_summary = None

    def monitor(self, config, source):
        self._recorder.append("competitor_tracking")
        # Re-enter while the first run is still in progress (14.4).
        assert self.scheduler is not None and self.config is not None
        self.overlap_summary = self.scheduler.run(
            self.config, TickingClock(), manual=True
        )
        return []


def test_overlapping_trigger_is_recorded_skipped_and_not_run_concurrently():
    """A trigger during an in-progress run is recorded skipped, not run (14.4)."""
    recorder: list[str] = []
    tracker = _ReentrantTracker(recorder)
    scheduler = make_scheduler(recorder)
    # Swap in the re-entrant competitor tracker.
    scheduler._competitor_tracker = tracker  # type: ignore[attr-defined]

    config = make_config()
    tracker.scheduler = scheduler
    tracker.config = config

    outer = scheduler.run(config, TickingClock(), manual=True)

    # The overlapping (inner) run did not start concurrently: every step skipped.
    inner = tracker.overlap_summary
    assert inner is not None
    assert inner.overlap_skipped is True
    assert [s.step for s in inner.steps] == list(STEP_ORDER)
    assert all(s.status == StepStatus.SKIPPED for s in inner.steps)

    # The outer run still completed all its steps normally.
    assert outer.overlap_skipped is False
    assert all(s.status == StepStatus.SUCCEEDED for s in outer.steps)

    # The overlapping trigger did NOT re-run the pipeline: competitor_tracking
    # was recorded once for the outer run (the re-entrant call short-circuited
    # before any step executed).
    assert recorder.count("competitor_tracking") == 1


def test_guard_is_released_so_a_later_run_executes_normally():
    """After a run finishes, the in-progress guard is cleared (14.4)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)
    config = make_config()

    first = scheduler.run(config, TickingClock(), manual=True)
    second = scheduler.run(config, TickingClock(), manual=True)

    assert first.overlap_skipped is False
    assert second.overlap_skipped is False
    assert all(s.status == StepStatus.SUCCEEDED for s in second.steps)


# ---------------------------------------------------------------------------
# 14.7: no schedule -> manual-only
# ---------------------------------------------------------------------------


def test_no_schedule_scheduled_trigger_does_not_run():
    """With no schedule, a non-manual (scheduled) trigger does not execute (14.7)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)
    config = make_config(with_schedule=False)

    summary = scheduler.run(config, TickingClock(), manual=False)

    # Nothing executed.
    assert recorder == []
    assert summary.steps == ()
    assert summary.overlap_skipped is False


def test_no_schedule_manual_trigger_runs_full_pipeline():
    """With no schedule, a manual trigger runs the full pipeline (14.7)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)
    config = make_config(with_schedule=False)

    summary = scheduler.run(config, TickingClock(), manual=True)

    assert recorder == list(STEP_ORDER)
    assert all(s.status == StepStatus.SUCCEEDED for s in summary.steps)


def test_with_schedule_scheduled_trigger_runs():
    """With a schedule configured, a scheduled (non-manual) trigger runs (14.7)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)
    config = make_config(with_schedule=True)

    summary = scheduler.run(config, TickingClock(), manual=False)

    assert recorder == list(STEP_ORDER)
    assert all(s.status == StepStatus.SUCCEEDED for s in summary.steps)


# ---------------------------------------------------------------------------
# Happy-path wiring: all seven steps + delivery hand-off
# ---------------------------------------------------------------------------


def test_run_invokes_digest_delivery_handoff():
    """The digest-delivery step compiles and delivers the report (14.3)."""
    recorder: list[str] = []
    digest = StubDigestService(recorder)
    # Build a fully-stubbed scheduler but keep a handle to the digest stub.
    scheduler = make_scheduler(recorder)
    scheduler._digest_service = digest  # type: ignore[attr-defined]

    scheduler.run(make_config(), TickingClock(), manual=True)

    assert DIGEST_DELIVERY in recorder
    assert digest.delivered is True


def test_run_uses_fake_clock_for_timestamps():
    """Run start/completion timestamps come from the injected clock (14.6)."""
    recorder: list[str] = []
    scheduler = make_scheduler(recorder)
    clock = FakeClock(start=100.0)

    summary = scheduler.run(make_config(), clock, manual=True)

    # FakeClock does not advance on read, so start == completion here; the
    # timestamps still derive from the injected clock's reading.
    assert float(summary.started_at) == 100.0
    assert float(summary.completed_at) == 100.0
