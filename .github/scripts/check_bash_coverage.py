"""Enforce the shell-script coverage floor from a bashcov/SimpleCov report.

The ``unit:bats:shell`` job runs the BATS suite under ``bashcov``, which traces
Bash through ``BASH_XTRACEFD`` and writes a SimpleCov resultset describing every
*relevant* line of every shell file it saw and how many times each ran. This
script turns that resultset into a pass/fail gate.

Deciding which lines of a shell script are even executable is the hard part of
Bash coverage — here-documents, ``case`` arms, line continuations and function
headers all have to be classified — so that judgement is deliberately left to
bashcov's lexer rather than re-implemented here, with one correction. The lexer
works from the text alone and marks two shapes executable that Bash's tracer
never reports, so no test could ever cover them: a compound-command terminator
that carries only redirections (``done <<< "$rows"``, ``} > "$report"`` — the
redirection belongs to the loop or group, and ``set -x`` prints simple
commands, not the loop) and a ``case`` arm with no body (``*/*) ;;``). Those
lines are structure, not statements; ``untraceable_lines()`` recognises exactly
those two shapes and ``evaluate()`` leaves them out of the count, the way
SimpleCov leaves out a comment. Anything that carries a command — a pipe into
``sed`` after ``}``, a ``:`` in the arm, a process substitution — is still
measured. What this script owns beyond that is everything SimpleCov cannot know
about *this* repository:

**Path shape.** bashcov reports absolute paths (``/home/runner/work/.../demo/
lib_demo.sh``), while the inventory, the ratchet and every error message use
repository-relative ones. Reported paths are mapped back onto the tracked
script they refer to by longest path suffix, falling back to a unique basename,
and hits from every path that maps to the same script are merged — so a script
exercised by several suites is credited with all of them.

That merging also covers copies of a script, which matters because many BATS
suites ``cp`` the script under test into ``$BATS_TEST_TMPDIR`` and run it from
an isolated fake repository. It does not rescue those suites on its own,
though: SimpleCov reads each file when it renders the report, and by then BATS
has deleted its temporary directories, so the copies are dropped before this
script ever sees them. Such scripts stay on the ratchet until their suite is
reworked to run the file in place. The merging is what keeps a *surviving*
copy, or the same script seen under two different absolute prefixes, from
being counted as two half-covered files.

**The ratchet.** Bringing 11k lines of shell to 100% is staged work. Scripts
that have not got there yet are listed in ``[tool.bash-coverage] ratchet`` in
``pyproject.toml``; everything else must be fully covered. The list only ever
shrinks, and ``tests/test_check_bash_coverage.py`` keeps it honest.

The gate fails closed: a tracked script absent from the report entirely is an
error, not a pass, because that is what a silently mis-scoped bashcov run or a
suite that never executes its subject looks like.

Usage::

    python3 .github/scripts/check_bash_coverage.py coverage/
    python3 .github/scripts/check_bash_coverage.py coverage/.resultset.json

Exit codes::

    0  every enforced script is fully covered
    1  at least one enforced script has uncovered lines or is missing
    2  the report could not be found or parsed

The module is importable from the test suite — ``evaluate()`` holds the whole
decision and takes plain data, so it can be exercised without Ruby or bats.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404  # fixed argv, no shell: `git ls-files` only
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RESULTSET_NAME = ".resultset.json"

# A redirection and its target word: an optional descriptor, one of the
# operators Bash has (including here-strings and ``>|``/``>&``), then a single
# quoted or bare word. Deliberately not a process substitution (``< <(cmd)``):
# the command inside one is traced on this line, so the line is measurable.
_REDIRECTION = r"""\d*(?:<<<|>>|<>|>\||[<>]&?|<)\s*(?:"(?:[^"\\]|\\.)*"|'[^']*'|[^\s()<>|&;]+)"""

# ``done``, ``fi``, ``esac`` or ``}`` followed by nothing but redirections.
_TERMINATOR_WITH_REDIRECTIONS = re.compile(
    rf"^\s*(?:done|fi|esac|\}})(?:\s+{_REDIRECTION})+\s*(?:#.*)?$"
)

# A ``case`` arm whose body is empty: ``pattern) ;;``. A ``:`` or any other
# command in the arm is a traced statement and keeps the line measurable.
_EMPTY_CASE_ARM = re.compile(r"^\s*[^)#\s][^)#]*\)\s*;;\s*(?:#.*)?$")


def untraceable_lines(source: str) -> set[int]:
    """Return the 1-based lines of ``source`` that Bash's tracer never reports.

    See the module docstring: compound-command terminators carrying only
    redirections, and empty ``case`` arms. Both are marked executable by
    bashcov's lexer, so without this they read as permanently uncovered.
    """
    return {
        number
        for number, line in enumerate(source.splitlines(), start=1)
        if _TERMINATOR_WITH_REDIRECTIONS.match(line) or _EMPTY_CASE_ARM.match(line)
    }


