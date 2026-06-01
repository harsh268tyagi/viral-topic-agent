"""Dependency-policy and secret-hygiene smoke tests (task 11.7).

These are *static* smoke tests over the project metadata and source tree -- they
read files, they do not import the edges or touch the network -- that pin down
the dependency policy and secret-hygiene decisions recorded in the design:

- **Exactly-pinned optional client extras (Requirement 15.1).** Every
  third-party client library the integration requires is declared under
  ``[project.optional-dependencies]`` in ``pyproject.toml`` and pinned to an
  exact version (``name==X.Y.Z``), and the runtime core declares no third-party
  dependency of its own (so the extras really are *optional*).
- **No third-party client imports in the core (Requirement 15.2).** The
  ``domain/`` and ``analysis/`` layers import nothing outside the Python
  standard library and the project's own first-party packages.
- **Standard library where it suffices (Requirement 15.3).** Email goes through
  stdlib ``smtplib``/``email`` and HTTP through stdlib ``urllib``; no heavy
  third-party HTTP client (``requests``/``httpx``/``aiohttp``/``urllib3``) is
  imported anywhere under ``src/``.
- **Secret hygiene (Requirement 12.4).** The ``.env`` file and local credential
  files are excluded from version control via ``.gitignore``.

The checks are deliberately structural so they stay fast and require neither the
optional ``youtube`` extra nor any network access.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repository layout
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

# Optional-dependency groups that are developer tooling rather than runtime
# third-party *client* libraries; Requirement 15.1 governs the client extras.
DEV_EXTRAS = frozenset({"test"})

# Heavy third-party HTTP clients the design deliberately avoids in favor of the
# stdlib ``urllib`` transport (Requirement 15.3).
THIRD_PARTY_HTTP_CLIENTS = frozenset(
    {"requests", "httpx", "aiohttp", "urllib3", "httplib2"}
)

# Version-specifier operators permitted by PEP 508.
_SPECIFIER_OP_RE = re.compile(r"(===|==|~=|!=|<=|>=|<|>)")
_REQUIREMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


# ---------------------------------------------------------------------------
# Helpers: project metadata
# ---------------------------------------------------------------------------


def _load_pyproject() -> dict:
    with PYPROJECT_PATH.open("rb") as handle:
        return tomllib.load(handle)


def _optional_dependencies() -> dict[str, list[str]]:
    project = _load_pyproject().get("project", {})
    return project.get("optional-dependencies", {})


def _split_requirement(requirement: str) -> tuple[str, str]:
    """Return ``(distribution_name, specifier_part)`` for a PEP 508 requirement.

    Environment markers (after ``;``) and extras (``name[extra]``) are stripped
    so only the distribution name and its version specifier remain.
    """
    base = requirement.split(";", 1)[0].strip()
    name_match = _REQUIREMENT_NAME_RE.match(base)
    assert name_match is not None, f"unparseable requirement: {requirement!r}"
    name = name_match.group(0)
    rest = base[len(name) :].strip()
    if rest.startswith("["):  # drop an extras group like "[security]"
        rest = rest[rest.index("]") + 1 :].strip()
    return name, rest


def _is_exactly_pinned(requirement: str) -> bool:
    """True iff the requirement's only version operator is a single ``==``."""
    _name, specifier = _split_requirement(requirement)
    return _SPECIFIER_OP_RE.findall(specifier) == ["=="]


def _client_extras() -> dict[str, list[str]]:
    """The optional-dependency groups that carry third-party client libraries."""
    return {
        name: reqs
        for name, reqs in _optional_dependencies().items()
        if name not in DEV_EXTRAS
    }


# ---------------------------------------------------------------------------
# Helpers: source-tree import scanning
# ---------------------------------------------------------------------------


def _first_party_packages() -> frozenset[str]:
    """Top-level package names under ``src/`` (directories with __init__.py)."""
    return frozenset(
        child.name
        for child in SRC_DIR.iterdir()
        if child.is_dir() and (child / "__init__.py").exists()
    )


def _python_files(layer: str) -> list[Path]:
    return sorted((SRC_DIR / layer).rglob("*.py"))


def _top_level_imports(source: str) -> set[str]:
    """Top-level module names imported by ``source`` (absolute imports only).

    Relative imports (``from . import x``) are first-party by construction and
    are skipped.
    """
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative import -> first-party
                continue
            if node.module:
                names.add(node.module.split(".", 1)[0])
    return names


def _third_party_imports_in_layer(layer: str) -> dict[str, set[str]]:
    """Map each file in ``layer`` to the third-party top-level names it imports.

    A name is third-party when it is neither in the standard library nor a
    first-party package under ``src/``. Files with no third-party imports are
    omitted from the result.
    """
    stdlib = sys.stdlib_module_names
    first_party = _first_party_packages()
    violations: dict[str, set[str]] = {}
    for path in _python_files(layer):
        imported = _top_level_imports(path.read_text(encoding="utf-8"))
        third_party = {
            name
            for name in imported
            if name not in stdlib and name not in first_party
        }
        if third_party:
            violations[str(path.relative_to(REPO_ROOT))] = third_party
    return violations


def _all_source_imports() -> dict[str, set[str]]:
    """Map every module under ``src/`` to its absolute top-level imports."""
    result: dict[str, set[str]] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" in path.parts or ".egg-info" in str(path):
            continue
        rel = str(path.relative_to(REPO_ROOT))
        result[rel] = _top_level_imports(path.read_text(encoding="utf-8"))
    return result


