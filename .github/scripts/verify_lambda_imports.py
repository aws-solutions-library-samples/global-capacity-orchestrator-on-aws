#!/usr/bin/env python3
"""Import every deployable Python Lambda handler in an isolated process."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENTRYPOINT_OVERRIDES = {
    "analytics-cleanup": "handler",
    "helm-orchestrator": "on_event",
}
DEFAULT_ENTRYPOINT = "lambda_handler"
IMPORT_CHECK = (
    "import importlib,sys;"
    "sys.path.insert(0,sys.argv[1]);"
    "module=importlib.import_module('handler');"
    "entrypoint=getattr(module,sys.argv[2]);"
    "raise SystemExit(0 if callable(entrypoint) else 1)"
)


@dataclass(frozen=True)
class HandlerTarget:
    """One handler module and the callable configured as its entrypoint."""

    directory: Path
    entrypoint: str


def discover_handlers(root: Path) -> list[HandlerTarget]:
    """Return every non-generated ``lambda/*/handler.py`` target."""
    lambda_root = root.resolve() / "lambda"
    targets = [
        HandlerTarget(
            directory=handler_path.parent,
            entrypoint=ENTRYPOINT_OVERRIDES.get(
                handler_path.parent.name,
                DEFAULT_ENTRYPOINT,
            ),
        )
        for handler_path in sorted(lambda_root.glob("*/handler.py"))
        if not handler_path.parent.name.endswith("-build")
    ]
    if not targets:
        raise RuntimeError(f"no Python Lambda handlers found under {lambda_root}")

    discovered_names = {target.directory.name for target in targets}
    stale_overrides = set(ENTRYPOINT_OVERRIDES) - discovered_names
    if stale_overrides:
        names = ", ".join(sorted(stale_overrides))
        raise RuntimeError(f"entrypoint overrides reference missing handlers: {names}")
    return targets


def verify_handler(target: HandlerTarget, *, timeout: int = 30) -> None:
    """Import one handler in a fresh interpreter and require a callable entrypoint."""
    environment = os.environ.copy()
    environment.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    environment.setdefault("AWS_REGION", "us-east-1")
    environment.setdefault("AWS_EC2_METADATA_DISABLED", "true")
    subprocess.run(
        [sys.executable, "-c", IMPORT_CHECK, str(target.directory), target.entrypoint],
        check=True,
        env=environment,
        timeout=timeout,
    )


def verify_handlers(targets: list[HandlerTarget]) -> None:
    """Verify every discovered target without sharing imported module state."""
    for target in targets:
        relative_directory = target.directory.name
        print(f"=== lambda/{relative_directory} ({target.entrypoint}) ===", flush=True)
        verify_handler(target)
    print(f"Imported {len(targets)} Python Lambda handlers.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root (defaults to the checkout containing this script)",
    )
    args = parser.parse_args(argv)
    verify_handlers(discover_handlers(args.root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
