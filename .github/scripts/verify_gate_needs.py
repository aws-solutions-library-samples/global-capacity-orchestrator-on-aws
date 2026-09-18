#!/usr/bin/env python3
"""Fail a workflow's gate job unless every job it needs succeeded.

Every PR-triggered workflow ends in one ``gate:<workflow>`` job that ``needs``
each other job in the file and is scheduled with ``always()``. Branch
protection requires only those gates, so the merge rule is "every job in
every PR workflow passed" without naming the jobs — matrix legs, renamed jobs
and new jobs included — and a ruleset entry can never drift from a job name.

The gate step hands this script the ``needs`` context (``toJSON(needs)``)::

    {"job-id": {"result": "success|failure|cancelled|skipped", "outputs": {...}}}

Every job must have ``result == "success"``. A job may be ``skipped`` only when
named by ``--allow-skipped``, and the ``JOB=DEP.OUTPUT=VALUE`` form additionally
requires the named output of another needed job to carry that value — the way
a path-filtered job proves its skip was the filter's decision rather than a
broken ``if:``. Anything else (failure, cancellation, an unexplained skip, a
result this script does not recognise) fails the gate.

Exit codes: ``0`` every job passed, ``1`` at least one job did not, ``2`` the
input or the skip policy is malformed — so "the gate was misconfigured" never
reads as "a job failed" or, worse, as a pass. Stdlib only: the gate job needs
no dependency install.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The only result that satisfies the gate without a skip allowance.
SUCCESS = "success"
#: The only other result an allowance can excuse.
SKIPPED = "skipped"


class PolicyError(ValueError):
    """The needs document or the skip policy is malformed (exit 2)."""


@dataclass(frozen=True)
class OutputCondition:
    """``DEP.OUTPUT=VALUE``: the skip is legitimate only while this output holds."""

    dependency: str
    output: str
    value: str

    def describe(self) -> str:
        return f"{self.dependency}.outputs.{self.output} == {self.value!r}"


@dataclass(frozen=True)
class SkipAllowance:
    """``JOB`` or ``JOB=DEP.OUTPUT=VALUE`` from ``--allow-skipped``."""

    job: str
    condition: OutputCondition | None = None

    def describe(self) -> str:
        if self.condition is None:
            return "skip allowed unconditionally"
        return f"skip allowed while {self.condition.describe()}"


def parse_allowance(spec: str) -> SkipAllowance:
    """Parse one ``--allow-skipped`` value; malformed specs are a PolicyError."""
    job, separator, condition = spec.partition("=")
    if not job:
        raise PolicyError(f"--allow-skipped {spec!r}: empty job id")
    if not separator:
        return SkipAllowance(job=job)
    dependency, dot, rest = condition.partition(".")
    output, equals, value = rest.partition("=")
    if not (dependency and dot and output and equals and value):
        raise PolicyError(
            f"--allow-skipped {spec!r}: expected JOB or JOB=DEP.OUTPUT=VALUE "
            "(DEP is another needed job id, OUTPUT one of its outputs)"
        )
    return SkipAllowance(job=job, condition=OutputCondition(dependency, output, value))


def parse_needs(document: str) -> dict[str, dict[str, Any]]:
    """Decode ``toJSON(needs)``; anything but a non-empty object of objects is a PolicyError."""
    try:
        needs = json.loads(document)
    except json.JSONDecodeError as exc:
        raise PolicyError(f"--needs is not valid JSON: {exc}") from exc
    if not isinstance(needs, dict) or not needs:
        raise PolicyError(
            "--needs must be the non-empty `needs` context object; a gate that "
            "needs nothing would pass vacuously"
        )
    for job_id, entry in needs.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("result"), str):
            raise PolicyError(f"needs[{job_id!r}] is missing a string `result`")
    return needs


def _index_allowances(
    needs: dict[str, dict[str, Any]], allowances: list[SkipAllowance]
) -> dict[str, SkipAllowance]:
    """Key allowances by job; a stale or duplicate allowance is a policy bug."""
    by_job: dict[str, SkipAllowance] = {}
    for allowance in allowances:
        if allowance.job not in needs:
            raise PolicyError(
                f"--allow-skipped names {allowance.job!r}, which this gate does not need"
            )
        if allowance.condition is not None and allowance.condition.dependency not in needs:
            raise PolicyError(
                f"--allow-skipped for {allowance.job!r} reads "
                f"{allowance.condition.dependency!r}, which this gate does not need"
            )
        if allowance.job in by_job:
            raise PolicyError(f"--allow-skipped names {allowance.job!r} twice")
        by_job[allowance.job] = allowance
    return by_job


def _judge_skip(needs: dict[str, dict[str, Any]], allowance: SkipAllowance) -> tuple[str, bool]:
    """Note for a skipped job with an allowance, and whether the skip is excused."""
    condition = allowance.condition
    if condition is None:
        return allowance.describe(), True
    outputs = needs[condition.dependency].get("outputs")
    actual = outputs.get(condition.output) if isinstance(outputs, dict) else None
    if actual == condition.value:
        return allowance.describe(), True
    return (
        f"skipped, but {condition.dependency}.outputs.{condition.output} is "
        f"{actual!r}, not {condition.value!r}",
        False,
    )


def evaluate(
    needs: dict[str, dict[str, Any]], allowances: list[SkipAllowance]
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Judge every needed job.

    Returns ``(rows, violations)``: one ``(job, result, note)`` row per needed
    job in sorted order, and the human-readable violations.
    """
    by_job = _index_allowances(needs, allowances)
    rows: list[tuple[str, str, str]] = []
    violations: list[str] = []
    for job_id in sorted(needs):
        result = needs[job_id]["result"]
        if result == SUCCESS:
            rows.append((job_id, result, ""))
            continue
        allowance = by_job.get(job_id)
        if result == SKIPPED and allowance is not None:
            note, excused = _judge_skip(needs, allowance)
        else:
            note = "skipped without an allowance" if result == SKIPPED else f"result was {result!r}"
            excused = False
        rows.append((job_id, result, note))
        if not excused:
            violations.append(f"{job_id}: {note}")
    return rows, violations


