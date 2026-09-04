"""Guard against stale path references in CI configuration.

A package rename (for example mcp to gco_mcp) can leave a CI config pointing
at a directory that no longer exists. The CodeQL autobuilder then crashes with
FileNotFoundError, and a stale --cov target silently records no coverage data.
These tests pin every such reference to a directory that actually exists.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CODEQL_CONFIG = PROJECT_ROOT / ".github" / "codeql" / "codeql-config.yml"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
WORKFLOWS_DIR = PROJECT_ROOT / ".github" / "workflows"
CI_FILES = sorted(WORKFLOWS_DIR.glob("*.yml"))

_COV_RE = re.compile(r"--cov=([A-Za-z0-9_./]+)")


def _top_level_dir(target: str) -> str:
    """First path component of a coverage target (gco/services to gco)."""
    return target.replace(".", "/").split("/", 1)[0]


def test_codeql_scan_paths_exist() -> None:
    """Every CodeQL paths entry must be a real directory."""
    config = yaml.safe_load(CODEQL_CONFIG.read_text())
    paths = config.get("paths", [])
    assert paths, "CodeQL config has no paths to scan"
    missing = [p for p in paths if not (PROJECT_ROOT / p).is_dir()]
    assert not missing, "CodeQL paths reference directories that do not exist: " + str(missing)


def test_coverage_cov_flags_point_at_real_dirs() -> None:
    """Every --cov flag in CI maps to a real top-level directory."""
    assert CI_FILES, f"no workflow files discovered under {WORKFLOWS_DIR}"
    offenders: list[str] = []
    for ci_file in CI_FILES:
        for target in _COV_RE.findall(ci_file.read_text()):
            if not (PROJECT_ROOT / _top_level_dir(target)).is_dir():
                offenders.append(ci_file.name + ": --cov=" + target)
    assert not offenders, "--cov flags reference packages that do not exist: " + str(offenders)


def test_coverage_source_dirs_exist() -> None:
    """Every [tool.coverage.run] source dir in pyproject must exist."""
    data = tomllib.loads(PYPROJECT.read_text())
    source = data["tool"]["coverage"]["run"].get("source", [])
    assert source, "pyproject [tool.coverage.run] has no source list"
    missing = [s for s in source if not (PROJECT_ROOT / s).is_dir()]
    assert not missing, "[tool.coverage.run] source references missing dirs: " + str(missing)
