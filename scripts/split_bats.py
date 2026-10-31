#!/usr/bin/env python3
"""Partition the BATS suite into balanced shards for parallel CI jobs.

``unit:bats:shell`` ran the whole suite in one job, once plain and once traced
under bashcov: 971 tests, 21-22 minutes on 2026-10-30, twice the longest pytest
shard and the job every other result in the Unit Tests workflow waited for.
Splitting it across jobs the way scripts/split_tests.py splits the core pytest
suite brings it down to the cost of the heaviest single ``.bats`` file.

The split is computed at run time, so it cannot drift as suites are added,
renamed or deleted: every ``tests/BATS/*.bats`` file is weighed, files are
sorted heaviest first, and each goes to whichever shard is currently lightest
(the same greedy bin-pack as split_tests.py, imported from it so the two cannot
disagree).

A file's weight is its ``@test`` count times a per-suite cost. For pytest the
count alone is a fair proxy, because the slow, uneven work lives in dedicated
jobs. Here it is not: a typical bats test costs 0.1-0.5 s, while one that runs
the whole dependency-scan driver end to end against the faked upstream world
costs about 9 s plain and 11 s traced. Balancing by count alone would put that
suite in a shard with others and make it a third longer than the heaviest file
by itself (measured on the 2026-10-30 timings: 568 s against 440 s).
SUITE_WEIGHTS carries the relative per-test cost of each suite whose tests are
not typical, with the measurement it came from, so a reader can re-measure.

Usage::

    # Files for shard 1 of 5, one per line
    python3 scripts/split_bats.py --shard 1 --of 5

    # What the partition looks like, without running anything
    python3 scripts/split_bats.py --of 5 --summary

Standard library only: the job that runs it has bats, Ruby and the runner's
python3, not the project's virtualenv. Exits non-zero if the suite directory
holds no ``.bats`` files, so a moved suite fails the job rather than silently
producing empty shards.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SUITE_ROOT = "tests/BATS"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from split_tests import balance  # noqa: E402

#: One ``@test`` per line that starts with the keyword, which is how bats-core
#: itself counts (``bats --count``): a test is always declared at column one.
TEST_LINE = re.compile(r"^@test\s", re.MULTILINE)

#: Relative per-test cost of the suites whose tests are not typical, with the
#: measurement each number came from. A suite not listed costs 1 per test.
#: Re-measure from a ``unit:bats:shell`` log (the TAP lines carry timestamps)
#: when a suite's shape changes; ``--summary`` shows the resulting balance.
SUITE_WEIGHTS: dict[str, tuple[int, str]] = {
    "tests/BATS/test_dependency_scan_driver.bats": (
        25,
        "each test runs .github/scripts/dependency-scan.sh end to end against the "
        "faked upstream world: 8.8 s plain and 11.2 s traced per test on 2026-10-30, "
        "against 0.16 s plain and 0.62 s traced for the average test elsewhere, so "
        "one of its tests costs about 25 typical tests",
    ),
}


def collect_counts(root: Path = REPO_ROOT) -> dict[str, int]:
    """Return ``{suite path: number of @test declarations}`` for every suite."""
    suite_dir = root / SUITE_ROOT
    counts: dict[str, int] = {}
    for path in sorted(suite_dir.glob("*.bats")):
        relative = path.relative_to(root).as_posix()
        counts[relative] = len(TEST_LINE.findall(path.read_text(encoding="utf-8")))
    if not counts:
        raise SystemExit(f"no .bats files under {suite_dir}; refusing to shard")
    return counts


def weigh(counts: dict[str, int]) -> dict[str, int]:
    """Return ``{suite path: weight}``: the test count times the suite's per-test cost."""
    unknown = sorted(set(SUITE_WEIGHTS) - set(counts))
    if unknown:
        raise SystemExit(f"SUITE_WEIGHTS names suites that do not exist: {unknown}")
    return {path: count * SUITE_WEIGHTS.get(path, (1, ""))[0] for path, count in counts.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--of", type=int, required=True, help="Total number of shards.")
    parser.add_argument("--shard", type=int, help="Which shard to print (1-based).")
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print the per-shard test counts and weights instead of a file list.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the partition as JSON.")
    args = parser.parse_args(argv)

    counts = collect_counts()
    weights = weigh(counts)
    groups = balance(weights, args.of)

    if args.json:
        print(
            json.dumps(
                {
                    "total_tests": sum(counts.values()),
                    "total_weight": sum(weights.values()),
                    "total_files": len(counts),
                    "shards": [
                        {
                            "shard": index + 1,
                            "tests": sum(counts[path] for path in group),
                            "weight": sum(weights[path] for path in group),
                            "files": group,
                        }
                        for index, group in enumerate(groups)
                    ],
                },
                indent=2,
            )
        )
        return 0

    if args.summary:
        total_tests = sum(counts.values())
        total_weight = sum(weights.values())
        print(
            f"{total_tests} tests (weight {total_weight}) across {len(counts)} suites "
            f"-> {args.of} shard(s)"
        )
        for index, group in enumerate(groups, 1):
            shard_tests = sum(counts[path] for path in group)
            shard_weight = sum(weights[path] for path in group)
            share = (shard_weight / total_weight * 100) if total_weight else 0
            print(
                f"  shard {index}: {shard_tests:4d} tests, weight {shard_weight:5d} "
                f"({share:5.1f}%) in {len(group)} suites"
            )
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
