"""Turn the weekly cve-scan's Trivy reports into its rolling-issue verdict.

``cve-scan.yml`` scans the source tree (``trivy fs``, lockfiles included) and
every buildable image (``trivy image``, the same matrix ``security.yml`` scans
on each PR) against that morning's vulnerability database. A red run used to be
the only signal, and nothing read it: the scan failed three Mondays running on
one HIGH in ``requirements-lock.txt`` before anyone looked. Its last job now
hands this script the reports and every scan job's result, and opens, refreshes
or closes one rolling issue from the verdict this script prints.

Each report is Trivy JSON written with ``--exit-code 0`` and the same filters
as the gating scan (severity, ``--ignore-unfixed`` for images, the dated
``.trivyignore``), so every vulnerability in it is one the gate fails on.

Verdicts, written as ``status=<verdict>`` to ``--github-output``:

``findings``
    at least one report lists a vulnerability. The issue body is the table.
``incomplete``
    no findings, but a scan job did not succeed, or a report the run should
    have produced is missing or unreadable. A scan that never ran is not a
    clean scan, so this opens the issue too, saying what is missing.
``clean``
    every scan job succeeded and every expected report is present and empty.
    The workflow closes the rolling issue.

Usage::

    python3 .github/scripts/cve_scan_report.py \\
        --reports-dir reports --needs-json "$NEEDS_JSON" \\
        --run-url "$RUN_URL" --output body.md --github-output "$GITHUB_OUTPUT"

The exit code is 0 for every verdict (the workflow branches on the output) and
2 for a malformed ``--needs-json``, which is a workflow bug rather than a
finding.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The filesystem report's file name, as cve-scan.yml writes it.
FS_REPORT = "trivy-cve-report.json"
#: Image reports are ``trivy-<image>-report.json``, one per matrix leg.
IMAGE_REPORT_PREFIX = "trivy-"
IMAGE_REPORT_SUFFIX = "-report.json"
#: The job whose ``outputs.images`` lists the images the matrix scanned.
DISCOVER_JOB = "discover-images"

ISSUE_TITLE = "[Automated] Weekly CVE scan is failing"
_REMEDIATION = (
    "## How to clear it\n\n"
    "- **OS package in an image** (`debian`, `amazon`): bump the image's "
    "`APT_SECURITY_EPOCH` / `DNF_SECURITY_EPOCH` "
    "([docs/MAINTENANCE.md](../blob/main/docs/MAINTENANCE.md#refreshing-base-image-security-patches)).\n"
    "- **Locked Python package** (`requirements-lock.txt`): move the pin with "
    "`pip-compile --upgrade-package` in the documented container "
    "([CONTRIBUTING.md](../blob/main/CONTRIBUTING.md#regenerating-the-lockfile)).\n"
    "- **No fixed release reachable**: a dated `.github/config/.trivyignore` entry with "
    "its justification "
    "([docs/MAINTENANCE.md](../blob/main/docs/MAINTENANCE.md#renewing-cve-suppressions)).\n"
)


@dataclass(frozen=True)
class Finding:
    """One vulnerability one report lists."""

    report: str
    target: str
    package: str
    vulnerability: str
    severity: str
    installed: str
    fixed: str


@dataclass(frozen=True)
class Verdict:
    """What the run found and what the issue should say."""

    status: str
    findings: tuple[Finding, ...]
    problems: tuple[str, ...]


def _cell(value: object) -> str:
    """A table cell: text with the characters Markdown tables break on escaped."""
    text = " ".join(str(value or "").split())
    return text.replace("|", "\\|") or "-"


def read_report(path: Path) -> list[Finding]:
    """The vulnerabilities in one Trivy JSON report.

    Raises ``ValueError`` when the file is not a Trivy report, so the caller
    can count it as missing evidence rather than as a clean scan.
    """
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path.name}: cannot be read as JSON ({exc})") from exc
    if not isinstance(document, dict) or not isinstance(document.get("Results", []), list):
        raise ValueError(f"{path.name}: not a Trivy report (no Results list)")
    findings: list[Finding] = []
    for result in document.get("Results") or []:
        if not isinstance(result, dict):
            continue
        for vuln in result.get("Vulnerabilities") or []:
            if not isinstance(vuln, dict):
                continue
            findings.append(
                Finding(
                    report=path.name,
                    target=str(result.get("Target") or ""),
                    package=str(vuln.get("PkgName") or ""),
                    vulnerability=str(vuln.get("VulnerabilityID") or ""),
                    severity=str(vuln.get("Severity") or ""),
                    installed=str(vuln.get("InstalledVersion") or ""),
                    fixed=str(vuln.get("FixedVersion") or ""),
                )
            )
    return findings


def expected_reports(needs: Mapping[str, Any]) -> list[str]:
    """The report file names this run should have produced.

    The filesystem report always, plus one per image the discovery job
    published. When discovery itself failed there is no image list to expect,
    and its failed result already makes the run incomplete.
    """
    names = [FS_REPORT]
    discover = needs.get(DISCOVER_JOB) or {}
    raw = (discover.get("outputs") or {}).get("images") if isinstance(discover, dict) else None
    if isinstance(raw, str) and raw.strip():
        try:
            images = json.loads(raw)
        except json.JSONDecodeError:
            images = []
        for image in images if isinstance(images, list) else []:
            if isinstance(image, str) and image:
                names.append(f"{IMAGE_REPORT_PREFIX}{image}{IMAGE_REPORT_SUFFIX}")
    return names


def evaluate(reports_dir: Path, needs: Mapping[str, Any]) -> Verdict:
    """Read every expected report and every job result into one verdict."""
    findings: list[Finding] = []
    problems: list[str] = []
    for job, details in sorted(needs.items()):
        result = details.get("result") if isinstance(details, dict) else None
        if result != "success":
            problems.append(f"job `{job}` finished `{result or 'unknown'}`")
    for name in expected_reports(needs):
        path = reports_dir / name
        if not path.is_file():
            problems.append(f"report `{name}` is missing")
            continue
        try:
            findings.extend(read_report(path))
        except ValueError as exc:
            problems.append(str(exc))
    if findings:
        status = "findings"
    elif problems:
        status = "incomplete"
    else:
        status = "clean"
    return Verdict(status=status, findings=tuple(findings), problems=tuple(problems))


def render(verdict: Verdict, *, run_url: str, today: datetime.date) -> str:
    """The rolling issue's body for a ``findings`` or ``incomplete`` verdict."""
    lines = [
        f"The scheduled CVE scan on {today.isoformat()} did not pass: [workflow run]({run_url}).",
        "",
        "Every row below fails the same scan on the next pull request "
        "(`security.yml` runs it with the same flags and ignore file).",
        "",
    ]
    if verdict.findings:
        lines += [
            "## Findings",
            "",
            "| Report | Target | Package | Vulnerability | Severity | Installed | Fixed |",
            "|---|---|---|---|---|---|---|",
        ]
        unique = sorted(
            set(verdict.findings),
            key=lambda f: (f.report, f.target, f.package, f.vulnerability),
        )
        for f in unique:
            lines.append(
                "| "
                + " | ".join(
                    _cell(v)
                    for v in (
                        f.report,
                        f.target,
                        f.package,
                        f.vulnerability,
                        f.severity,
                        f.installed,
                        f.fixed,
                    )
                )
                + " |"
            )
        lines.append("")
    if verdict.problems:
        lines += ["## Incomplete evidence", ""]
        lines += [f"- {problem}" for problem in verdict.problems]
        lines += [
            "",
            "A scan that did not run is not a clean scan; re-run the workflow "
            "once the cause is fixed.",
            "",
        ]
    lines.append(_REMEDIATION)
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--needs-json", required=True, help="toJSON(needs) of the report job")
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--output", type=Path, required=True, help="where to write the body")
    parser.add_argument("--github-output", type=Path, help="append status=<verdict> here")
    parser.add_argument("--today", type=datetime.date.fromisoformat, help="override for tests")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        needs: object = json.loads(args.needs_json)
    except json.JSONDecodeError as exc:
        print(f"--needs-json is not JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(needs, dict):
        print("--needs-json must be a JSON object (toJSON(needs))", file=sys.stderr)
        return 2
    verdict = evaluate(args.reports_dir, needs)
    today = args.today or datetime.datetime.now(datetime.UTC).date()
    args.output.write_text(render(verdict, run_url=args.run_url, today=today), encoding="utf-8")
    if args.github_output is not None:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"status={verdict.status}\n")
    print(
        f"cve-scan verdict: {verdict.status} "
        f"({len(set(verdict.findings))} finding(s), {len(verdict.problems)} problem(s))"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
