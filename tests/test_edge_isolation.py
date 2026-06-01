"""Edge-isolation and extras-free core tests (task 11.6).

Covers Requirement 15.4 and 15.5 of the Real Provider Integration feature:

- **15.4** When the optional integration extras are not installed, the core test
  suite runs to completion without importing any third-party client library.
- **15.5** If a core module imported a third-party client library while the
  extras were absent, the affected test fails with an *import error* rather than
  the import being silently guarded at runtime.

The heavy lifting is performed by :mod:`tests._extras_free_probe`, executed in a
*fresh* interpreter (see :data:`_PROBE`). Running in a subprocess makes the
check deterministic no matter whether the optional ``youtube`` extra happens to
be installed in the developer's environment: the probe installs a
``sys.meta_path`` blocker that makes the declared third-party client libraries
behave as if they were not installed, then imports the entire ``src/`` import
graph and reports what imported, what failed, and whether any forbidden module
leaked into ``sys.modules``.

This module also runs a fast in-process check that the ``domain`` and
``analysis`` core layers import cleanly, documenting the everyday expectation
that the dependency-free core never reaches for an edge-only client library.

# Feature: real-provider-integration, Task 11.6: edge isolation / extras-free core
"""

from __future__ import annotations

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_DIR = _REPO_ROOT / "src"
_PROBE = Path(__file__).resolve().parent / "_extras_free_probe.py"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

# The ``test`` extra is the harness (pytest/hypothesis), not a runtime client
# library, so it is excluded from the dependency-policy scan below.
_HARNESS_EXTRAS = frozenset({"test"})

# Maps each declared third-party *distribution* (as it appears in
# ``[project.optional-dependencies]``) to the top-level *import* name(s) it
# installs. The probe blocks these import roots to simulate the extras being
# absent (15.4) and to prove a forbidden import would fail loudly (15.5). When a
# new client library is added to an optional extra, add its import root here so
# this test keeps the core honest.
_DISTRIBUTION_TO_IMPORT_ROOTS: dict[str, tuple[str, ...]] = {
    "google-auth": ("google",),
    "google-api-python-client": ("googleapiclient",),
}

