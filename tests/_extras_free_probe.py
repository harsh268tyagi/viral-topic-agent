"""Subprocess probe for the extras-free core import tests (task 11.6).

Run by :mod:`tests.test_edge_isolation` in a *fresh* interpreter so the check is
deterministic regardless of whether the optional ``youtube`` integration extra
happens to be installed in the developer's environment (in CI it is absent; on a
dev box ``google-auth`` may be present). The probe makes the optional extras
behave as if they were *not installed* and then exercises the core import graph.

Steps:

1. Install an import blocker -- a ``sys.meta_path`` finder -- that raises
   ``ModuleNotFoundError`` for every third-party client library named on the
   command line, exactly as if the optional extras were not installed
   (Requirement 15.4). A core module that imported such a library *at import
   time* would therefore fail with an import error rather than be silently
   guarded (Requirement 15.5).
2. Import every module under ``src/`` (the whole core/edge import graph). The
   edge modules confine their third-party imports to lazy, call-time imports
   (e.g. ``AuthManager.refresh_access_token``), so importing the modules must
   still succeed with the extras blocked.
3. Report, as JSON on stdout, which modules imported, which failed (with
   tracebacks), whether the blocker actually blocks a representative forbidden
   module, and whether any forbidden third-party client module ended up in
   ``sys.modules`` after the imports (Requirement 15.5).

Usage::

    python _extras_free_probe.py <src_dir> <forbidden_top_level_csv>

This file is intentionally not named ``test_*`` so pytest does not collect it;
it is executed only as a subprocess.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import traceback


class _BlockedImportFinder:
    """A ``sys.meta_path`` finder that simulates uninstalled optional extras.

    For any module whose top-level name is in ``blocked`` (or a submodule
    thereof), :meth:`find_spec` raises ``ModuleNotFoundError`` so the import
    fails exactly as it would if the package were not installed. For every other
    module it returns ``None`` and defers to the remaining finders.
    """

    def __init__(self, blocked):
        self._blocked = tuple(blocked)

    def _is_blocked(self, name):
        for top in self._blocked:
            if name == top or name.startswith(top + "."):
                return True
        return False

    def find_spec(self, fullname, path=None, target=None):
        if self._is_blocked(fullname):
            raise ModuleNotFoundError(
                f"import of {fullname!r} is blocked: simulating the optional "
                "integration extras not being installed "
                "(Requirement 15.4/15.5)",
                name=fullname,
            )
        return None


def _discover_modules(src_dir):
    """Return the dotted names of every importable module under ``src_dir``.

    Walks the source tree, skipping ``__pycache__`` and any ``*.egg-info``
    directories. Each package ``__init__.py`` contributes its package name and
    every other ``*.py`` file contributes its module name, so the full import
    graph (core consumers and edge providers alike) is exercised.
    """
    names = []
    for root, dirs, files in os.walk(src_dir):
        dirs[:] = [
            d
            for d in dirs
            if d != "__pycache__" and not d.endswith(".egg-info")
        ]
        rel = os.path.relpath(root, src_dir)
        package = "" if rel == "." else rel.replace(os.sep, ".")
        for filename in sorted(files):
            if not filename.endswith(".py"):
                continue
            if filename == "__init__.py":
                if package:
                    names.append(package)
            else:
                module = filename[:-3]
                names.append(f"{package}.{module}" if package else module)
    return sorted(set(names))


def _forbidden_in_sys_modules(forbidden):
    """Return any imported modules belonging to a forbidden third-party client."""
    return sorted(
        name
        for name in list(sys.modules)
        if any(name == top or name.startswith(top + ".") for top in forbidden)
    )


def main(argv):
    src_dir = argv[1]
    forbidden = tuple(part for part in argv[2].split(",") if part)

    # Block the optional extras before anything project-related is imported.
    sys.meta_path.insert(0, _BlockedImportFinder(forbidden))
    sys.path.insert(0, src_dir)

    # Self-check: a representative forbidden module must now be unimportable.
    # This documents the 15.5 rule -- a forbidden import fails with an import
    # error rather than being guarded -- and proves the blocker is effective
    # even when the package is installed in this interpreter's environment.
    blocked_ok = True
    if forbidden:
        try:
            importlib.import_module(forbidden[0])
            blocked_ok = False
        except ModuleNotFoundError:
            blocked_ok = True
        except Exception:  # pragma: no cover - any other error is also a failure
            blocked_ok = False
        sys.modules.pop(forbidden[0], None)

    imported = []
    failed = {}
    for name in _discover_modules(src_dir):
        try:
            importlib.import_module(name)
            imported.append(name)
        except Exception:  # noqa: BLE001 - report every import failure verbatim
            failed[name] = traceback.format_exc()

    json.dump(
        {
            "imported": imported,
            "failed": failed,
            "forbidden_present": _forbidden_in_sys_modules(forbidden),
            "blocked_ok": blocked_ok,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