class ReportError(Exception):
    """The bashcov report is missing, unreadable or not a SimpleCov resultset."""


@dataclass
class ScriptCoverage:
    """Merged coverage for one tracked script, across all of its copies."""

    path: str
    hits: dict[int, int] = field(default_factory=dict)
    sources: set[str] = field(default_factory=set)

    @property
    def measured(self) -> bool:
        """Whether any reported path mapped onto this script.

        False means no BATS suite executed it under a traced Bash, which is not
        the same as "fully covered" — the coverage is simply unknown.
        """
        return bool(self.sources)

    @property
    def total_lines(self) -> int:
        return len(self.hits)

    @property
    def missed_lines(self) -> list[int]:
        return sorted(line for line, count in self.hits.items() if count == 0)

    @property
    def percent(self) -> float:
        if not self.hits:
            return 100.0
        covered = self.total_lines - len(self.missed_lines)
        return 100.0 * covered / self.total_lines


@dataclass
class Result:
    """Outcome of a gate evaluation."""

    enforced: list[ScriptCoverage]
    ratcheted: list[ScriptCoverage]
    failures: list[str]
    unmapped: list[str]

    @property
    def ok(self) -> bool:
        return not self.failures


def find_report(target: Path) -> Path:
    """Return the SimpleCov resultset inside ``target``.

    ``target`` may be the resultset itself or the directory bashcov wrote
    (``coverage/`` by default).
    """
    if target.is_file():
        return target
    if not target.is_dir():
        raise ReportError(f"bashcov output path does not exist: {target}")
    direct = target / RESULTSET_NAME
    if direct.is_file():
        return direct
    nested = sorted(target.glob(f"**/{RESULTSET_NAME}"))
    if not nested:
        raise ReportError(f"no {RESULTSET_NAME} found under {target}")
    return nested[0]


