#!/usr/bin/env python3
"""Gate npm-audit JSON with exact, expiring suppressions.

Suppression file format (one entry per non-comment line)::

    package-dir|package|advisory|node-path|exp:YYYY-MM-DD

A finding is suppressed only when all four identity fields match. Suppressions
expire inclusively and stale entries fail, forcing their removal after the
upstream dependency is fixed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ADVISORY_RE = re.compile(r"^https://github\.com/advisories/(GHSA-[0-9a-z-]+)$")
SEVERITY = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


@dataclass(frozen=True)
class Suppression:
    package_dir: str
    package: str
    advisory: str
    node_path: str
    expires: dt.date
    line: int

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.package_dir, self.package, self.advisory, self.node_path)


def _load_suppressions(path: Path, today: dt.date) -> list[Suppression]:
    if not path.is_file():
        raise ValueError(f"suppression file not found: {path}")

    suppressions: list[Suppression] = []
    identities: set[tuple[str, str, str, str]] = set()
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        fields = line.split("|")
        if len(fields) != 5 or not fields[4].startswith("exp:"):
            raise ValueError(
                f"{path}:{line_number}: expected package-dir|package|advisory|"
                "node-path|exp:YYYY-MM-DD"
            )
        package_dir, package, advisory, node_path, expiry_field = fields
        if not all((package_dir, package, advisory, node_path)):
            raise ValueError(f"{path}:{line_number}: suppression fields cannot be empty")
        try:
            expires = dt.date.fromisoformat(expiry_field.removeprefix("exp:"))
        except ValueError as exc:
            raise ValueError(f"{path}:{line_number}: invalid expiration date") from exc
        if expires <= today:
            raise ValueError(f"{path}:{line_number}: {advisory} expired on {expires.isoformat()}")

        suppression = Suppression(
            package_dir=package_dir,
            package=package,
            advisory=advisory,
            node_path=node_path,
            expires=expires,
            line=line_number,
        )
        if suppression.identity in identities:
            raise ValueError(f"{path}:{line_number}: duplicate suppression")
        identities.add(suppression.identity)
        suppressions.append(suppression)
    return suppressions


def _advisories(via: Any) -> set[str]:
    advisories: set[str] = set()
    if not isinstance(via, list):
        return advisories
    for item in via:
        if not isinstance(item, dict):
            continue
        match = ADVISORY_RE.fullmatch(str(item.get("url", "")))
        if match:
            advisories.add(match.group(1))
    return advisories


def _load_report(path: Path) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid npm-audit JSON in {path}: {exc}") from exc
    if not isinstance(report, dict) or isinstance(report.get("error"), dict):
        raise ValueError(f"npm audit returned an operational error: {report.get('error')!r}")
    if not isinstance(report.get("vulnerabilities"), dict):
        raise ValueError("npm-audit report is missing the vulnerabilities object")
    return report


def check_report(report: dict[str, Any], package_dir: str, suppressions: list[Suppression]) -> int:
    scoped = {item.identity: item for item in suppressions if item.package_dir == package_dir}
    used: set[tuple[str, str, str, str]] = set()
    failures: list[str] = []

    for package, finding in report["vulnerabilities"].items():
        if not isinstance(finding, dict):
            failures.append(f"{package}: malformed vulnerability record")
            continue
        if SEVERITY.get(str(finding.get("severity", "")), -1) < SEVERITY["high"]:
            continue

        advisories = _advisories(finding.get("via"))
        nodes = set(finding.get("nodes", [])) if isinstance(finding.get("nodes"), list) else set()
        matches = {
            identity
            for identity in scoped
            if identity[1] == package and identity[2] in advisories and identity[3] in nodes
        }
        # Fail closed for compound records: every advisory and node must be
        # represented, and no broader package-level suppression is accepted.
        expected = {
            (package_dir, package, advisory, node) for advisory in advisories for node in nodes
        }
        if advisories and nodes and matches == expected:
            used.update(matches)
            for identity in sorted(matches):
                suppression = scoped[identity]
                print(
                    "::warning::Temporarily suppressing "
                    f"{suppression.advisory} for {suppression.node_path} "
                    f"until {suppression.expires.isoformat()}"
                )
        else:
            failures.append(
                f"{package}: unsuppressed {finding.get('severity', 'unknown')} finding "
                f"(advisories={sorted(advisories)}, nodes={sorted(nodes)})"
            )

    stale = set(scoped) - used
    for identity in sorted(stale):
        suppression = scoped[identity]
        failures.append(
            f"stale suppression at line {suppression.line}: {suppression.advisory} "
            f"for {suppression.node_path} no longer matches npm audit"
        )

    for failure in failures:
        print(f"ERROR: {failure}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--package-dir", required=True)
    parser.add_argument("--ignore-file", required=True, type=Path)
    args = parser.parse_args(argv)

    try:
        suppressions = _load_suppressions(args.ignore_file, dt.date.today())
        report = _load_report(args.report)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return check_report(report, args.package_dir, suppressions)


if __name__ == "__main__":
    raise SystemExit(main())
