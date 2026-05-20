"""Validate the .pip-audit-ignore suppression file.

Each entry must:
  * have a non-comment, non-blank line whose first whitespace-delimited
    token is the vulnerability ID (e.g. ``PYSEC-2025-183``,
    ``CVE-2025-45768``, ``GHSA-xxxx-xxxx-xxxx``); and
  * include an ``exp:YYYY-MM-DD`` marker somewhere on the same line.

The check fails the workflow when:
  * any entry's ``exp:`` date is on-or-before the reference date
    (today by default; configurable via ``--today`` for tests); or
  * any entry is missing the ``exp:`` marker entirely or has a
    malformed date.

Inclusive expiration is intentional — once the listed date arrives the
suppression is considered expired, no bonus day. That mirrors how
``.trivyignore`` is treated by the rest of the project.

Usage::

    python3 .github/scripts/check_pip_audit_ignore.py .pip-audit-ignore
    python3 .github/scripts/check_pip_audit_ignore.py .pip-audit-ignore --today 2026-08-19

Exit codes::

    0  OK (or file does not exist — an absent ignore file is not an error)
    1  one or more entries failed validation
    2  unexpected I/O / argument error

The module is importable from the test suite — call ``check_file()``
directly to exercise the logic against fixtures.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

EXP_RE = re.compile(r"\bexp:(\d{4})-(\d{2})-(\d{2})\b")


def check_file(
    path: Path, today: datetime.date | None = None
) -> tuple[list[tuple[int, str]], list[tuple[int, str, datetime.date]]]:
    """Return (missing, expired) entry lists for the given ignore file.

    ``missing`` lists ``(line_number, vuln_id)`` for entries without a
    valid ``exp:YYYY-MM-DD`` marker. ``expired`` lists
    ``(line_number, vuln_id, exp_date)`` for entries whose date is
    on-or-before ``today``.

    ``today`` defaults to ``datetime.date.today()`` and exists as a
    parameter so the test suite can pin a deterministic reference date.

    A non-existent file returns two empty lists — the absence of an
    ignore file is not an error.
    """
    today = today or datetime.date.today()
    missing: list[tuple[int, str]] = []
    expired: list[tuple[int, str, datetime.date]] = []

    if not path.exists():
        return missing, expired

    for lineno, raw in enumerate(path.read_text().splitlines(), start=1):
        stripped = raw.strip()
        # Blank lines and full-line comments are skipped.
        if not stripped or stripped.startswith("#"):
            continue
        # First whitespace token is the vuln ID; rest is rationale + exp:.
        tokens = stripped.split(None, 1)
        vuln_id = tokens[0]
        rest = tokens[1] if len(tokens) > 1 else ""
        match = EXP_RE.search(rest)
        if not match:
            missing.append((lineno, vuln_id))
            continue
        year, month, day = (int(part) for part in match.groups())
        try:
            exp_date = datetime.date(year, month, day)
        except ValueError:
            # Catches e.g. exp:2026-13-40 — invalid date components.
            missing.append((lineno, vuln_id))
            continue
        if exp_date <= today:
            expired.append((lineno, vuln_id, exp_date))

    return missing, expired


def _format_report(
    today: datetime.date,
    missing: list[tuple[int, str]],
    expired: list[tuple[int, str, datetime.date]],
) -> str:
    """Render the human-facing error report. Empty string when both clean."""
    lines: list[str] = []
    if missing:
        lines.append("ERROR: .pip-audit-ignore entries missing a valid exp:YYYY-MM-DD marker:")
        for lineno, vuln_id in missing:
            lines.append(f"  line {lineno}: {vuln_id}")
    if expired:
        lines.append(
            f"ERROR: .pip-audit-ignore entries past their expiration date "
            f"(today is {today.isoformat()}):"
        )
        for lineno, vuln_id, exp_date in expired:
            lines.append(f"  line {lineno}: {vuln_id} expired on {exp_date.isoformat()}")
        lines.append(
            "Re-evaluate each entry: remove it if the CVE is fixed, or extend "
            "the date with fresh rationale."
        )
    return "\n".join(lines)


def _parse_today(value: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--today must be YYYY-MM-DD, got {value!r}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "path",
        type=Path,
        help="Path to the .pip-audit-ignore file to validate.",
    )
    parser.add_argument(
        "--today",
        type=_parse_today,
        default=None,
        help=(
            "Reference date in YYYY-MM-DD format. Defaults to today's "
            "UTC-naive date. Provided for deterministic testing."
        ),
    )
    args = parser.parse_args(argv)

    today = args.today or datetime.date.today()
    missing, expired = check_file(args.path, today=today)
    report = _format_report(today, missing, expired)
    if report:
        print(report)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
