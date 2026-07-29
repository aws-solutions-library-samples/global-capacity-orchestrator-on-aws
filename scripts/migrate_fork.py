#!/usr/bin/env python3
"""Repoint this checkout's upstream references at your own fork.

GCO hard-codes ``awslabs/global-capacity-orchestrator-on-aws`` in CI badges,
clone instructions, issue links, package metadata, the GitHub Pages URL, and —
critically — the OIDC trust-policy subject that lets GitHub Actions assume a
deploy role. A fork that leaves those pointing upstream gets badges reporting
someone else's CI, "report an issue" links filed against upstream, and an OIDC
role that refuses its workflows.

The obvious fix, ``sed -i s/awslabs/myorg/g``, breaks the repository. The org
string also appears in links to seven unrelated AWS Labs projects and in the
``awslabs.*`` MCP server package names that ``mcp.json`` resolves at runtime;
rewriting those produces dead links and tooling that cannot start. This script
therefore classifies every occurrence and rewrites only the ones that identify
*this* repository.

Usage::

    # See what would change (default; nothing is written)
    python scripts/migrate_fork.py --repo-url https://github.com/myorg/my-gco

    # Apply it
    python scripts/migrate_fork.py --repo-url https://github.com/myorg/my-gco --apply

    # Equivalent, without a URL
    python scripts/migrate_fork.py --owner myorg --repo my-gco --apply

Only git-tracked text files are considered, so build artifacts and ignored
directories are never touched. The script refuses to run against a dirty working
tree unless ``--allow-dirty`` is passed, so ``git diff`` always shows exactly
what it did and ``git checkout .`` always undoes it.

Running it twice is a no-op: the second run finds nothing to rewrite.

See ``docs/FORKING.md`` for the surrounding checklist — the parts of a migration
that are decisions rather than string substitutions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The upstream identity this checkout ships with.
UPSTREAM_OWNER = "awslabs"
UPSTREAM_REPO = "global-capacity-orchestrator-on-aws"

#: GitHub's own constraint on owner and repository names. Validated before any
#: rewrite so a typo cannot scatter a malformed slug across the tree.
_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

#: ``owner/repo`` parsed out of any of the URL forms GitHub hands out.
_REPO_URL_RE = re.compile(
    r"^(?:https?://(?:www\.)?github\.com/|git@github\.com:|github\.com/)"
    r"(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)

#: Files whose *content defines or documents the upstream identity*. Rewriting
#: them would erase this script's own reference points and make the migration
#: guide describe a migration away from the fork it already is.
SELF_REFERENTIAL_PATHS = frozenset(
    {
        "scripts/migrate_fork.py",
        "tests/test_migrate_fork.py",
        "docs/FORKING.md",
    }
)

#: Suffixes that are either not text or are recordings whose correct fix is
#: re-recording, not editing captured terminal output.
SKIPPED_SUFFIXES = frozenset(
    {
        ".cast",
        ".gif",
        ".png",
        ".jpg",
        ".jpeg",
        ".svg",
        ".ico",
        ".gz",
        ".zip",
        ".whl",
        ".pyc",
        ".woff",
        ".woff2",
    }
)


@dataclass(frozen=True)
class Rule:
    """One classification rule applied to a single line of a tracked file."""

    name: str
    pattern: re.Pattern[str]
    #: ``None`` marks a reference that must survive untouched.
    replacement: str | None
    why: str


def _build_rules(owner: str, repo: str) -> tuple[Rule, ...]:
    """Return the ordered rule table for a target ``owner``/``repo``.

    Order matters: at any position the earliest-starting, and among ties the
    first-listed, rule wins. Preservation rules come first so a reference to
    another AWS Labs project can never be consumed by a broader rewrite, and the
    rewrite rules run most-specific first so ``git@github.com:owner/repo`` is not
    partially matched by the bare-slug rule.
    """
    up_owner = re.escape(UPSTREAM_OWNER)
    up_repo = re.escape(UPSTREAM_REPO)
    # The Pages host appears both literally and percent-encoded (the shields.io
    # coverage badge embeds it as a query parameter). Both spellings must be
    # excluded here or the package-name rule swallows them.
    pages_guard = rf"(?!github\.io(?:/|%2[Ff]){up_repo})"

    return (
        Rule(
            name="other-awslabs-project",
            pattern=re.compile(rf"github\.com/{up_owner}/(?!{up_repo})[\w.-]+"),
            replacement=None,
            why="link to a different AWS Labs project",
        ),
        Rule(
            name="awslabs-package-name",
            # ``*`` is accepted so prose referring to the namespace as a glob
            # ("the awslabs.* MCP servers") classifies as a package reference
            # rather than falling through unrecognized.
            pattern=re.compile(rf"{up_owner}\.{pages_guard}[a-z0-9_*-]+[a-z0-9_.*-]*"),
            replacement=None,
            why="published package name (for example an awslabs.* MCP server)",
        ),
        Rule(
            name="clone-url-ssh",
            pattern=re.compile(rf"git@github\.com:{up_owner}/{up_repo}"),
            replacement=f"git@github.com:{owner}/{repo}",
            why="SSH clone URL",
        ),
        Rule(
            name="pages-url-encoded",
            pattern=re.compile(rf"{up_owner}\.github\.io%2F{up_repo}"),
            replacement=f"{owner}.github.io%2F{repo}",
            why="percent-encoded GitHub Pages URL (shields.io badge endpoint)",
        ),
        Rule(
            name="pages-url",
            pattern=re.compile(rf"{up_owner}\.github\.io/{up_repo}"),
            replacement=f"{owner}.github.io/{repo}",
            why="GitHub Pages URL",
        ),
        Rule(
            name="repo-url",
            pattern=re.compile(rf"github\.com/{up_owner}/{up_repo}"),
            replacement=f"github.com/{owner}/{repo}",
            why="repository URL (badges, issue links, tree/blob links)",
        ),
        Rule(
            name="repo-slug",
            pattern=re.compile(rf"(?<![\w/.-]){up_owner}/{up_repo}(?![\w-])"),
            replacement=f"{owner}/{repo}",
            why="bare owner/repo slug (OIDC trust-policy subject, CI docs)",
        ),
        Rule(
            name="repo-name",
            # A preceding "/" is allowed so filesystem placeholders such as
            # "/path/to/global-capacity-orchestrator-on-aws" (the MCP server
            # setup instructions) are updated too. URL forms cannot be caught
            # here by mistake: the rules above start earlier in the line and
            # consume the whole reference first. A preceding word character,
            # "-", or "." still blocks a match, so a differently-prefixed
            # directory like "PROD-global-capacity-orchestrator-on-aws" is left
            # alone.
            pattern=re.compile(rf"(?<![\w.-]){up_repo}(?![\w-])"),
            replacement=repo,
            why="bare repository name (clone directory, package metadata)",
        ),
    )


@dataclass
class Occurrence:
    """A single classified match."""

    path: str
    lineno: int
    rule: Rule
    matched: str

    @property
    def rewritten(self) -> str | None:
        return self.rule.replacement


@dataclass
class Report:
    """Everything one run found."""

    rewrites: list[Occurrence] = field(default_factory=list)
    preserved: list[Occurrence] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    skipped_binary: list[str] = field(default_factory=list)
    skipped_self: list[str] = field(default_factory=list)
    follow_ups: list[tuple[str, str]] = field(default_factory=list)


def parse_target(args: argparse.Namespace) -> tuple[str, str]:
    """Resolve and validate the destination ``owner``/``repo``."""
    if args.repo_url:
        match = _REPO_URL_RE.match(args.repo_url.strip())
        if not match:
            raise SystemExit(
                f"Could not parse --repo-url {args.repo_url!r}. Expected something like "
                "https://github.com/myorg/my-gco or git@github.com:myorg/my-gco.git"
            )
        owner, repo = match.group("owner"), match.group("repo")
    else:
        owner, repo = args.owner, args.repo

    if not owner or not repo:
        raise SystemExit("Provide --repo-url, or both --owner and --repo.")
    if not _OWNER_RE.match(owner):
        raise SystemExit(f"Invalid GitHub owner name: {owner!r}")
    if not _REPO_NAME_RE.match(repo):
        raise SystemExit(f"Invalid GitHub repository name: {repo!r}")
    if (owner, repo) == (UPSTREAM_OWNER, UPSTREAM_REPO):
        raise SystemExit(f"Target is the upstream repository ({owner}/{repo}); nothing to migrate.")
    return owner, repo


def _git(*argv: str) -> str:
    """Run a read-only git command in the repository root."""
    result = subprocess.run(
        ["git", *argv],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def tracked_files() -> list[Path]:
    """Every file git tracks, so ignored build output is never rewritten."""
    return [Path(name) for name in _git("ls-files", "-z").split("\0") if name]


def working_tree_is_dirty() -> bool:
    """Whether the checkout has uncommitted changes."""
    return bool(_git("status", "--porcelain").strip())


def classify_line(line: str, rules: Iterable[Rule]) -> Iterator[tuple[Rule, re.Match[str]]]:
    """Yield each classified match in ``line``, left to right, without overlap."""
    rules = tuple(rules)
    position = 0
    while position < len(line):
        best: tuple[Rule, re.Match[str]] | None = None
        for rule in rules:
            match = rule.pattern.search(line, position)
            if match is None:
                continue
            if best is None or match.start() < best[1].start():
                best = (rule, match)
        if best is None:
            return
        yield best
        position = max(best[1].end(), position + 1)


def rewrite_text(text: str, path: str, rules: Iterable[Rule], report: Report) -> str:
    """Return ``text`` with every rewrite rule applied, recording each match."""
    rules = tuple(rules)
    out_lines: list[str] = []
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        stripped = line.rstrip("\r\n")
        ending = line[len(stripped) :]
        rebuilt: list[str] = []
        cursor = 0
        for rule, match in classify_line(stripped, rules):
            occurrence = Occurrence(path, lineno, rule, match.group(0))
            if rule.replacement is None:
                report.preserved.append(occurrence)
                continue
            report.rewrites.append(occurrence)
            rebuilt.append(stripped[cursor : match.start()])
            rebuilt.append(rule.replacement)
            cursor = match.end()
        rebuilt.append(stripped[cursor:])
        out_lines.append("".join(rebuilt) + ending)
    return "".join(out_lines)


def _detect_follow_ups() -> list[tuple[str, str]]:
    """Decisions a string rewrite cannot make for you.

    Each is detected rather than assumed, so the checklist reflects this
    checkout instead of listing items that may not apply.
    """
    follow_ups: list[tuple[str, str]] = []

    codeowners = REPO_ROOT / ".github" / "CODEOWNERS"
    if codeowners.is_file():
        owners = sorted(
            set(
                re.findall(
                    r"@[A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9._-]+)?",
                    codeowners.read_text(encoding="utf-8"),
                )
            )
        )
        if owners:
            named = ", ".join(owners)
            follow_ups.append(
                (
                    ".github/CODEOWNERS",
                    f"Assigns review to {named}, which will not resolve in your fork "
                    "(a personal handle without access, or a team that does not exist "
                    "in your organization). Every pull request then requests review "
                    "from a missing owner. Replace with your own owners or delete the "
                    "file.",
                )
            )

    app_py = REPO_ROOT / "app.py"
    if app_py.is_file():
        text = app_py.read_text(encoding="utf-8")
        match = re.search(r'SOLUTION_ID\s*=\s*"([^"]+)"', text)
        if match:
            follow_ups.append(
                (
                    "app.py",
                    f"Sets SOLUTION_ID = {match.group(1)!r}, the AWS Solutions identifier "
                    "for the published guidance, on the global stack description. Decide "
                    "whether your fork should keep claiming it; a divergent fork usually "
                    "should not.",
                )
            )

    security = REPO_ROOT / ".github" / "SECURITY.md"
    if security.is_file():
        follow_ups.append(
            (
                ".github/SECURITY.md",
                "Describes AWS's vulnerability disclosure process. Keep it for the "
                "inherited code, but add how reporters should contact you about "
                "fork-specific issues.",
            )
        )

    oidc = REPO_ROOT / ".github" / "oidc_provider" / "cdk.json"
    if oidc.is_file():
        follow_ups.append(
            (
                ".github/oidc_provider/",
                "The github_repo context value is the OIDC trust-policy subject. This "
                "script updates the string, but the change only takes effect once you "
                "redeploy the OIDC stack in your AWS account — until then your workflows "
                "cannot assume the deploy role.",
            )
        )

    for name in ("LICENSE", "NOTICE"):
        if (REPO_ROOT / name).is_file():
            follow_ups.append(
                (
                    name,
                    "Upstream attribution. Left untouched deliberately; keep it, and add "
                    "your own copyright rather than replacing it.",
                )
            )

    if (REPO_ROOT / ".github" / "workflows" / "pages.yml").is_file():
        follow_ups.append(
            (
                ".github/workflows/pages.yml",
                "Enable GitHub Pages on your fork (Settings > Pages, source: GitHub "
                "Actions) or the coverage badge will 404 even with the URL updated.",
            )
        )

    recordings = sorted(
        str(path)
        for path in tracked_files()
        if path.suffix == ".cast" and UPSTREAM_REPO in _safe_read(path)
    )
    if recordings:
        follow_ups.append(
            (
                ", ".join(recordings),
                "Terminal recordings that captured the upstream clone URL. Editing "
                "captured output would desynchronize the recording from reality; "
                "re-record with demo/record_demo.sh instead.",
            )
        )

    return follow_ups


def _safe_read(path: Path) -> str:
    """Read a tracked file as text, returning ``""`` when it is not text."""
    try:
        return (REPO_ROOT / path).read_text(encoding="utf-8")
    except UnicodeDecodeError, OSError:
        return ""


def run(owner: str, repo: str, *, apply: bool) -> Report:
    """Classify, and optionally rewrite, every tracked file."""
    rules = _build_rules(owner, repo)
    report = Report()

    for path in tracked_files():
        name = path.as_posix()
        if name in SELF_REFERENTIAL_PATHS:
            report.skipped_self.append(name)
            continue
        if path.suffix.lower() in SKIPPED_SUFFIXES:
            if UPSTREAM_REPO in _safe_read(path) or UPSTREAM_OWNER in _safe_read(path):
                report.skipped_binary.append(name)
            continue

        original = _safe_read(path)
        if not original or (UPSTREAM_OWNER not in original and UPSTREAM_REPO not in original):
            continue

        before = len(report.rewrites)
        updated = rewrite_text(original, name, rules, report)
        if len(report.rewrites) == before:
            continue

        report.changed_files.append(name)
        if apply and updated != original:
            (REPO_ROOT / path).write_text(updated, encoding="utf-8")

    report.follow_ups = _detect_follow_ups()
    return report


def print_report(report: Report, owner: str, repo: str, *, apply: bool) -> None:
    """Render the report for a human reader."""
    heading = "APPLIED" if apply else "DRY RUN — nothing was written"
    print(f"{heading}")
    print(f"Target: {UPSTREAM_OWNER}/{UPSTREAM_REPO} -> {owner}/{repo}\n")

    if report.rewrites:
        by_file: dict[str, list[Occurrence]] = {}
        for occurrence in report.rewrites:
            by_file.setdefault(occurrence.path, []).append(occurrence)
        verb = "Rewrote" if apply else "Would rewrite"
        print(f"{verb} {len(report.rewrites)} reference(s) in {len(by_file)} file(s):")
        for path in sorted(by_file):
            print(f"\n  {path}")
            for occurrence in by_file[path]:
                print(
                    f"    line {occurrence.lineno}: {occurrence.matched}"
                    f"  ->  {occurrence.rewritten}"
                )
    else:
        print("No references needed rewriting.")

    if report.preserved:
        distinct: dict[str, list[Occurrence]] = {}
        for occurrence in report.preserved:
            distinct.setdefault(occurrence.matched, []).append(occurrence)
        print(
            f"\nPreserved {len(report.preserved)} reference(s) that are not this "
            f"repository ({len(distinct)} distinct):"
        )
        for matched in sorted(distinct):
            occurrences = distinct[matched]
            print(f"    {matched}  ({occurrences[0].rule.why}, x{len(occurrences)})")

    if report.skipped_self:
        print("\nSkipped (define or document the upstream identity):")
        for name in sorted(report.skipped_self):
            print(f"    {name}")

    if report.skipped_binary:
        print("\nSkipped (not text, or a recording to regenerate):")
        for name in sorted(report.skipped_binary):
            print(f"    {name}")

    if report.follow_ups:
        print(f"\nManual follow-ups ({len(report.follow_ups)}) — see docs/FORKING.md:")
        for target, note in report.follow_ups:
            print(f"\n  [ ] {target}")
            for line in _wrap(note, 74):
                print(f"      {line}")

    if not apply and report.rewrites:
        print("\nRe-run with --apply to write these changes.")


def _wrap(text: str, width: int) -> list[str]:
    """Wrap ``text`` without importing textwrap for one call site."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) > width and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _as_json(report: Report, owner: str, repo: str, *, apply: bool) -> str:
    payload = {
        "applied": apply,
        "target": {"owner": owner, "repo": repo},
        "upstream": {"owner": UPSTREAM_OWNER, "repo": UPSTREAM_REPO},
        "rewrites": [
            {
                "path": occurrence.path,
                "line": occurrence.lineno,
                "rule": occurrence.rule.name,
                "from": occurrence.matched,
                "to": occurrence.rewritten,
            }
            for occurrence in report.rewrites
        ],
        "preserved": [
            {
                "path": occurrence.path,
                "line": occurrence.lineno,
                "rule": occurrence.rule.name,
                "matched": occurrence.matched,
            }
            for occurrence in report.preserved
        ],
        "changed_files": sorted(report.changed_files),
        "skipped": sorted(report.skipped_self + report.skipped_binary),
        "follow_ups": [{"target": t, "note": n} for t, n in report.follow_ups],
    }
    return json.dumps(payload, indent=2, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repoint upstream GCO references at your own fork.",
        epilog="Dry-run by default. See docs/FORKING.md for the full checklist.",
    )
    target = parser.add_argument_group("destination")
    target.add_argument("--repo-url", help="Fork URL, e.g. https://github.com/myorg/my-gco")
    target.add_argument("--owner", help="Fork owner (user or organization)")
    target.add_argument("--repo", help="Fork repository name")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes (default: report only).",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Permit --apply with uncommitted changes present.",
    )
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON.")
    args = parser.parse_args(argv)

    owner, repo = parse_target(args)

    if args.apply and not args.allow_dirty and working_tree_is_dirty():
        print(
            "Refusing to rewrite a dirty working tree: commit or stash first so "
            "`git diff` shows only this script's changes and `git checkout .` "
            "reverts them. Override with --allow-dirty.",
            file=sys.stderr,
        )
        return 2

    report = run(owner, repo, apply=args.apply)

    if args.json:
        print(_as_json(report, owner, repo, apply=args.apply))
    else:
        print_report(report, owner, repo, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
