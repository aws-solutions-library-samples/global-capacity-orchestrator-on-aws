"""Enforce the shell-script coverage floor from a bashcov/SimpleCov report.

The ``unit:bats:shell`` job runs the BATS suite under ``bashcov``, which traces
Bash through ``BASH_XTRACEFD`` and writes a SimpleCov resultset describing every
*relevant* line of every shell file it saw and how many times each ran. This
script turns that resultset into a pass/fail gate.

Deciding which lines of a shell script are even executable is the hard part of
Bash coverage — here-documents, ``case`` arms, line continuations and function
headers all have to be classified — so that judgement is deliberately left to
bashcov's lexer rather than re-implemented here, with two corrections that
come from measuring what ``set -x`` actually prints:

**Lines Bash never traces.** The lexer works from the text alone and marks two
shapes executable that the tracer never reports, so no test could ever cover
them: a compound-command terminator that carries only redirections (``done <<<
"$rows"``, ``} > "$report"`` — the redirection belongs to the loop or group,
and ``set -x`` prints simple commands, not the loop) and a ``case`` arm with no
body (``*/*) ;;``). ``untraceable_lines()`` recognises exactly those two shapes
and ``evaluate()`` leaves them out of the count, the way SimpleCov leaves out a
comment. Anything that carries a command — a pipe into ``sed`` after ``}``, a
``:`` in the arm, a process substitution — is still measured.

**Statements that span lines.** Bash reports one line per statement, and which
physical line it picks depends on the shape: the first line of ``python3 -c
"..."`` with a multi-line string, the *second* line of a plain backslash chain
(``kill_it \\ / one \\ / two`` reports line 2) or of a backgrounded one, and the
*last* line of ``VAR=$(...)``, of ``VAR="multi\\nline"``, and of the ``cat
<<'EOF' ... EOF )`` heredoc-in-substitution the client examples build their
payloads with. The lexer propagates the first line's count across some of these
shapes and not others (a ``\\"`` inside the string or a ``||`` on the last line
of a chain defeats its patterns), which left dozens of lines permanently at
zero. ``statement_spans()`` scans each script for statements that continue
across lines — a trailing backslash, an unclosed ``(``/``$(``, an open quote,
a here-document body — and ``evaluate()`` folds every span onto its first line
with the highest count seen on any of its lines. A statement is covered when
Bash reported it, wherever it reported it. A chain is split where a list
operator (``||``, ``&&``, ``|``) starts a new command at the top level, because
Bash does report those elements on their own lines; the fallback in
``aws ... 2>&1 || echo "may already exist"`` therefore stays a separately
measured statement.

What this script owns beyond that is everything SimpleCov cannot know about
*this* repository:

**Path shape.** bashcov reports absolute paths (``/home/runner/work/.../demo/
lib_demo.sh``), while the inventory and every error message use
repository-relative ones. Reported paths are mapped back onto the tracked
script they refer to by longest path suffix, falling back to a unique basename,
and hits from every path that maps to the same script are merged — so a script
exercised by several suites is credited with all of them.

That merging also covers copies of a script, which matters because a BATS
suite may ``cp`` the script under test into ``$BATS_TEST_TMPDIR`` and run it
from an isolated fake repository. It does not rescue such a suite on its own,
though: SimpleCov reads each file when it renders the report, and by then BATS
has deleted its temporary directories, so the copies are dropped before this
script ever sees them. A suite has to run the tracked file in place for its
hits to count (the recorders take a repository-root override for exactly
this). The merging is what keeps a *surviving* copy, or the same script seen
under two different absolute prefixes, from being counted as two half-covered
files.

**The floor.** Every tracked script must be fully covered. The climb to 100%
was staged through a shrink-only list of not-yet-covered scripts
(``[tool.bash-coverage] ratchet`` in ``pyproject.toml``); it emptied and was
deleted, and this script reads no exclusion list of any kind — a new script is
covered, not listed. ``tests/test_check_bash_coverage.py`` keeps the list from
coming back.

The gate fails closed: a tracked script absent from the report entirely is an
error, not a pass, because that is what a silently mis-scoped bashcov run or a
suite that never executes its subject looks like.

Usage::

    python3 .github/scripts/check_bash_coverage.py coverage/
    python3 .github/scripts/check_bash_coverage.py coverage/.resultset.json

Exit codes::

    0  every tracked script is fully covered
    1  at least one tracked script has uncovered lines or is missing
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


_HEREDOC = re.compile(r"<<-?\s*(?P<quote>['\"]?)(?P<tag>\w+)(?P=quote)")
_LIST_OPERATOR = re.compile(r"\|\||&&|\|(?!\|)")


@dataclass(frozen=True)
class _Scan:
    """Lexer state carried from one physical line to the next.

    ``parens`` is the stack of unclosed parentheses; ``True`` marks one that
    keeps a statement open across lines — ``$(``, an array's ``=(``, a process
    substitution's ``<(``/``>(``, an arithmetic ``((`` — while ``False`` marks a
    subshell ``(``, whose inner commands Bash reports on their own lines and
    which must therefore not be folded.
    """

    parens: tuple[bool, ...] = ()
    quote: str | None = None  # "'" or '"' while inside a quoted string
    heredoc: str | None = None  # terminator of the here-document being read
    heredoc_strip: bool = False  # <<- : leading tabs are stripped before comparing
    continued: bool = False  # the line ended with an escaping backslash

    @property
    def depth(self) -> int:
        return sum(1 for spanning in self.parens if spanning)

    def open(self) -> bool:
        """Whether the statement is still open at the end of a line."""
        return (
            self.depth > 0 or self.quote is not None or self.heredoc is not None or self.continued
        )


def _scan_line(line: str, state: _Scan) -> tuple[_Scan, bool]:
    """Advance ``state`` over one physical line.

    Returns the new state and whether a list operator (``||``, ``&&``, ``|``)
    appeared at the top level of this line — i.e. outside quotes and outside
    any parenthesis — which is where Bash starts a new, separately reported
    command inside a backslash chain.
    """
    parens = list(state.parens)
    quote, heredoc, heredoc_strip = state.quote, state.heredoc, state.heredoc_strip
    if heredoc is not None:
        candidate = line.lstrip("\t") if heredoc_strip else line
        if candidate == heredoc:
            heredoc = None
        return _Scan(tuple(parens), quote, heredoc, heredoc_strip, False), False

    pending_heredoc: tuple[str, bool] | None = None
    list_operator = False
    continued = False
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\":
                if index == length - 1:
                    continued = True  # a backslash-newline inside "..." continues the string
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        # Unquoted.
        if char == "\\":
            if index == length - 1:
                continued = True
            index += 2
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t;("):
            break  # comment to end of line
        if char in "'\"":
            quote = char
            index += 1
            continue
        if char == "(":
            parens.append(index > 0 and line[index - 1] in "$=<>(")
        elif char == ")":
            if parens:
                parens.pop()  # a `pattern)` case arm has no opener: nothing to pop
        elif char == "<" and pending_heredoc is None:
            match = _HEREDOC.match(line, index)
            if match:
                pending_heredoc = (match.group("tag"), line[index : index + 3] == "<<-")
                index = match.end()
                continue
        elif char in "|&" and not parens:
            match = _LIST_OPERATOR.match(line, index)
            if match:
                list_operator = True
                index = match.end()
                continue
        index += 1

    if pending_heredoc is not None:
        heredoc, heredoc_strip = pending_heredoc
    return _Scan(tuple(parens), quote, heredoc, heredoc_strip, continued), list_operator


def statement_spans(source: str) -> list[tuple[int, int]]:
    """Return ``(first, last)`` line pairs for statements spanning several lines.

    A statement continues onto the next line while a parenthesis, quote or
    here-document is open or the line ends with a backslash. Inside a
    backslash chain a line that starts a new top-level list element (``||``,
    ``&&``, ``|``) begins a new span, since Bash reports that command on its
    own line. Single-line statements are not returned.
    """
    spans: list[tuple[int, int]] = []
    state = _Scan()
    start: int | None = None
    for number, line in enumerate(source.splitlines(), start=1):
        was_open = state.open()
        chain_only = was_open and state.depth == 0 and state.quote is None and state.heredoc is None
        state, list_operator = _scan_line(line, state)
        if chain_only and list_operator and start is not None:
            # `cmd \` / `  arg \` / `  arg || fallback`: the fallback is its own statement.
            if number - 1 > start:
                spans.append((start, number - 1))
            start = number
        elif not was_open:
            start = number
        if not state.open():
            if start is not None and number > start:
                spans.append((start, number))
            start = None
    if start is not None and state.open():
        spans.append((start, len(source.splitlines())))
    return spans


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
    """Outcome of a gate evaluation: one record per tracked script."""

    scripts: list[ScriptCoverage]
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


def fold_spans(hits: dict[int, int], spans: list[tuple[int, int]]) -> dict[int, int]:
    """Collapse each multi-line statement onto its first line.

    Every line of a span that the report lists is replaced by the span's first
    line carrying the highest count seen anywhere in the span, so a statement
    Bash reported on its second or last line counts once, as covered. A span
    none of whose lines the report mentions stays absent (bashcov judged it
    non-executable, e.g. a multi-line comment block would never be a span).
    """
    folded = dict(hits)
    for first, last in spans:
        members = [line for line in range(first, last + 1) if line in folded]
        if not members:
            continue
        best = max(folded[line] for line in members)
        for line in members:
            del folded[line]
        folded[first] = best
    return folded


def evaluate(
    reported: dict[str, dict[int, int]],
    inventory: list[str],
    untraceable: dict[str, set[int]] | None = None,
    spans: dict[str, list[tuple[int, int]]] | None = None,
) -> Result:
    """Merge reported coverage onto the inventory and apply the floor to all of it.

    ``untraceable`` maps a tracked script to the line numbers that
    :func:`untraceable_lines` found in it; those lines are dropped from the
    script's count whatever the report says about them. ``spans`` maps a
    tracked script to its :func:`statement_spans`, each folded onto its first
    line by :func:`fold_spans`.
    """
    merged: dict[str, ScriptCoverage] = {path: ScriptCoverage(path=path) for path in inventory}
    unmapped: list[str] = []
    untraceable = untraceable or {}
    spans = spans or {}

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

    for path, record in merged.items():
        if record.hits and path in spans:
            record.hits = fold_spans(record.hits, spans[path])

    scripts = [record for _, record in sorted(merged.items())]

    failures: list[str] = []
    for record in scripts:
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
    return Result(scripts=scripts, failures=failures, unmapped=unmapped)


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


def classify_scripts(
    root: Path, inventory: list[str]
) -> tuple[dict[str, set[int]], dict[str, list[tuple[int, int]]]]:
    """Run :func:`untraceable_lines` and :func:`statement_spans` over the inventory.

    Returns the two per-script maps :func:`evaluate` takes, each holding only
    the scripts that have something to report.
    """
    untraceable: dict[str, set[int]] = {}
    spans: dict[str, list[tuple[int, int]]] = {}
    for path in inventory:
        try:
            source = (root / path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ReportError(f"could not read tracked script {path}: {exc}") from exc
        lines = untraceable_lines(source)
        if lines:
            untraceable[path] = lines
        found = statement_spans(source)
        if found:
            spans[path] = found
    return untraceable, spans


def format_report(result: Result) -> str:
    """Render the human-facing summary printed by ``main``."""
    lines: list[str] = []
    failing = [record for record in result.scripts if record.missed_lines or not record.measured]
    covered = len(result.scripts) - len(failing)
    lines.append(f"bash coverage: {covered}/{len(result.scripts)} tracked scripts at 100%")
    if result.unmapped:
        lines.append(
            f"note: {len(result.unmapped)} reported path(s) matched no tracked script "
            "and were ignored:"
        )
        lines.extend(f"  {path}" for path in result.unmapped[:10])
    if result.failures:
        lines.append("")
        lines.append("ERROR: shell scripts are not fully covered:")
        lines.extend(f"  {failure}" for failure in result.failures)
        lines.append("")
        lines.append(
            "Add BATS coverage for the lines above. Every tracked *.sh file is held "
            "to 100%: a new script ships with a suite that executes it, and a script "
            "absent from the report needs its suite to run the tracked file in place "
            "rather than a copy."
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
        untraceable, spans = classify_scripts(args.root, inventory)
    except ReportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    result = evaluate(reported, inventory, untraceable, spans)
    print(format_report(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
