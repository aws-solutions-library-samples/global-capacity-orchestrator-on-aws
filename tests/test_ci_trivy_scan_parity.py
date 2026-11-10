"""The weekly cve-scan scans exactly what the per-PR security gate scans.

``cve-scan.yml`` exists to say on Monday what the next pull request will hit.
That only holds while both workflows run Trivy over the same inputs with the
same flags. They drifted once: the PR-side filesystem scan had no
``--file-patterns`` for ``requirements-lock.txt``, so a HIGH in a locked
package failed the weekly scan for three weeks while every PR passed. These
tests pin the shared parts so a change to one workflow has to be made in both.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SECURITY = ROOT / ".github" / "workflows" / "security.yml"
CVE_SCAN = ROOT / ".github" / "workflows" / "cve-scan.yml"
LOCK_PATTERN = r"pip:requirements-lock\.txt$"


def _jobs(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected one step named {name!r}"
    return matches[0]


def _invocations(script: str, tool: str) -> list[list[str]]:
    """Every ``trivy <tool>`` command in a run script, as argv lists."""
    joined = re.sub(r"\\\n\s*", " ", script)
    commands = []
    for raw in joined.splitlines():
        command = raw.strip()
        if command.startswith(f"trivy {tool} "):
            commands.append(shlex.split(command))
    assert commands, f"no 'trivy {tool}' command found"
    return commands


def _gating_flags(argv: list[str]) -> list[str]:
    """The flags that decide what fails, without the per-run output handling."""
    flags: list[str] = []
    skip_next = False
    for index, token in enumerate(argv[2:], start=2):
        if skip_next:
            skip_next = False
            continue
        if token in ("--format", "--output"):
            skip_next = True
            continue
        if token == "--exit-code":
            skip_next = True
            continue
        if token.startswith("-") and index + 1 < len(argv) and not argv[index + 1].startswith("-"):
            flags.append(f"{token}={argv[index + 1]}")
            skip_next = True
        elif token.startswith("-"):
            flags.append(token)
    return flags


def _fs_scripts() -> dict[str, str]:
    return {
        "security.yml": _step(
            _jobs(SECURITY)["security-trivy-filesystem"], "Trivy filesystem scan"
        )["run"],
        "cve-scan.yml": _step(
            _jobs(CVE_SCAN)["cve-scan"], "Trivy CVE scan (table → log, JSON → artifact)"
        )["run"],
    }


@pytest.mark.parametrize("workflow", ["security.yml", "cve-scan.yml"])
def test_every_filesystem_scan_reads_the_top_level_lockfile(workflow: str) -> None:
    for argv in _invocations(_fs_scripts()[workflow], "fs"):
        assert f"--file-patterns={LOCK_PATTERN}" in _gating_flags(argv), argv


def test_filesystem_scans_gate_on_the_same_flags() -> None:
    scripts = _fs_scripts()
    security = {tuple(_gating_flags(argv)) for argv in _invocations(scripts["security.yml"], "fs")}
    weekly = {tuple(_gating_flags(argv)) for argv in _invocations(scripts["cve-scan.yml"], "fs")}
    assert len(security) == 1, "security.yml's two fs invocations disagree"
    assert security == weekly


def test_image_scans_gate_on_the_same_flags() -> None:
    security = _step(_jobs(SECURITY)["security-trivy-container-scan"], "Trivy image scan")["run"]
    weekly = _step(_jobs(CVE_SCAN)["container-scan"], "Trivy image scan")["run"]
    security_flags = {tuple(_gating_flags(argv)) for argv in _invocations(security, "image")}
    weekly_flags = {tuple(_gating_flags(argv)) for argv in _invocations(weekly, "image")}
    assert len(security_flags) == 1
    assert security_flags == weekly_flags


def test_both_workflows_scan_the_same_image_matrix() -> None:
    def discovery(path: Path, job: str) -> str:
        script = _step(_jobs(path)[job], "Discover buildable images")["run"]
        return next(line.strip() for line in script.splitlines() if "gco.service_images" in line)

    assert discovery(SECURITY, "security-discover-images") == discovery(CVE_SCAN, "discover-images")


def test_weekly_images_come_from_the_pr_layer_cache_without_writing_it() -> None:
    security = _step(_jobs(SECURITY)["security-trivy-container-scan"], "Build image")["with"]
    weekly = _step(_jobs(CVE_SCAN)["container-scan"], "Build image")["with"]
    assert weekly["cache-from"] == security["cache-from"]
    assert "cache-to" not in weekly
    for key in ("context", "file", "tags", "load"):
        assert weekly[key] == security[key]


def test_the_report_job_judges_every_scan_job() -> None:
    jobs = _jobs(CVE_SCAN)
    report = jobs["report"]
    scan_jobs = sorted(job for job in jobs if job != "report")
    assert sorted(report["needs"]) == scan_jobs
    assert report["if"].startswith("always()")
    assert report["permissions"] == {"contents": "read", "issues": "write"}
    verdict = _step(report, "Judge the run")
    assert ".github/scripts/cve_scan_report.py" in verdict["run"]
    assert verdict["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