# Core layers that must never pull in a third-party client library at import
# time. These are imported in-process as a fast first signal (15.4).
_CORE_MODULES = (
    "domain",
    "domain.models",
    "analysis.baseline",
    "analysis.category_filter",
    "analysis.channel_analyzer",
    "analysis.competitor_tracker",
    "analysis.format_recommender",
    "analysis.outlier_detector",
    "analysis.publish_time_predictor",
    "analysis.scoring",
    "analysis.seo_analyzer",
    "analysis.trend_discovery",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_distribution_name(requirement: str) -> str:
    """Return the bare distribution name from a requirement string.

    ``"google-auth==2.53.0"`` -> ``"google-auth"``. Strips the version pin and
    any environment marker / extras so only the distribution name remains.
    """
    head = requirement.split(";", 1)[0].strip()
    for separator in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", " "):
        index = head.find(separator)
        if index != -1:
            head = head[:index]
    return head.strip()


def _forbidden_import_roots() -> tuple[str, ...]:
    """Derive the third-party import roots to block from ``pyproject.toml``.

    Reads every optional extra except the test harness, maps each declared
    distribution to its import root via :data:`_DISTRIBUTION_TO_IMPORT_ROOTS`,
    and returns the de-duplicated set. An unmapped distribution fails the test
    with a clear instruction, keeping the blocker in sync with the declared
    extras (15.1/15.4).
    """
    metadata = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    optional = metadata.get("project", {}).get("optional-dependencies", {})

    roots: set[str] = set()
    for extra, requirements in optional.items():
        if extra in _HARNESS_EXTRAS:
            continue
        for requirement in requirements:
            distribution = _parse_distribution_name(requirement)
            assert distribution in _DISTRIBUTION_TO_IMPORT_ROOTS, (
                f"Optional extra '{extra}' declares the third-party "
                f"distribution '{distribution}', which has no known import "
                "root. Add it to _DISTRIBUTION_TO_IMPORT_ROOTS in "
                "tests/test_edge_isolation.py so the extras-free core check "
                "blocks it (Requirement 15.4)."
            )
            roots.update(_DISTRIBUTION_TO_IMPORT_ROOTS[distribution])

    return tuple(sorted(roots))


def _run_probe(forbidden: tuple[str, ...]) -> dict:
    """Execute the extras-free probe in a fresh interpreter and parse its JSON.

    The probe blocks ``forbidden`` import roots, then imports the whole ``src``
    graph; the returned mapping reports ``imported`` / ``failed`` modules,
    whether a representative forbidden module is genuinely blocked
    (``blocked_ok``), and any forbidden modules left in ``sys.modules``
    (``forbidden_present``).
    """
    completed = subprocess.run(
        [sys.executable, str(_PROBE), str(_SRC_DIR), ",".join(forbidden)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, (
        "Extras-free probe subprocess exited with "
        f"{completed.returncode}.\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return json.loads(completed.stdout)


# ---------------------------------------------------------------------------
# In-process core import check (15.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module_name", _CORE_MODULES)
def test_core_module_imports_cleanly(module_name: str) -> None:
    """Each ``domain``/``analysis`` core module imports without error (15.4).

    The core layers are dependency-free, so importing them must never require an
    optional integration extra to be installed.
    """
    module = importlib.import_module(module_name)
    assert module is not None


# ---------------------------------------------------------------------------
# Extras-free, full-graph subprocess checks (15.4, 15.5)
# ---------------------------------------------------------------------------


def test_forbidden_import_roots_are_declared() -> None:
    """The optional extras resolve to at least one blockable import root.

    Guards against the test silently passing because nothing was blocked (which
    would make the 15.4/15.5 assertions vacuous).
    """
    forbidden = _forbidden_import_roots()
    assert forbidden, (
        "Expected at least one optional third-party client library to block; "
        "found none in [project.optional-dependencies]."
    )


def test_core_imports_without_optional_extras() -> None:
    """Every ``src`` module imports with the optional extras blocked (15.4/15.5).

    With the declared third-party client libraries made unimportable, the entire
    import graph -- core consumers and edge providers alike -- must still import,
    because the only third-party imports live at the edges and are performed
    lazily at call time (e.g. ``AuthManager.refresh_access_token``). Any module
    that imported an extra at import time would appear in ``failed`` with an
    ``ImportError`` traceback, satisfying the 15.5 "fail loudly" rule.
    """
    forbidden = _forbidden_import_roots()
    result = _run_probe(forbidden)

    # The blocker must actually block, otherwise the check below is meaningless.
    assert result["blocked_ok"], (
        "The import blocker did not make a representative forbidden module "
        "unimportable; the extras-free check would be vacuous."
    )

    assert result["failed"] == {}, (
        "Core/edge modules failed to import with the optional extras blocked. "
        "A third-party client library is being imported at import time instead "
        "of lazily at the edge (Requirement 15.4/15.5):\n"
        + "\n".join(
            f"- {name}:\n{trace}" for name, trace in result["failed"].items()
        )
    )

    # Sanity check: the core layers were actually exercised by the probe.
    imported = set(result["imported"])
    for module_name in _CORE_MODULES:
        assert module_name in imported, (
            f"Expected core module '{module_name}' to be imported by the probe; "
            f"it was not among the imported modules: {sorted(imported)}"
        )


def test_core_suite_imports_no_third_party_client_library() -> None:
    """The extras-free import run pulls in no third-party client library (15.4).

    After importing the whole ``src`` graph with the extras blocked, no module
    belonging to a forbidden third-party client library may be present in
    ``sys.modules``. This is the positive form of 15.4: the core completes
    without importing any third-party client library.
    """
    forbidden = _forbidden_import_roots()
    result = _run_probe(forbidden)

    assert result["forbidden_present"] == [], (
        "A third-party client library was imported while the optional extras "
        "were blocked, violating edge isolation (Requirement 15.4): "
        f"{result['forbidden_present']}"
    )
