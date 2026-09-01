"""Dependency maintenance commands.

``gco deps scan`` wraps the repository's dependency scanner
(``.github/scripts/dependency-scan.sh``) — the same script the monthly
``deps-scan`` workflow runs to build the rolling
"[Automated] Dependency updates available" issue — so an operator or agent
can generate the exact same update list on demand instead of waiting for
the schedule or hand-assembling the invocation.

The scanner communicates through the GitHub Actions file-output protocol
(``$GITHUB_OUTPUT``); this wrapper points that at a private temp file and
reads back ``has_drift`` / ``scan_complete`` / ``report_path``, so the
script itself runs bit-for-bit the way CI runs it.

Two operating modes:

* Full scan (default) — every surface the workflow checks: Python/npm
  pins, Docker images, Helm charts, EKS add-ons, Dockerfile.dev ARGs,
  autopilot pins, pre-commit hooks, CI tooling, version consistency,
  suppression expiries, lockfile freshness, and the accelerator-catalog /
  Karpenter NodePool policy. Surfaces that need AWS credentials or tools
  the host is missing are skipped and reported as incomplete, exactly as
  in CI.
* ``--nodepools-only`` — just the accelerator-catalog / NodePool freshness
  check (``scripts/accelerator_catalog.py``): the deterministic offline
  validation always runs; the live EC2 catalog comparison runs when AWS
  credentials resolve and is reported as skipped otherwise.

Honest side-effect warning: the full scan's Python surface runs
``pip install -e ".[<every extra>]"`` into the *active environment* (that
is how it asks pip for outdated direct pins), mirroring the throwaway CI
environment. Run it from the dev container or a dedicated venv if that
matters to you.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, cast

import click

#: Tools the full scan shells out to, and the surfaces that go incomplete
#: without them. Missing entries are warnings, not errors — the scanner
#: records the gap and keeps going, exactly as it does in CI.
_OPTIONAL_TOOLS: tuple[tuple[str, str], ...] = (
    ("jq", "Python-package report rendering"),
    ("curl", "npm / GitHub / endoflife.date lookups"),
    ("skopeo", "Docker image tag and digest checks"),
    ("helm", "Helm chart version checks"),
    ("aws", "EKS / Aurora / EMR / Bedrock / online accelerator checks"),
)

_SCAN_SCRIPT = Path(".github") / "scripts" / "dependency-scan.sh"
_CATALOG_SCRIPT = Path("scripts") / "accelerator_catalog.py"


def _fail(message: str) -> NoReturn:
    raise click.ClickException(message)


def _repo_root() -> Path:
    """Resolve the checkout root; the scanner only exists in a git checkout."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _fail(
            "gco deps scan must run from inside a GCO checkout "
            "(the dependency scanner lives under .github/scripts/)"
        )
    root = Path(result.stdout.strip())
    if not (root / _SCAN_SCRIPT).is_file():
        _fail(f"{root} has no {_SCAN_SCRIPT} — not a GCO checkout?")
    return root


def _parse_github_output(path: Path) -> dict[str, str]:
    """Parse the ``key=value`` lines the scanner writes to $GITHUB_OUTPUT."""
    outputs: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return outputs
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            outputs[key.strip()] = value.strip()
    return outputs


def _sts_identity_available() -> bool:
    """Mirror the scanner's credential preflight for the nodepools fast path."""
    if shutil.which("aws") is None:
        return False
    probe = subprocess.run(
        ["aws", "sts", "get-caller-identity"],
        capture_output=True,
        text=True,
        check=False,
    )
    return probe.returncode == 0