def parse_report(report: Path) -> dict[str, dict[int, int]]:
    """Return ``{reported_path: {line_number: hits}}`` from a SimpleCov resultset.

    SimpleCov stores one entry per test command, each mapping an absolute file
    path to a list indexed by line number minus one, where ``null`` marks a
    line that is not executable and an integer is a hit count. Commands are
    merged by taking the highest count seen for each line, so a script executed
    by several suites is credited with all of them.
    """
    try:
        raw = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReportError(f"could not parse {report}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ReportError(f"{report} is not a SimpleCov resultset (expected an object)")

    parsed: dict[str, dict[int, int]] = {}
    for command in raw.values():
        if not isinstance(command, dict):
            continue
        coverage = command.get("coverage")
        if not isinstance(coverage, dict):
            continue
        for filename, entry in coverage.items():
            if not filename.endswith(".sh"):
                continue
            # SimpleCov 1.x nests the array under "lines"; older payloads store
            # the bare array. Accept both so a gem bump cannot silently zero
            # the gate.
            lines = entry.get("lines") if isinstance(entry, dict) else entry
            if not isinstance(lines, list):
                continue
            merged = parsed.setdefault(filename, {})
            for index, hits in enumerate(lines):
                if not isinstance(hits, int):
                    continue  # null => line is not executable
                line_number = index + 1
                merged[line_number] = max(merged.get(line_number, 0), hits)
    return parsed


def map_to_tracked(reported_path: str, inventory: list[str]) -> str | None:
    """Map a path from the report onto the tracked script it is a copy of.

    Prefers the longest matching path suffix (``/tmp/x/y/demo/lib_demo.sh``
    maps to ``demo/lib_demo.sh``). Falls back to matching on basename alone,
    which is safe only while script basenames are unique — a property
    ``tests/test_check_bash_coverage.py`` asserts against the real inventory.
    Returns ``None`` when nothing matches.
    """
    normalised = reported_path.replace("\\", "/")
    suffix_matches = [
        tracked
        for tracked in inventory
        if normalised == tracked or normalised.endswith("/" + tracked)
    ]
    if suffix_matches:
        return max(suffix_matches, key=lambda tracked: (tracked.count("/"), len(tracked)))

    basename = normalised.rsplit("/", 1)[-1]
    basename_matches = [tracked for tracked in inventory if tracked.rsplit("/", 1)[-1] == basename]
    if len(basename_matches) == 1:
        return basename_matches[0]
    return None


def evaluate(
    reported: dict[str, dict[int, int]],
    inventory: list[str],
    ratchet: list[str],
    untraceable: dict[str, set[int]] | None = None,
) -> Result:
    """Merge reported coverage onto the inventory and apply the floor.

    ``untraceable`` maps a tracked script to the line numbers that
    :func:`untraceable_lines` found in it; those lines are dropped from the
    script's count whatever the report says about them.
    """
    merged: dict[str, ScriptCoverage] = {path: ScriptCoverage(path=path) for path in inventory}
    unmapped: list[str] = []
    untraceable = untraceable or {}

    for reported_path, lines in sorted(reported.items()):
        tracked = map_to_tracked(reported_path, inventory)
        if tracked is None:
            unmapped.append(reported_path)
            continue
        record = merged[tracked]
        record.sources.add(reported_path)
        skipped = untraceable.get(tracked, set())
        for line_number, hits in lines.items():
            if line_number in skipped:
                continue
            record.hits[line_number] = max(record.hits.get(line_number, 0), hits)

    ratchet_set = set(ratchet)
    enforced = [record for path, record in sorted(merged.items()) if path not in ratchet_set]
    ratcheted = [record for path, record in sorted(merged.items()) if path in ratchet_set]

    failures: list[str] = []
    for record in enforced:
        if not record.measured:
            failures.append(
                f"{record.path}: absent from the bashcov report — no BATS suite executed it, "
                "so its coverage is unknown"
            )
            continue
        missed = record.missed_lines
        if missed:
            shown = ", ".join(str(line) for line in missed[:20])
            more = "" if len(missed) <= 20 else f" (+{len(missed) - 20} more)"
            failures.append(
                f"{record.path}: {len(missed)}/{record.total_lines} lines uncovered "
                f"({record.percent:.2f}%): {shown}{more}"
            )
    return Result(enforced=enforced, ratcheted=ratcheted, failures=failures, unmapped=unmapped)


def load_ratchet(pyproject: Path) -> list[str]:
    """Return ``[tool.bash-coverage] ratchet`` from pyproject, or ``[]``."""
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ReportError(f"could not read {pyproject}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ReportError(f"{pyproject} is not valid TOML: {exc}") from exc
    section = data.get("tool", {}).get("bash-coverage", {})
    return [str(item) for item in section.get("ratchet", [])]


def tracked_shell_scripts(root: Path) -> list[str]:
    """Return every tracked ``*.sh`` path, excluding the test-suite's own.

    Uses ``git ls-files`` so generated and untracked scripts never enter the
    gate, matching how ``lint:shellcheck:shell`` builds its inventory.
    """
    try:
        completed = subprocess.run(  # nosec B603  # fixed argv, no shell
            ["git", "-C", str(root), "ls-files", "*.sh"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReportError(f"could not list tracked shell scripts: {exc}") from exc
    paths = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return sorted(path for path in paths if not path.startswith("tests/"))


def untraceable_lines_by_script(root: Path, inventory: list[str]) -> dict[str, set[int]]:
    """Run :func:`untraceable_lines` over every tracked script under ``root``."""
    found: dict[str, set[int]] = {}
    for path in inventory:
        try:
            source = (root / path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReportError(f"could not read tracked script {path}: {exc}") from exc
        lines = untraceable_lines(source)
        if lines:
            found[path] = lines
    return found


def format_report(result: Result) -> str:
    """Render the human-facing summary printed by ``main``."""
    lines: list[str] = []
    failing = [record for record in result.enforced if record.missed_lines or not record.measured]
    covered = len(result.enforced) - len(failing)
    lines.append(
        f"bash coverage: {covered}/{len(result.enforced)} enforced scripts at 100%, "
        f"{len(result.ratcheted)} on the ratchet"
    )
    if result.unmapped:
        lines.append(
            f"note: {len(result.unmapped)} reported path(s) matched no tracked script "
            "and were ignored:"
        )
        lines.extend(f"  {path}" for path in result.unmapped[:10])
    if result.ratcheted:
        measured = [record for record in result.ratcheted if record.measured]
        unmeasured = [record for record in result.ratcheted if not record.measured]
        lines.append("ratcheted scripts (not yet enforced, lowest coverage first):")
        for record in sorted(measured, key=lambda item: item.percent):
            lines.append(
                f"  {record.percent:6.2f}%  {record.path} "
                f"({len(record.missed_lines)}/{record.total_lines} uncovered)"
            )
        # Deliberately not rendered as 0% or 100%: no suite executed these, so
        # there is no measurement to report, and printing a number would invite
        # someone to strike a script off the ratchet that is not tested at all.
        for record in unmeasured:
            lines.append(f"     n/a  {record.path} (not executed by any suite)")
    if result.failures:
        lines.append("")
        lines.append("ERROR: enforced shell scripts are not fully covered:")
        lines.extend(f"  {failure}" for failure in result.failures)
        lines.append("")
        lines.append(
            "Add BATS coverage for the lines above. If this script is new and its "
            "tests are staged work, add it to [tool.bash-coverage] ratchet in "
            "pyproject.toml and say so in the PR description."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "bashcov_output",
        type=Path,
        help="bashcov output directory (coverage/), or a .resultset.json directly.",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="Repository root used to build the tracked-script inventory.",
    )
    args = parser.parse_args(argv)

    try:
        report = find_report(args.bashcov_output)
        reported = parse_report(report)
        inventory = tracked_shell_scripts(args.root)
        ratchet = load_ratchet(args.root / "pyproject.toml")
        untraceable = untraceable_lines_by_script(args.root, inventory)
    except ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = evaluate(reported, inventory, ratchet, untraceable)
    print(format_report(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
