"""Guard: every OS-base Dockerfile applies security patches at build time.

The helm-installer Lambda image shipped HIGH CVEs (util-linux, sqlite-libs)
because it was the one image that never ran a build-time OS package upgrade —
the Debian service images all run ``apt-get upgrade`` behind an
``APT_SECURITY_EPOCH`` cache-buster, but that convention was informal and
unenforced, so a new image could silently skip it.

This asserts that every tracked Dockerfile whose final stage uses an OS base
(one with a package manager) runs an upgrade step: ``apt-get upgrade`` /
``dnf upgrade`` / ``dnf update`` / ``apk upgrade`` / ``yum|zypper update``.
Bases with no package manager (``scratch``, distroless) are skipped
automatically. A Dockerfile that legitimately can't patch may opt out with a
``# os-patch-lint: skip - <reason>`` comment.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Directory names never worth scanning: virtualenvs, build scratch dirs, caches,
# vendored trees, and the agent workspace.
_EXCLUDE_PARTS = {
    ".git",
    ".venv",
    ".venv-diagrams",
    ".venv-verify",
    "cdk.out",
    "node_modules",
    ".kiro",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "htmlcov",
}

# Final-stage bases with no package manager — nothing to patch, so exempt.
_NO_PKG_MGR = re.compile(r"\b(scratch|distroless)\b", re.IGNORECASE)

# Build-time OS security-upgrade invocations across the common package managers.
_UPGRADE = re.compile(
    r"\b(?:apt-get|apt)\s+upgrade\b"
    r"|\b(?:dnf|microdnf|yum|zypper)\s+(?:upgrade|update|up)\b"
    r"|\bapk\s+upgrade\b",
    re.IGNORECASE,
)

_OPT_OUT = re.compile(r"#\s*os-patch-lint:\s*skip", re.IGNORECASE)

# Explicit, reasoned exemptions keyed by repo-relative path. Empty today — every
# shipped OS-base image patches. Add an entry (with a comment) only for an image
# that genuinely cannot, and prefer the in-file ``# os-patch-lint: skip`` marker.
_ALLOWLIST: dict[str, str] = {}


def _is_dockerfile(path: Path) -> bool:
    name = path.name
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith("-dockerfile")


def _iter_dockerfiles() -> list[Path]:
    files: list[Path] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or not _is_dockerfile(path):
            continue
        parts = set(path.relative_to(PROJECT_ROOT).parts)
        if parts & _EXCLUDE_PARTS:
            continue
        if any(part.endswith("-build") for part in parts):
            continue
        files.append(path)
    return sorted(files)


def _final_from_line(text: str) -> str | None:
    """Return the last ``FROM`` line (the final build stage), or None."""
    from_lines = [
        line.strip() for line in text.splitlines() if line.strip().upper().startswith("FROM ")
    ]
    return from_lines[-1] if from_lines else None


def test_os_base_dockerfiles_apply_security_patches() -> None:
    dockerfiles = _iter_dockerfiles()
    # Sanity floor: the enumeration must find the known images so an empty walk
    # can't pass vacuously.
    assert len(dockerfiles) >= 5, (
        f"expected to discover the project's Dockerfiles, found {len(dockerfiles)}: "
        f"{[str(p.relative_to(PROJECT_ROOT)) for p in dockerfiles]}"
    )

    offenders: list[str] = []
    for path in dockerfiles:
        rel = str(path.relative_to(PROJECT_ROOT))
        if rel in _ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if _OPT_OUT.search(text):
            continue
        from_line = _final_from_line(text)
        if from_line is None:
            continue  # not a real Dockerfile (no FROM) — ignore
        if _NO_PKG_MGR.search(from_line):
            continue  # scratch / distroless — no package manager to patch
        if not _UPGRADE.search(text):
            offenders.append(rel)

    assert not offenders, (
        "These OS-base Dockerfiles do not run a build-time package upgrade "
        "(apt-get/dnf/apk upgrade). Add one behind a cache-busting epoch ARG, "
        "or opt out with a '# os-patch-lint: skip - <reason>' comment:\n  " + "\n  ".join(offenders)
    )
