"""Tests for ``scripts/migrate_fork.py``, the fork-migration assistant.

The headline case is ``test_every_upstream_reference_is_classified``: it walks
every git-tracked file and asserts that each occurrence of the upstream
organization or repository name is claimed by exactly one rule. That is what
makes the tool safe to trust. A new reference in an unanticipated shape — a
``raw.githubusercontent.com`` URL, a percent-encoded Pages link, a new package
name — fails this test until a rule classifies it, rather than silently being
left pointing upstream (or, worse, rewritten when it should not be).

The distinction the rules encode is not cosmetic. Beyond the references that
identify *this* repository, the tree carries references that must survive any
migration untouched: links to sibling projects under the upstream org, other
projects' GitHub Pages hosts, and the ``awslabs.*`` MCP server package names
that ``mcp.json`` resolves at runtime — a relic of the project's original org
that still names real published packages. A blanket find-and-replace produces
dead documentation links and MCP servers that cannot start, so preservation is
tested as explicitly as rewriting.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = PROJECT_ROOT / "scripts" / "migrate_fork.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("gco_migrate_fork", SCRIPT)
    assert spec and spec.loader, f"could not load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    # Register before executing: the script's dataclasses use postponed
    # annotations, and ``dataclasses`` resolves them through
    # ``sys.modules[cls.__module__]``, which fails if the module is absent.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migrate() -> Any:
    return _load_module()


@pytest.fixture(scope="module")
def rules(migrate: Any) -> tuple[Any, ...]:
    return migrate._build_rules("acme-labs", "gco-fork")


def _tracked_text_files(migrate: Any) -> list[Path]:
    names = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split("\0")
    files: list[Path] = []
    for name in names:
        if not name:
            continue
        path = Path(name)
        if path.as_posix() in migrate.SELF_REFERENTIAL_PATHS:
            continue
        if path.suffix.lower() in migrate.SKIPPED_SUFFIXES:
            continue
        files.append(path)
    return files


def test_every_upstream_reference_is_classified(migrate: Any, rules: tuple[Any, ...]) -> None:
    """No occurrence of the upstream org or repo name escapes classification.

    Every character span containing the upstream org or repository name must be
    covered by a rule match, so the tool can never encounter a reference it has
    no opinion about.
    """
    unclassified: list[str] = []
    scanned = 0

    for path in _tracked_text_files(migrate):
        try:
            text = (PROJECT_ROOT / path).read_text(encoding="utf-8")
        except UnicodeDecodeError, OSError:
            continue
        if migrate.UPSTREAM_OWNER not in text and migrate.UPSTREAM_REPO not in text:
            continue
        scanned += 1

        for lineno, line in enumerate(text.splitlines(), 1):
            covered: set[int] = set()
            for _rule, match in migrate.classify_line(line, rules):
                covered.update(range(match.start(), match.end()))

            for token in (migrate.UPSTREAM_OWNER, migrate.UPSTREAM_REPO):
                start = line.find(token)
                while start != -1:
                    if start not in covered:
                        unclassified.append(f"{path}:{lineno}: {token!r} in: {line.strip()[:110]}")
                        break
                    start = line.find(token, start + 1)

    assert scanned > 15, f"sanity floor: only {scanned} files mentioned upstream"
    assert not unclassified, (
        "these upstream references are not classified by any rule in "
        "scripts/migrate_fork.py — add a rule (or a preservation rule) so the "
        "migration tool handles them deliberately:\n  " + "\n  ".join(unclassified)
    )


def test_other_upstream_org_projects_are_preserved(migrate: Any, rules: tuple[Any, ...]) -> None:
    """Links to different projects under the upstream org must never be rewritten."""
    samples = (
        "see https://github.com/aws-solutions-library-samples/guidance-for-x for details",
        "https://github.com/aws-solutions-library-samples/another-guidance-sample",
        "https://aws-solutions-library-samples.github.io/some-other-guidance/docs/",
    )
    for line in samples:
        report = migrate.Report()
        assert migrate.rewrite_text(line, "sample", rules, report) == line
        assert report.rewrites == [], f"rewrote a third-party reference: {line}"
        assert report.preserved, f"failed to classify as preserved: {line}"


def test_original_org_references_survive_untouched(migrate: Any, rules: tuple[Any, ...]) -> None:
    """References from the project's original ``awslabs`` home never rewrite.

    Since the August 2026 org move these no longer contain the upstream
    identity, so no rule fires at all — which is itself the guarantee that
    ``awslabs.*`` MCP package names and AWS Labs project links keep working.
    """
    samples = (
        "see https://github.com/awslabs/aws-sigv4-proxy for details",
        "https://github.com/awslabs/amazon-eks-ami",
        "https://awslabs.github.io/ai-on-eks/docs/blueprints/",
        "https://awslabs.github.io/mcp/servers/eks-mcp-server/",
        '"args": ["awslabs.aws-documentation-mcp-server@latest"]',
    )
    for line in samples:
        report = migrate.Report()
        assert migrate.rewrite_text(line, "sample", rules, report) == line
        assert report.rewrites == [], f"rewrote an original-org reference: {line}"


def test_mcp_package_names_are_preserved(migrate: Any, rules: tuple[Any, ...]) -> None:
    """Package names resolved at runtime must not change.

    An ``aws-solutions-library-samples.*`` name contains the upstream org, so
    it must be claimed by the preservation rule; ``awslabs.*`` names no longer
    match any rule and survive by construction (covered above).
    """
    line = '"args": ["aws-solutions-library-samples.example-mcp-server@latest"]'
    report = migrate.Report()
    assert migrate.rewrite_text(line, "mcp.json", rules, report) == line
    assert report.rewrites == []
    assert report.preserved, "upstream-org package name should classify as preserved"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (
            "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/issues",
            "https://github.com/acme-labs/gco-fork/issues",
        ),
        (
            "repos/aws-solutions-library-samples/global-capacity-orchestrator-on-aws/actions/oidc/customization/sub",
            "repos/acme-labs/gco-fork/actions/oidc/customization/sub",
        ),
        (
            "git clone git@github.com:aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git",
            "git clone git@github.com:acme-labs/gco-fork.git",
        ),
        (
            "https://aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws/",
            "https://acme-labs.github.io/gco-fork/",
        ),
        # The shields.io coverage badge embeds the Pages URL percent-encoded; a
        # plain URL rewrite misses it and the badge keeps reporting upstream.
        (
            "url=https%3A%2F%2Faws-solutions-library-samples.github.io%2Fglobal-capacity-orchestrator-on-aws%2Fx.json",
            "url=https%3A%2F%2Facme-labs.github.io%2Fgco-fork%2Fx.json",
        ),
        # A percent-encoded Pages URL whose owner is NOT upstream (an org move
        # or a second migration) must still have its repo-name segment
        # rewritten — pages-url-encoded cannot match a foreign owner's host,
        # so the repo-name rule claims the segment after "%2F".
        (
            "url=https%3A%2F%2Fsomeone-else.github.io%2Fglobal-capacity-orchestrator-on-aws%2Fx.json",
            "url=https%3A%2F%2Fsomeone-else.github.io%2Fgco-fork%2Fx.json",
        ),
        # The README's one-click MCP install buttons (Kiro, VS Code) embed the
        # git URL percent-encoded in their deep-link config parameter.
        (
            "config=%7B%22--from%22%2C%22git%2Bhttps%3A%2F%2Fgithub.com%2Faws-solutions-library-samples%2Fglobal-capacity-orchestrator-on-aws.git%40v7.6.1%22%7D",
            "config=%7B%22--from%22%2C%22git%2Bhttps%3A%2F%2Fgithub.com%2Facme-labs%2Fgco-fork.git%40v7.6.1%22%7D",
        ),
        # The README's latest-release badge embeds the slug in a shields.io
        # path (not a github.com URL). Both the owner and the repo segment
        # must move, or a fork's badge keeps reporting upstream's releases.
        (
            "https://img.shields.io/github/v/release/aws-solutions-library-samples/global-capacity-orchestrator-on-aws?sort=semver",
            "https://img.shields.io/github/v/release/acme-labs/gco-fork?sort=semver",
        ),
        # The OIDC trust-policy subject is a bare owner/repo slug.
        (
            '"github_repo": "aws-solutions-library-samples/global-capacity-orchestrator-on-aws",',
            '"github_repo": "acme-labs/gco-fork",',
        ),
        (
            '"github_subject_prefix": "repo:aws-solutions-library-samples@109766924/global-capacity-orchestrator-on-aws@1219314144",',
            '"github_subject_prefix": "REPLACE_WITH_GITHUB_OIDC_SUBJECT_PREFIX",',
        ),
        (
            "cd global-capacity-orchestrator-on-aws",
            "cd gco-fork",
        ),
        (
            '"cwd": "/path/to/global-capacity-orchestrator-on-aws",',
            '"cwd": "/path/to/gco-fork",',
        ),
    ],
)
def test_rewrites(migrate: Any, rules: tuple[Any, ...], before: str, after: str) -> None:
    report = migrate.Report()
    assert migrate.rewrite_text(before, "sample", rules, report) == after


def test_differently_prefixed_directory_is_left_alone(migrate: Any, rules: tuple[Any, ...]) -> None:
    """A directory that merely ends with the repo name is not the repo."""
    line = "cd /Users/me/PROD-global-capacity-orchestrator-on-aws"
    report = migrate.Report()
    assert migrate.rewrite_text(line, "sample", rules, report) == line


def test_rewriting_is_idempotent(migrate: Any, rules: tuple[Any, ...]) -> None:
    """Running the tool twice changes nothing the second time."""
    original = (
        "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws\n"
        "git@github.com:aws-solutions-library-samples/global-capacity-orchestrator-on-aws\n"
        "aws-solutions-library-samples.github.io/global-capacity-orchestrator-on-aws\n"
        "awslabs.aws-pricing-mcp-server\n"
    )
    once = migrate.rewrite_text(original, "sample", rules, migrate.Report())
    twice = migrate.rewrite_text(once, "sample", rules, migrate.Report())
    assert once == twice
    assert "awslabs.aws-pricing-mcp-server" in twice


def test_self_referential_files_are_excluded(migrate: Any) -> None:
    """The tool, its tests, and the guide keep their upstream references.

    They define or explain the upstream identity; rewriting them would erase the
    tool's own reference points.
    """
    assert "scripts/migrate_fork.py" in migrate.SELF_REFERENTIAL_PATHS
    assert "tests/test_migrate_fork.py" in migrate.SELF_REFERENTIAL_PATHS
    assert "docs/FORKING.md" in migrate.SELF_REFERENTIAL_PATHS


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme/repo", ("acme", "repo")),
        ("https://github.com/acme/repo.git", ("acme", "repo")),
        ("https://www.github.com/acme/repo/", ("acme", "repo")),
        ("git@github.com:acme/repo.git", ("acme", "repo")),
        ("github.com/acme/repo", ("acme", "repo")),
    ],
)
def test_repo_url_parsing(migrate: Any, url: str, expected: tuple[str, str]) -> None:
    args = migrate.argparse.Namespace(repo_url=url, owner=None, repo=None)
    assert migrate.parse_target(args) == expected


@pytest.mark.parametrize(
    "bad",
    [
        "https://gitlab.com/acme/repo",
        "https://github.com/acme",
        "not a url",
    ],
)
def test_bad_repo_url_is_rejected(migrate: Any, bad: str) -> None:
    args = migrate.argparse.Namespace(repo_url=bad, owner=None, repo=None)
    with pytest.raises(SystemExit):
        migrate.parse_target(args)


def test_upstream_target_is_rejected(migrate: Any) -> None:
    """Migrating to the upstream repository is a no-op worth catching early."""
    args = migrate.argparse.Namespace(
        repo_url=None,
        owner=migrate.UPSTREAM_OWNER,
        repo=migrate.UPSTREAM_REPO,
    )
    with pytest.raises(SystemExit):
        migrate.parse_target(args)


@pytest.mark.parametrize(
    ("owner", "repo"),
    [("bad owner", "repo"), ("owner", "bad repo"), ("-", "repo"), ("owner", "")],
)
def test_invalid_names_are_rejected(migrate: Any, owner: str, repo: str) -> None:
    args = migrate.argparse.Namespace(repo_url=None, owner=owner, repo=repo)
    with pytest.raises(SystemExit):
        migrate.parse_target(args)


def test_dry_run_reports_the_repository_and_writes_nothing(migrate: Any) -> None:
    """A dry run finds this checkout's references without modifying anything."""
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    report = migrate.run("acme-labs", "gco-fork", apply=False)

    assert len(report.rewrites) > 40, "expected the upstream references to be found"
    assert report.changed_files, "expected files to be reported as changing"
    assert report.follow_ups, "expected manual follow-ups to be detected"
    assert any(target == "README.md" for target, _note in report.follow_ups), (
        "expected a README.md follow-up: the Cursor install button's base64 "
        "deep link cannot be reached by a string rewrite and needs the "
        "bump_version.py table regeneration"
    )

    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert before == after, "a dry run modified the working tree"


