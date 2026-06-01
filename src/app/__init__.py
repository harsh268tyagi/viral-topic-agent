"""Application entry-point layer for the Real Provider Integration.

This package holds the :class:`~app.composition_root.CompositionRoot` -- the
single factory that reads validated :class:`~config.settings.Settings`, wires the
real edge components into the existing seams, builds a domain
:class:`~domain.models.Configuration`, and runs the
:class:`~orchestration.automation_scheduler.AutomationScheduler` (Requirement
14). It is a thin, stdlib-only composition layer: it introduces no third-party
imports of its own, keeping the dependency-free core intact (Requirement 15).
"""
