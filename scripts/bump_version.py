#!/usr/bin/env python3
"""
Version bump script for GCO.

The authoritative version lives in the top-level ``VERSION`` file so that
shell scripts, Dockerfiles, and CI workflows can read it without importing
Python. This script keeps three locations in sync:

- ``VERSION``                  (source of truth, plain text: ``MAJOR.MINOR.PATCH``)
- ``gco/_version.py``          (mirrors VERSION as ``__version__``)
- ``cli/__init__.py``          (fallback ``__version__`` when ``gco`` is not importable)

``gco._version.__version__`` reads its value at import time and should always
match ``VERSION`` on a clean checkout.

Other components that follow the same version automatically (no script
changes needed):

- ``gco_mcp/run_mcp.py``            imports ``gco._version.__version__`` for its
                                ``_MCP_SERVER_VERSION`` and reports it in the
                                startup audit log.
- ``pyproject.toml``            uses ``dynamic = ["version"]`` with
                                ``setuptools.dynamic.version`` set to
                                ``{attr = "gco._version.__version__"}``, so the
                                built wheel tracks the same value.

Usage:
    python scripts/bump_version.py major            # 0.0.9 -> 1.0.0
    python scripts/bump_version.py minor            # 0.0.9 -> 0.1.0
    python scripts/bump_version.py patch            # 0.0.9 -> 0.0.10
    python scripts/bump_version.py patch --dry-run  # Show what would change
    python scripts/bump_version.py                  # Show current version
"""

import re
import sys
from pathlib import Path

# File paths relative to project root
PROJECT_ROOT = Path(__file__).parent.parent
VERSION_FILE = PROJECT_ROOT / "VERSION"
VERSION_PY = PROJECT_ROOT / "gco" / "_version.py"
CLI_INIT_FILE = PROJECT_ROOT / "cli" / "__init__.py"


def get_version() -> str:
    """Read current version from the top-level VERSION file."""
    if not VERSION_FILE.exists():
        raise FileNotFoundError(
            f"VERSION file not found at {VERSION_FILE.relative_to(PROJECT_ROOT)}. "
            "Create it with a single line of the form MAJOR.MINOR.PATCH."
        )
    version = VERSION_FILE.read_text().strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(
            f"Invalid version in {VERSION_FILE.relative_to(PROJECT_ROOT)}: {version!r}. "
            "Expected MAJOR.MINOR.PATCH."
        )
    return version


def update_version_file(version: str, dry_run: bool = False) -> None:
    """Update the top-level VERSION file."""
    if dry_run:
        print(f"  [dry-run] Would update {VERSION_FILE.relative_to(PROJECT_ROOT)}")
        return
    VERSION_FILE.write_text(f"{version}\n")
    print(f"  ✓ Updated {VERSION_FILE.relative_to(PROJECT_ROOT)}")


def update_version_py(version: str, dry_run: bool = False) -> None:
    """Update ``__version__`` in gco/_version.py."""
    if dry_run:
        print(f"  [dry-run] Would update {VERSION_PY.relative_to(PROJECT_ROOT)}")
        return
    content = VERSION_PY.read_text()
    new_content = re.sub(
        r'__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{version}"',
        content,
    )
    VERSION_PY.write_text(new_content)
    print(f"  ✓ Updated {VERSION_PY.relative_to(PROJECT_ROOT)}")


def update_cli_init(version: str, dry_run: bool = False) -> None:
    """Update fallback ``__version__`` in cli/__init__.py."""
    if dry_run:
        print(f"  [dry-run] Would update {CLI_INIT_FILE.relative_to(PROJECT_ROOT)}")
        return
    content = CLI_INIT_FILE.read_text()
    new_content = re.sub(
        r'__version__\s*=\s*["\'][^"\']+["\']',
        f'__version__ = "{version}"',
        content,
        flags=re.MULTILINE,
    )
    CLI_INIT_FILE.write_text(new_content)
    print(f"  ✓ Updated {CLI_INIT_FILE.relative_to(PROJECT_ROOT)}")


def bump_version(bump_type: str) -> str:
    """Bump version based on type (major, minor, patch)."""
    current = get_version()
    major, minor, patch = (int(p) for p in current.split("."))

    if bump_type == "major":
        major += 1
        minor = 0
        patch = 0
    elif bump_type == "minor":
        minor += 1
        patch = 0
    elif bump_type == "patch":
        patch += 1
    else:
        raise ValueError(f"Invalid bump type: {bump_type}")

    return f"{major}.{minor}.{patch}"


def set_version(version: str, dry_run: bool = False) -> None:
    """Update version in all three locations."""
    action = "Would update" if dry_run else "Updating"
    print(f"\n{action} version to {version}:")
    update_version_file(version, dry_run)
    update_version_py(version, dry_run)
    update_cli_init(version, dry_run)


def main() -> None:
    args = [a.lower() for a in sys.argv[1:]]
    dry_run = "--dry-run" in args or "-n" in args
    args = [a for a in args if a not in ("--dry-run", "-n")]

    if len(args) < 1:
        print(f"Current version: {get_version()}")
        print("\nVersion locations:")
        print(f"  - {VERSION_FILE.relative_to(PROJECT_ROOT)}  (source of truth)")
        print(f"  - {VERSION_PY.relative_to(PROJECT_ROOT)}")
        print(f"  - {CLI_INIT_FILE.relative_to(PROJECT_ROOT)}")
        return

    bump_type = args[0]
    if bump_type not in ("major", "minor", "patch"):
        print(f"Usage: {sys.argv[0]} [major|minor|patch] [--dry-run]")
        sys.exit(1)

    old_version = get_version()
    new_version = bump_version(bump_type)
    set_version(new_version, dry_run)

    if dry_run:
        print(f"\n[dry-run] Would bump version: {old_version} -> {new_version}")
    else:
        print(f"\n✓ Bumped version: {old_version} -> {new_version}")
        print("\nTo complete the release:")
        print("  git add VERSION gco/_version.py cli/__init__.py")
        print(f"  git commit -m 'Release v{new_version}'")
        print(f"  git tag v{new_version}")
        print("  git push origin main --tags")


if __name__ == "__main__":
    main()
