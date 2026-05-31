"""Orchestration layer.

The ``AutomationScheduler`` entry point: owns the recurring schedule, prevents
overlapping runs, executes the seven pipeline steps in the mandated order,
short-circuits steps whose input depends on a failed step, and records a run
summary.
"""