def render_table(rows: list[tuple[str, str, str]]) -> str:
    """Markdown table of every needed job — the step summary and the log share it."""
    lines = ["| Job | Result | Note |", "|-----|--------|------|"]
    for job_id, result, note in rows:
        marker = "✅" if result == SUCCESS or note.startswith("skip allowed") else "❌"
        lines.append(f"| `{job_id}` | {marker} `{result}` | {note} |")
    return "\n".join(lines)


def write_step_summary(table: str, verdict: str) -> None:
    """Append the verdict to the job summary when GitHub provides one."""
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    with Path(summary_path).open("a", encoding="utf-8") as handle:
        handle.write(f"### Gate: {verdict}\n\n{table}\n\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--needs",
        required=True,
        help="the gate job's `needs` context as JSON (pass `${{ toJSON(needs) }}` through env)",
    )
    parser.add_argument(
        "--allow-skipped",
        action="append",
        default=[],
        metavar="JOB[=DEP.OUTPUT=VALUE]",
        help=(
            "a needed job whose `skipped` result is acceptable; with the "
            "DEP.OUTPUT=VALUE suffix, only while that output of another needed "
            "job carries that value (repeatable)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        needs = parse_needs(args.needs)
        allowances = [parse_allowance(spec) for spec in args.allow_skipped]
        rows, violations = evaluate(needs, allowances)
    except PolicyError as exc:
        print(f"::error::gate misconfigured: {exc}", file=sys.stderr)
        return 2

    table = render_table(rows)
    verdict = (
        "every needed job succeeded"
        if not violations
        else f"{len(violations)} job(s) did not succeed"
    )
    print(table)
    write_step_summary(table, verdict)
    for violation in violations:
        print(f"::error::{violation}", file=sys.stderr)
    if violations:
        print(f"::error::refusing a successful gate: {verdict}", file=sys.stderr)
        return 1
    print(f"Gate satisfied: {len(rows)} job(s) succeeded or were skipped by policy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