# ---------------------------------------------------------------------------
# Helpers: .gitignore
# ---------------------------------------------------------------------------


def _gitignore_patterns() -> list[str]:
    lines = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    patterns = []
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        patterns.append(stripped)
    return patterns


# ===========================================================================
# Requirement 15.1 -- exactly-pinned optional client extras + dependency-free core
# ===========================================================================


def test_core_declares_no_runtime_third_party_dependencies() -> None:
    """The runtime core lists no third-party dependency; clients are optional (15.1)."""
    project = _load_pyproject().get("project", {})
    assert project.get("dependencies", []) == [], (
        "the runtime core must stay dependency-free; third-party client "
        "libraries belong in [project.optional-dependencies], not in "
        "[project.dependencies]"
    )


def test_youtube_oauth_client_extra_declared_and_exactly_pinned() -> None:
    """The YouTube OAuth client libraries are declared as an exactly-pinned extra (15.1)."""
    extras = _optional_dependencies()
    assert "youtube" in extras, (
        "expected a 'youtube' optional extra declaring the OAuth client libraries"
    )

    youtube_requirements = extras["youtube"]
    pinned_names = {_split_requirement(req)[0] for req in youtube_requirements}
    assert {"google-auth", "google-api-python-client"} <= pinned_names, (
        "the 'youtube' extra must declare the google-auth and "
        f"google-api-python-client client libraries; found {sorted(pinned_names)}"
    )

    not_pinned = [req for req in youtube_requirements if not _is_exactly_pinned(req)]
    assert not not_pinned, (
        f"every 'youtube' client library must be exactly pinned (==): {not_pinned}"
    )


def test_all_third_party_client_extras_are_exactly_pinned() -> None:
    """Every client-library extra is pinned to an exact version (15.1)."""
    client_extras = _client_extras()
    assert client_extras, (
        "expected at least one third-party client extra under "
        "[project.optional-dependencies]"
    )

    unpinned: dict[str, list[str]] = {}
    for extra_name, requirements in client_extras.items():
        offending = [req for req in requirements if not _is_exactly_pinned(req)]
        if offending:
            unpinned[extra_name] = offending

    assert not unpinned, (
        "every third-party client library must be declared as an exactly-pinned "
        f"optional extra (name==version); unpinned entries: {unpinned}"
    )


# ===========================================================================
# Requirement 15.2 -- no third-party client imports in the core layers
# ===========================================================================


@pytest.mark.parametrize("layer", ["domain", "analysis"])
def test_core_layer_imports_no_third_party_client(layer: str) -> None:
    """``domain/`` and ``analysis/`` import only stdlib + first-party modules (15.2)."""
    violations = _third_party_imports_in_layer(layer)
    assert not violations, (
        f"the {layer}/ layer must not import any third-party client library; "
        f"found: {violations}"
    )


# ===========================================================================
# Requirement 15.3 -- standard library where it suffices
# ===========================================================================


def test_email_transport_uses_stdlib_smtp() -> None:
    """Email transmission goes through stdlib ``smtplib``/``email`` (15.3)."""
    smtp_module = SRC_DIR / "delivery" / "smtp_transport.py"
    imported = _top_level_imports(smtp_module.read_text(encoding="utf-8"))
    assert "smtplib" in imported, (
        "delivery/smtp_transport.py must use stdlib smtplib for SMTP transmission"
    )
    assert "smtplib" in sys.stdlib_module_names


def test_http_transport_uses_stdlib_urllib() -> None:
    """The HTTP transport goes through stdlib ``urllib`` (15.3)."""
    http_module = SRC_DIR / "infrastructure" / "http_transport.py"
    imported = _top_level_imports(http_module.read_text(encoding="utf-8"))
    assert "urllib" in imported, (
        "infrastructure/http_transport.py must use stdlib urllib for HTTP"
    )
    assert "urllib" in sys.stdlib_module_names


def test_no_third_party_http_client_anywhere_in_source() -> None:
    """No heavy third-party HTTP client is imported under ``src/`` (15.3)."""
    offenders: dict[str, set[str]] = {}
    for rel_path, imported in _all_source_imports().items():
        clients = imported & THIRD_PARTY_HTTP_CLIENTS
        if clients:
            offenders[rel_path] = clients
    assert not offenders, (
        "the standard-library urllib transport must be used instead of a "
        f"third-party HTTP client; found: {offenders}"
    )


# ===========================================================================
# Requirement 12.4 -- secret hygiene: secrets excluded from version control
# ===========================================================================


def test_env_file_excluded_from_version_control() -> None:
    """The local ``.env`` file is gitignored (12.4)."""
    patterns = _gitignore_patterns()
    assert ".env" in patterns, (
        ".env must be excluded from version control via .gitignore so local "
        "secrets are never committed"
    )


def test_local_credential_files_excluded_from_version_control() -> None:
    """Local credential files are gitignored (12.4)."""
    patterns = set(_gitignore_patterns())

    # A private-key pattern (PEM/key material) must be ignored.
    assert {"*.pem", "*.key"} <= patterns, (
        "private key material (*.pem, *.key) must be excluded from version control"
    )

    # OAuth / service-account credential files must be ignored.
    expected_credential_patterns = {
        "credentials.json",
        "token.json",
    }
    missing = expected_credential_patterns - patterns
    assert not missing, (
        "local credential files must be excluded from version control; "
        f"missing .gitignore patterns: {sorted(missing)}"
    )
