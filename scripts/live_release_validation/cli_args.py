"""Argument helpers shared by the validation harness CLIs.

Used by ``scripts.live_release_validation.__main__`` and
``scripts.example_job_validation.__main__``. Lives outside ``__main__`` so
the sibling harness never imports another package's entrypoint module.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def repository_root(value: str | None) -> Path:
    """Resolve and sanity-check the GCO checkout root."""
    if value:
        root = Path(value).expanduser().resolve()
    else:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("Run from a Git checkout or pass --repo-root")
        root = Path(result.stdout.strip()).resolve()
    if not (root / ".git").exists() or not (root / "cdk.json").is_file():
        raise ValueError(f"Not a GCO repository root: {root}")
    return root


def split_csv_names(value: str) -> tuple[str, ...]:
    """Parse a comma-separated name list, deduplicated, order-preserving."""
    names = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not names:
        raise argparse.ArgumentTypeError("expected at least one name")
    return names


def path_from_root(root: Path, value: str | None, default: Path) -> Path:
    """Resolve an optional path argument against the repository root."""
    path = Path(value).expanduser() if value else default
    candidate = path if path.is_absolute() else root / path
    return Path(os.path.abspath(os.fspath(candidate)))