# ---------------------------------------------------------------------------
# Fake-checkout harness: run(), _detect_follow_ups(), the report renderers
# and main() against a tree under tmp_path, with both git boundaries
# (``tracked_files`` and ``_git``) replaced so no subprocess ever runs.
# ---------------------------------------------------------------------------

import json  # noqa: E402 - grouped with the harness it serves

UPSTREAM_HTTPS = (
    "https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws"
)


def _rule(rules: tuple[Any, ...], name: str) -> Any:
    return next(rule for rule in rules if rule.name == name)


def _fake_checkout(
    migrate: Any,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    files: dict[str, str | bytes],
) -> None:
    """Materialise ``files`` under ``root`` and point the module at that tree."""
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
    monkeypatch.setattr(migrate, "REPO_ROOT", root)
    monkeypatch.setattr(migrate, "tracked_files", lambda: [Path(name) for name in sorted(files)])


_FULL_CHECKOUT: dict[str, str | bytes] = {
    "README.md": (
        f"[![CI]({UPSTREAM_HTTPS}/actions/workflows/ci.yml/badge.svg)]({UPSTREAM_HTTPS}/actions)\n"
        "<!-- BEGIN MCP INSTALL TABLE -->\n"
    ),
    ".github/oidc_provider/cdk.json": (
        '{"context": {"github_repo": '
        '"aws-solutions-library-samples/global-capacity-orchestrator-on-aws"}}\n'
    ),
    # Only a sibling-project link: classified as preserved, never rewritten,
    # so the file must not be reported as changing.
    "docs/sibling.md": "See https://github.com/aws-solutions-library-samples/guidance-for-x\n",
    # A recording that captured the upstream clone directory.
    "demo/demo.cast": '[0.1, "o", "cd global-capacity-orchestrator-on-aws"]\n',
    # A binary without any upstream mention: skipped silently.
    "images/logo.png": b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR",
    # Self-referential: keeps its upstream constants.
    "scripts/migrate_fork.py": 'UPSTREAM_OWNER = "aws-solutions-library-samples"\n',
    "empty.txt": "",
    "unrelated.py": "print('hello')\n",
    ".github/CODEOWNERS": "* @acme-labs/platform-team @octocat\n",
    "app.py": 'SOLUTION_ID = "SO9999"\n',
    ".github/SECURITY.md": "Report vulnerabilities to AWS.\n",
    "LICENSE": "Apache License\n",
    "NOTICE": "Copyright Amazon.com\n",
    ".github/workflows/pages.yml": "name: pages\n",
}


