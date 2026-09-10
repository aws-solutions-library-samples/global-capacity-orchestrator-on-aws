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