def _run_nodepools_check(repo_root: Path) -> dict[str, object]:
    """Run the accelerator-catalog / NodePool freshness checks.

    Returns a JSON-friendly envelope with an ``offline`` section (always
    runs; deterministic) and an ``online`` section (runs when AWS
    credentials resolve, mirrors the scanner's STS preflight).
    """
    offline_report = subprocess.run(
        [sys.executable, str(_CATALOG_SCRIPT), "validate", "--format", "markdown"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if offline_report.returncode not in (0, 1):
        _fail(
            "accelerator catalog validation failed operationally: "
            + (offline_report.stderr.strip() or f"exit {offline_report.returncode}")
        )
    finding_count = len(re.findall(r"^### ", offline_report.stdout, re.M))
    offline: dict[str, object] = {
        "status": "pass" if offline_report.returncode == 0 else "findings",
        "finding_count": finding_count,
        "report_markdown": offline_report.stdout,
    }

    online: dict[str, object]
    if not _sts_identity_available():
        online = {
            "status": "skipped",
            "skip_reason": (
                "No AWS credentials available for the online EC2 catalog check "
                "(needs ec2:DescribeRegions and ec2:DescribeInstanceTypes); "
                "offline policy validation still ran."
            ),
        }
    else:
        with tempfile.TemporaryDirectory(prefix="gco-deps-") as tmp:
            online_report_path = Path(tmp) / "online.md"
            online_run = subprocess.run(
                [
                    sys.executable,
                    str(_CATALOG_SCRIPT),
                    "check-online",
                    "--report",
                    str(online_report_path),
                    "--json-summary",
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
            )
            if online_run.returncode not in (0, 1):
                _fail(
                    "online accelerator catalog check failed operationally: "
                    + (online_run.stderr.strip() or f"exit {online_run.returncode}")
                )
            try:
                summary = json.loads(online_run.stdout)
            except json.JSONDecodeError:
                _fail("online accelerator catalog check emitted a malformed JSON summary")
            online = {
                "status": summary.get("status", "error"),
                "drift_count": summary.get("drift_count"),
                "regions_checked": summary.get("regions_checked"),
            }
            with contextlib.suppress(OSError):
                online["report_markdown"] = online_report_path.read_text(encoding="utf-8")

    has_drift = offline["status"] != "pass" or online.get("status") == "drift"
    return {
        "nodepools_only": True,
        "has_drift": has_drift,
        "scan_complete": online.get("status") != "skipped",
        "offline": offline,
        "online": online,
    }


def _run_full_scan(repo_root: Path, *, stream: bool) -> dict[str, object]:
    """Run the full dependency scanner and return its parsed envelope."""
    missing = [
        f"{tool} ({surfaces})" for tool, surfaces in _OPTIONAL_TOOLS if shutil.which(tool) is None
    ]
    if missing:
        click.echo(
            "warning: missing tools — these surfaces will be reported as "
            "incomplete: " + "; ".join(missing),
            err=True,
        )

    with tempfile.TemporaryDirectory(prefix="gco-deps-") as tmp:
        github_output = Path(tmp) / "github-output"
        github_output.touch()
        env = dict(os.environ)
        env["GITHUB_OUTPUT"] = str(github_output)
        # Never leak into a real Actions job summary if the caller's
        # environment happens to carry one.
        env.pop("GITHUB_STEP_SUMMARY", None)
        env.setdefault("WORKFLOWS_DIR", ".github/workflows")

        result = subprocess.run(  # noqa: S603 — fixed argv, repo-owned script
            ["bash", str(_SCAN_SCRIPT)],
            cwd=repo_root,
            env=env,
            check=False,
            capture_output=not stream,
            text=True,
        )
        if result.returncode != 0:
            detail = "" if stream else f"\n{(result.stderr or '')[-2000:]}"
            _fail(f"dependency scanner exited with status {result.returncode}{detail}")

        outputs = _parse_github_output(github_output)
        has_drift = outputs.get("has_drift") == "true"
        scan_complete = outputs.get("scan_complete") == "true"

        report_markdown: str
        if has_drift:
            report_path = outputs.get("report_path", "")
            try:
                report_markdown = Path(report_path).read_text(encoding="utf-8")
            except OSError:
                _fail("the scanner reported drift but its report file is unreadable")
        else:
            report_markdown = "# Dependency Update Report\n\n" + (
                "All dependencies are up to date.\n"
                if scan_complete
                else "No drift was found in completed checks, but the scan is "
                "incomplete — zero-count surfaces are provisional. See the "
                "scan log for skipped checks.\n"
            )

    envelope: dict[str, object] = {
        "has_drift": has_drift,
        "scan_complete": scan_complete,
        "report_markdown": report_markdown,
    }
    if not stream:
        envelope["log_tail"] = (result.stdout or "").splitlines()[-40:]
    return envelope


@click.group()
def deps() -> None:
    """Dependency maintenance (update scans, NodePool registry freshness)."""


@deps.command("scan")
@click.option(
    "--nodepools-only",
    is_flag=True,
    default=False,
    help=(
        "Run only the accelerator-catalog / Karpenter NodePool freshness "
        "check instead of the full scan."
    ),
)
@click.option(
    "--report",
    "report_file",
    type=click.Path(dir_okay=False, writable=True, path_type=Path),
    default=None,
    help="Write the Markdown report to this file instead of stdout.",
)
@click.pass_context
def scan(ctx: click.Context, nodepools_only: bool, report_file: Path | None) -> None:
    """Generate the dependency update list the monthly deps-scan produces.

    Runs the same scanner as the ``deps-scan`` GitHub Actions workflow and
    prints its Markdown report, so the update list in the rolling
    "[Automated] Dependency updates available" issue can be reproduced on
    demand. Surfaces that need AWS credentials or missing host tools are
    skipped and flagged as incomplete rather than failing the run.

    With ``--nodepools-only``, runs just the accelerator catalog /
    NodePool policy checks (offline always; live EC2 comparison when AWS
    credentials resolve).

    With the global ``-o json``, prints a machine-readable envelope
    (``has_drift``, ``scan_complete``, ``report_markdown``) instead of
    the bare report — this is the shape the MCP ``deps_scan`` tool
    returns.
    """
    repo_root = _repo_root()
    json_output = bool(ctx.obj) and getattr(ctx.obj, "output_format", "table") == "json"

    if nodepools_only:
        envelope = _run_nodepools_check(repo_root)
    else:
        envelope = _run_full_scan(repo_root, stream=not json_output)

    if json_output:
        click.echo(json.dumps(envelope, indent=2))
        return

    if nodepools_only:
        offline = cast("dict[str, object]", envelope["offline"])
        online = cast("dict[str, object]", envelope["online"])
        report_markdown = str(offline.get("report_markdown", ""))
        if online.get("report_markdown"):
            report_markdown += "\n" + str(online["report_markdown"])
        click.echo(f"offline policy check: {offline['status']}", err=True)
        click.echo(f"online EC2 catalog:   {online['status']}", err=True)
    else:
        report_markdown = str(envelope["report_markdown"])
        click.echo(f"has_drift:     {envelope['has_drift']}", err=True)
        click.echo(f"scan_complete: {envelope['scan_complete']}", err=True)

    if report_file is not None:
        report_file.write_text(report_markdown, encoding="utf-8")
        click.echo(f"report:        {report_file}", err=True)
    else:
        click.echo(report_markdown)