def test_occurrence_rewritten_mirrors_its_rule(rules: tuple[Any, ...], migrate: Any) -> None:
    rewrite = migrate.Occurrence("README.md", 3, _rule(rules, "repo-url"), "github.com/x/y")
    preserved = migrate.Occurrence("docs/a.md", 1, _rule(rules, "other-upstream-org-project"), "m")
    assert rewrite.rewritten == "github.com/acme-labs/gco-fork"
    assert preserved.rewritten is None


def test_working_tree_is_dirty_reads_porcelain_status(
    migrate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_git(*argv: str) -> str:
        calls.append(argv)
        return " M README.md\n?? scratch.txt\n" if len(calls) == 1 else "\n"

    monkeypatch.setattr(migrate, "_git", fake_git)
    assert migrate.working_tree_is_dirty() is True
    assert migrate.working_tree_is_dirty() is False
    assert calls == [("status", "--porcelain"), ("status", "--porcelain")]


def test_git_runs_read_only_commands_in_repo_root(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="a.txt\0b/c.md\0", stderr="")

    monkeypatch.setattr(migrate, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(migrate.subprocess, "run", fake_run)
    assert migrate.tracked_files() == [Path("a.txt"), Path("b/c.md")]
    assert seen["argv"] == ["git", "ls-files", "-z"]
    assert seen["kwargs"]["cwd"] == tmp_path
    assert seen["kwargs"]["check"] is True
    assert seen["kwargs"]["capture_output"] is True


def test_run_apply_rewrites_only_identifying_references(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)

    report = migrate.run("acme-labs", "gco-fork", apply=True)

    assert report.changed_files == [".github/oidc_provider/cdk.json", "README.md"]
    assert [(o.path, o.rule.name) for o in report.rewrites] == [
        (".github/oidc_provider/cdk.json", "repo-slug"),
        ("README.md", "repo-url"),
        ("README.md", "repo-url"),
    ]
    assert [(o.path, o.matched) for o in report.preserved] == [
        ("docs/sibling.md", "github.com/aws-solutions-library-samples/guidance-for-x"),
    ]
    assert report.skipped_self == ["scripts/migrate_fork.py"]
    assert report.skipped_binary == ["demo/demo.cast"]

    # Written to disk exactly where rewrites were recorded, nowhere else.
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == (
        "[![CI](https://github.com/acme-labs/gco-fork/actions/workflows/ci.yml/badge.svg)]"
        "(https://github.com/acme-labs/gco-fork/actions)\n"
        "<!-- BEGIN MCP INSTALL TABLE -->\n"
    )
    assert json.loads((tmp_path / ".github/oidc_provider/cdk.json").read_text()) == {
        "context": {"github_repo": "acme-labs/gco-fork"}
    }
    assert (tmp_path / "docs/sibling.md").read_text() == _FULL_CHECKOUT["docs/sibling.md"]
    assert (tmp_path / "demo/demo.cast").read_text() == _FULL_CHECKOUT["demo/demo.cast"]
    assert (tmp_path / "scripts/migrate_fork.py").read_text() == (
        _FULL_CHECKOUT["scripts/migrate_fork.py"]
    )

    # Every detected follow-up for a fully populated checkout, in order.
    assert [target for target, _note in report.follow_ups] == [
        ".github/CODEOWNERS",
        "app.py",
        ".github/SECURITY.md",
        ".github/oidc_provider/",
        "LICENSE",
        "NOTICE",
        ".github/workflows/pages.yml",
        "README.md",
        "demo/demo.cast",
    ]
    notes = dict(report.follow_ups)
    assert "Assigns review to @acme-labs/platform-team, @octocat" in notes[".github/CODEOWNERS"]
    assert "SOLUTION_ID = 'SO9999'" in notes["app.py"]
    assert "re-record with demo/record_demo.sh" in notes["demo/demo.cast"]

    # A second pass over the rewritten tree finds nothing left to rewrite.
    again = migrate.run("acme-labs", "gco-fork", apply=True)
    assert again.rewrites == []
    assert again.changed_files == []
    assert again.skipped_binary == ["demo/demo.cast"]


def test_run_dry_run_records_changes_without_writing(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)
    report = migrate.run("acme-labs", "gco-fork", apply=False)
    assert report.changed_files == [".github/oidc_provider/cdk.json", "README.md"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == _FULL_CHECKOUT["README.md"]


def test_follow_ups_are_detected_not_assumed(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A checkout without the trigger files (or with empty ones) yields no checklist."""
    _fake_checkout(
        migrate,
        monkeypatch,
        tmp_path,
        {
            "README.md": "# My fork\n",
            ".github/CODEOWNERS": "# no owners assigned yet\n",
            "app.py": "app = App()\n",
            "demo/demo.cast": '[0.1, "o", "gco status"]\n',
        },
    )
    assert migrate._detect_follow_ups() == []


def test_follow_ups_for_an_empty_checkout(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No CODEOWNERS, no app.py, no README: nothing to flag and nothing to read."""
    _fake_checkout(migrate, monkeypatch, tmp_path, {})
    assert migrate._detect_follow_ups() == []
    assert migrate.run("acme-labs", "gco-fork", apply=True) == migrate.Report()


def test_safe_read_returns_empty_for_missing_or_binary_files(
    migrate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(migrate, "REPO_ROOT", tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x80")
    (tmp_path / "text.md").write_text("hello\n", encoding="utf-8")
    assert migrate._safe_read(Path("missing.md")) == ""
    assert migrate._safe_read(Path("blob.bin")) == ""
    assert migrate._safe_read(Path("text.md")) == "hello\n"


def _sample_report(migrate: Any, rules: tuple[Any, ...]) -> Any:
    report = migrate.Report()
    repo_url = _rule(rules, "repo-url")
    sibling = _rule(rules, "other-upstream-org-project")
    report.rewrites = [
        migrate.Occurrence("README.md", 3, repo_url, "github.com/up/stream"),
        migrate.Occurrence("README.md", 9, repo_url, "github.com/up/stream"),
        migrate.Occurrence("docs/CLI.md", 1, _rule(rules, "repo-name"), "upstream-repo"),
    ]
    report.preserved = [
        migrate.Occurrence("docs/a.md", 2, sibling, "github.com/up/other"),
        migrate.Occurrence("docs/b.md", 5, sibling, "github.com/up/other"),
    ]
    report.changed_files = ["README.md", "docs/CLI.md"]
    report.skipped_self = ["scripts/migrate_fork.py"]
    report.skipped_binary = ["demo/demo.cast"]
    report.follow_ups = [
        (
            "LICENSE",
            "Upstream attribution. Left untouched deliberately; keep it, and add "
            "your own copyright rather than replacing it.",
        )
    ]
    return report


def test_print_report_dry_run_lists_every_section(
    migrate: Any, rules: tuple[Any, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    migrate.print_report(_sample_report(migrate, rules), "acme-labs", "gco-fork", apply=False)
    out = capsys.readouterr().out

    assert out.startswith("DRY RUN — nothing was written\n")
    assert "Target: aws-solutions-library-samples/global-capacity-orchestrator-on-aws" in out
    assert "-> acme-labs/gco-fork" in out
    assert "Would rewrite 3 reference(s) in 2 file(s):" in out
    # Files sorted, occurrences in recorded order, each with its rewrite.
    assert out.index("\n  README.md\n") < out.index("\n  docs/CLI.md\n")
    assert "    line 3: github.com/up/stream  ->  github.com/acme-labs/gco-fork\n" in out
    assert "    line 9: github.com/up/stream  ->  github.com/acme-labs/gco-fork\n" in out
    assert "    line 1: upstream-repo  ->  gco-fork\n" in out
    assert "Preserved 2 reference(s) that are not this repository (1 distinct):" in out
    assert (
        "    github.com/up/other  (link to a different project in the upstream org, x2)\n"
    ) in out
    assert (
        "Skipped (define or document the upstream identity):\n    scripts/migrate_fork.py\n" in out
    )
    assert "Skipped (not text, or a recording to regenerate):\n    demo/demo.cast\n" in out
    assert "Manual follow-ups (1) — see docs/FORKING.md:" in out
    assert "\n  [ ] LICENSE\n" in out
    # Notes are wrapped at 74 columns and indented under their checkbox.
    note_lines = [line for line in out.splitlines() if line.startswith("      ")]
    assert note_lines == [
        "      Upstream attribution. Left untouched deliberately; keep it, and add your",
        "      own copyright rather than replacing it.",
    ]
    assert all(len(line) - 6 <= 74 for line in note_lines)
    assert out.rstrip().endswith("Re-run with --apply to write these changes.")


def test_print_report_applied_uses_past_tense_and_no_rerun_hint(
    migrate: Any, rules: tuple[Any, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    migrate.print_report(_sample_report(migrate, rules), "acme-labs", "gco-fork", apply=True)
    out = capsys.readouterr().out
    assert out.startswith("APPLIED\n")
    assert "Rewrote 3 reference(s) in 2 file(s):" in out
    assert "Re-run with --apply" not in out


def test_print_report_with_nothing_found_is_minimal(
    migrate: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate.print_report(migrate.Report(), "acme-labs", "gco-fork", apply=False)
    out = capsys.readouterr().out
    assert "No references needed rewriting." in out
    for heading in ("Preserved", "Skipped", "Manual follow-ups", "Re-run with --apply"):
        assert heading not in out


@pytest.mark.parametrize(
    ("text", "width", "expected"),
    [
        ("", 74, []),
        ("short note", 74, ["short note"]),
        ("aaaa bbbb cccc", 9, ["aaaa bbbb", "cccc"]),
        # A word longer than the width still lands on its own line rather
        # than being dropped or producing an empty leading line.
        ("supercalifragilistic x y", 5, ["supercalifragilistic", "x y"]),
        ("  spaced   out  ", 3, ["spaced", "out"]),
    ],
)
def test_wrap(migrate: Any, text: str, width: int, expected: list[str]) -> None:
    assert migrate._wrap(text, width) == expected


def test_as_json_is_a_stable_machine_readable_report(migrate: Any, rules: tuple[Any, ...]) -> None:
    report = _sample_report(migrate, rules)
    report.changed_files = ["docs/CLI.md", "README.md"]
    payload = json.loads(migrate._as_json(report, "acme-labs", "gco-fork", apply=True))

    assert payload["applied"] is True
    assert payload["target"] == {"owner": "acme-labs", "repo": "gco-fork"}
    assert payload["upstream"] == {
        "owner": "aws-solutions-library-samples",
        "repo": "global-capacity-orchestrator-on-aws",
    }
    assert payload["rewrites"][0] == {
        "path": "README.md",
        "line": 3,
        "rule": "repo-url",
        "from": "github.com/up/stream",
        "to": "github.com/acme-labs/gco-fork",
    }
    assert payload["rewrites"][2]["to"] == "gco-fork"
    assert payload["preserved"] == [
        {
            "path": "docs/a.md",
            "line": 2,
            "rule": "other-upstream-org-project",
            "matched": "github.com/up/other",
        },
        {
            "path": "docs/b.md",
            "line": 5,
            "rule": "other-upstream-org-project",
            "matched": "github.com/up/other",
        },
    ]
    assert payload["changed_files"] == ["README.md", "docs/CLI.md"]
    assert payload["skipped"] == ["demo/demo.cast", "scripts/migrate_fork.py"]
    assert payload["follow_ups"] == [{"target": "LICENSE", "note": report.follow_ups[0][1]}]
    # Keys are sorted so diffs between runs are meaningful.
    assert list(payload) == sorted(payload)


def test_main_json_dry_run_writes_nothing(
    migrate: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)

    def no_git(*argv: str) -> str:
        raise AssertionError(f"a dry run must not consult git status: {argv}")

    monkeypatch.setattr(migrate, "_git", no_git)

    rc = migrate.main(["--repo-url", "https://github.com/acme-labs/gco-fork.git", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["applied"] is False
    assert payload["target"] == {"owner": "acme-labs", "repo": "gco-fork"}
    assert payload["changed_files"] == [".github/oidc_provider/cdk.json", "README.md"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == _FULL_CHECKOUT["README.md"]


def test_main_refuses_to_apply_on_a_dirty_tree(
    migrate: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)
    monkeypatch.setattr(migrate, "_git", lambda *argv: " M README.md\n")

    rc = migrate.main(["--owner", "acme-labs", "--repo", "gco-fork", "--apply"])

    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "Refusing to rewrite a dirty working tree" in captured.err
    assert "Override with --allow-dirty" in captured.err
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == _FULL_CHECKOUT["README.md"]


def test_main_apply_on_a_clean_tree_writes_and_prints_report(
    migrate: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)
    monkeypatch.setattr(migrate, "_git", lambda *argv: "")

    rc = migrate.main(["--owner", "acme-labs", "--repo", "gco-fork", "--apply"])

    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("APPLIED\n")
    assert "Rewrote 3 reference(s) in 2 file(s):" in out
    assert "https://github.com/acme-labs/gco-fork/actions" in (tmp_path / "README.md").read_text()


def test_main_allow_dirty_skips_the_status_check(
    migrate: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _fake_checkout(migrate, monkeypatch, tmp_path, _FULL_CHECKOUT)

    def no_git(*argv: str) -> str:
        raise AssertionError(f"--allow-dirty must not consult git status: {argv}")

    monkeypatch.setattr(migrate, "_git", no_git)

    rc = migrate.main(["--owner", "acme-labs", "--repo", "gco-fork", "--apply", "--allow-dirty"])

    assert rc == 0
    assert capsys.readouterr().out.startswith("APPLIED\n")
    assert "acme-labs/gco-fork" in (tmp_path / ".github/oidc_provider/cdk.json").read_text()


def test_main_rejects_an_unparseable_target(migrate: Any) -> None:
    with pytest.raises(SystemExit, match="Provide --repo-url, or both --owner and --repo"):
        migrate.main(["--owner", "acme-labs"])
