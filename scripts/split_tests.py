#!/usr/bin/env python3
"""Partition the core pytest suite into balanced shards for parallel CI jobs.

``unit:pytest:core`` ran the whole suite in one job and had grown to a steady
14-15 minutes against a 20-minute timeout — close enough that a normal increase
in test count tipped it over. Splitting the run across jobs restores headroom
and halves the time contributors wait for a result.

The split is computed at run time rather than checked in, so it cannot drift as
test files are added, renamed, or deleted. Collection is asked for the test count
of every file, files are sorted heaviest first, and each is assigned to whichever
shard currently holds the fewest tests. That is a greedy bin-pack: not optimal,
but it keeps shards within a few percent of each other and is deterministic, so
a rerun of the same commit produces the same partition.

Test *count* is a proxy for test *duration*. It is a good enough proxy here
because the slow, uneven work — CDK synthesis, cdk-nag, cross-module integration
— already lives in its own dedicated jobs and is excluded below.

Usage::

    # Files for shard 1 of 2, one per line
    python scripts/split_tests.py --shard 1 --of 2

    # What the partition looks like, without running anything
    python scripts/split_tests.py --of 2 --summary

Exits non-zero if collection fails, so a broken suite fails the job rather than
silently producing an empty shard.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_ROOT = "tests"

#: Test modules excluded from the core suite because a dedicated CI job runs
#: them. Keeping the mapping here — rather than as bare ``--ignore`` flags in
#: the workflow — means the two shard jobs cannot disagree about the exclusions,
#: and the reason for each is visible next to the path.
DEDICATED_JOB_MODULES: dict[str, str] = {
    "tests/test_integration.py": "integration:pytest:cross-module",
    "tests/test_mcp_integration.py": "integration:mcp:server",
    "tests/test_nag_compliance.py": "unit:cdk:nag-compliance",
    "tests/test_cdk_synthesis_matrix.py": "unit:cdk:config-matrix",
    "tests/test_project_name_scoping.py": "unit:cdk:project-name-scoping",
    "tests/test_accelerator_catalog.py": "unit:pytest:core, offline policy step",
    "tests/test_accelerator_pools.py": "unit:pytest:core, offline policy step",
}


def ignore_args() -> list[str]:
    """Return the ``--ignore`` flags that carve the core suite out of ``tests/``."""
    return [f"--ignore={path}" for path in sorted(DEDICATED_JOB_MODULES)]


def collect_counts() -> dict[str, int]:
    """Return ``{test file: number of collected tests}`` for the core suite.

    Uses ``pytest --collect-only -q``, which lists one node id per line, so the
    counts reflect parametrization rather than the number of ``def test_``
    statements.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            SUITE_ROOT,
            # ``-o addopts=`` clears the project's ``addopts`` (which sets -v).
            # Without it verbosity nets to 0 and --collect-only prints an
            # indented tree instead of one node id per line, which this parser
            # cannot read. Overriding rather than adding more -q flags keeps the
            # output shape independent of project-level verbosity settings.
            "-o",
            "addopts=",
            "-q",
            "--collect-only",
            "--no-header",
            "-p",
            "no:cacheprovider",
            *ignore_args(),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(
            f"pytest collection failed with exit code {result.returncode}; "
            "cannot partition the suite"
        )

    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        node = line.strip()
        if "::" not in node:
            continue
        path = node.split("::", 1)[0]
        if not path.endswith(".py"):
            continue
        counts[path] = counts.get(path, 0) + 1

    if not counts:
        raise SystemExit("pytest collection produced no test ids; refusing to shard")
    return counts


def balance(counts: dict[str, int], shards: int) -> list[list[str]]:
    """Greedily bin-pack files into ``shards`` groups of similar test count.

    Files are placed heaviest first into the currently lightest shard. Ties are
    broken by path so the partition is stable across runs.
    """
    if shards < 1:
        raise SystemExit("--of must be at least 1")

    groups: list[list[str]] = [[] for _ in range(shards)]
    totals = [0] * shards

    for path, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(shards), key=lambda index: (totals[index], index))
        groups[target].append(path)
        totals[target] += count

    return [sorted(group) for group in groups]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--of", type=int, required=True, help="Total number of shards.")
    parser.add_argument("--shard", type=int, help="Which shard to print (1-based).")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the per-shard test counts instead of a file list.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the partition as JSON.")
    args = parser.parse_args(argv)

    counts = collect_counts()
    groups = balance(counts, args.of)

    if args.json:
        print(
            json.dumps(
                {
                    "total_tests": sum(counts.values()),
                    "total_files": len(counts),
                    "shards": [
                        {"shard": i + 1, "tests": sum(counts[p] for p in g), "files": g}
                        for i, g in enumerate(groups)
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.summary:
        total = sum(counts.values())
        print(f"{total} tests across {len(counts)} files -> {args.of} shard(s)")
        for index, group in enumerate(groups, 1):
            shard_total = sum(counts[path] for path in group)
            share = (shard_total / total * 100) if total else 0
            print(f"  shard {index}: {shard_total:5d} tests ({share:5.1f}%) in {len(group)} files")
        return 0

    if args.shard is None:
        raise SystemExit("--shard is required unless --summary or --json is given")
    if not 1 <= args.shard <= args.of:
        raise SystemExit(f"--shard must be between 1 and {args.of}")

    for path in groups[args.shard - 1]:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
