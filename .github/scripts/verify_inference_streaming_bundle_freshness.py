#!/usr/bin/env python3
"""CI-only regression for the real ignored inference streaming bundle.

The check deliberately damages ``lambda/inference-streaming-proxy-build`` and
then enters the production ``StackManager.synth`` and ``StackManager.diff``
paths. Only ``_run_cdk`` is mocked, so no CDK process runs during this check;
the real pinned npm builder must repair the real ignored asset both times.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_PROJECT_ROOT))

from cli.stacks import StackManager  # noqa: E402

_PACKAGE_FILES = ("index.mjs", "package.json", "package-lock.json")


def _dependency_markers(source_dir: Path, build_dir: Path) -> tuple[Path, ...]:
    package_data = json.loads((source_dir / "package.json").read_text(encoding="utf-8"))
    dependencies = package_data.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        raise AssertionError("Inference streaming package has no production dependencies")
    return tuple(
        build_dir / "node_modules" / name / "package.json" for name in sorted(dependencies)
    )


def _transitive_dependency_file(source_dir: Path, build_dir: Path) -> Path:
    package_data = json.loads((source_dir / "package.json").read_text(encoding="utf-8"))
    direct_dependencies = set(package_data.get("dependencies", {}))
    for marker in sorted((build_dir / "node_modules").rglob("package.json")):
        relative = marker.relative_to(build_dir / "node_modules")
        if len(relative.parts) < 2:
            continue
        package_name = (
            "/".join(relative.parts[:2]) if relative.parts[0].startswith("@") else relative.parts[0]
        )
        if package_name not in direct_dependencies:
            return marker
    raise AssertionError("Inference streaming bundle has no transitive dependency marker")


def _assert_fresh(manager: StackManager, source_dir: Path, build_dir: Path) -> None:
    if not manager._inference_streaming_build_is_fresh(source_dir, build_dir):
        raise AssertionError("Inference streaming bundle is not source-current")
    for name in _PACKAGE_FILES:
        if (build_dir / name).read_bytes() != (source_dir / name).read_bytes():
            raise AssertionError(f"Inference streaming bundle did not restore {name}")
    for marker in _dependency_markers(source_dir, build_dir):
        if not marker.is_file():
            raise AssertionError(
                f"Inference streaming bundle did not restore dependency marker {marker}"
            )


def main() -> None:
    project_root = _PROJECT_ROOT
    source_dir = project_root / "lambda" / "inference-streaming-proxy"
    build_dir = project_root / "lambda" / "inference-streaming-proxy-build"
    manager = object.__new__(StackManager)
    manager.project_root = project_root

    _assert_fresh(manager, source_dir, build_dir)

    stale_handler = (source_dir / "index.mjs").read_bytes() + b"\n// deliberate CI staleness\n"
    (build_dir / "index.mjs").write_bytes(stale_handler)
    if manager._inference_streaming_build_is_fresh(source_dir, build_dir):
        raise AssertionError("Changed handler bytes were incorrectly accepted as fresh")

    synth_result = subprocess.CompletedProcess(
        args=["cdk", "synth"],
        returncode=0,
        stdout="mocked synth",
        stderr="",
    )
    with patch.object(manager, "_run_cdk", return_value=synth_result) as run_cdk:
        if manager.synth("gco-api-gateway") != "mocked synth":
            raise AssertionError("Unexpected mocked synth result")
        run_cdk.assert_called_once_with(
            ["synth", "gco-api-gateway", "--quiet"], capture_output=True
        )
    _assert_fresh(manager, source_dir, build_dir)

    transitive_marker = _transitive_dependency_file(source_dir, build_dir)
    transitive_relative = transitive_marker.relative_to(build_dir)
    transitive_marker.unlink()
    if manager._inference_streaming_build_is_fresh(source_dir, build_dir):
        raise AssertionError(
            f"Missing transitive dependency file was accepted as fresh: {transitive_relative}"
        )

    diff_result = subprocess.CompletedProcess(
        args=["cdk", "diff"],
        returncode=1,
        stdout="",
        stderr="mocked diff",
    )
    with patch.object(manager, "_run_cdk", return_value=diff_result) as run_cdk:
        if manager.diff("gco-api-gateway") != "mocked diff":
            raise AssertionError("Unexpected mocked diff result")
        run_cdk.assert_called_once_with(
            ["diff", "--no-color", "gco-api-gateway"], capture_output=True
        )
    _assert_fresh(manager, source_dir, build_dir)
    if not (build_dir / transitive_relative).is_file():
        raise AssertionError(f"Inference streaming bundle did not restore {transitive_relative}")

    print("Real inference streaming bundle freshness verified for synth and diff")


if __name__ == "__main__":
    main()
