"""CLI entry: ``python -m scripts.upgrade_validation``.

Mirrors ``scripts.live_release_validation.__main__`` (the same identity flags,
consent posture, and checkpoint/resume semantics) plus the release to upgrade
from (``--base-ref``, by default the newest release tag in the checkout's
history) and the private workspace the base release is cloned into.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from cli.upgrade import RELEASE_TAG_RE, parse_release_tags
from scripts.live_release_validation.cli_args import (
    path_from_root,
    repository_root,
    split_csv_names,
)
from scripts.live_release_validation.models import ValidationReport, utc_now
from scripts.live_release_validation.runner import (
    LiveValidationRunner,
    require_local_execution,
)

from .models import UpgradeRunSettings
from .registry import build_action_registry
from .workspace import Workspace, remove_workspace

REPORT_TITLE = "GCO Upgrade Validation"
REPORT_STEM = "upgrade-validation"
DEFAULT_REPORTS_DIR = Path("~/gco-upgrade-validation-reports")


def _build_parser() -> argparse.ArgumentParser:
    registry = build_action_registry()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.upgrade_validation",
        description=(
            "Deploy the previous release with its own gco, upgrade it to this checkout "
            "with that gco's upgrade command, verify the upgraded deployment, and always "
            "destroy what was deployed. Reports carry account-specific identifiers; post "
            "only sanitized summaries."
        ),
    )
    parser.add_argument("--repo-root", help="GCO checkout (default: current Git root)")
    parser.add_argument(
        "--expected-account",
        default=os.environ.get("GCO_LIVE_EXPECTED_ACCOUNT"),
        help="Exact 12-digit AWS account ID (or GCO_LIVE_EXPECTED_ACCOUNT)",
    )
    parser.add_argument(
        "--expected-sha",
        default=os.environ.get("GCO_LIVE_EXPECTED_SHA"),
        help="Exact 40-character Git commit (or GCO_LIVE_EXPECTED_SHA)",
    )
    parser.add_argument(
        "--expected-branch",
        default=os.environ.get("GCO_LIVE_EXPECTED_BRANCH"),
        help="Exact local branch identity (or GCO_LIVE_EXPECTED_BRANCH)",
    )
    parser.add_argument(
        "--base-ref",
        metavar="vX.Y.Z",
        help="Release to deploy first and upgrade from (default: the newest earlier release)",
    )
    parser.add_argument(
        "--actions",
        type=split_csv_names,
        default=("all",),
        metavar="NAME[,NAME...]",
        help="Selectable actions; dependencies are added automatically (default: all)",
    )
    parser.add_argument("--list-actions", action="store_true", help="List actions and exit")
    parser.add_argument("--run-id", help="Stable run/checkpoint identifier")
    parser.add_argument(
        "--report-dir", help=f"Report directory (default: {DEFAULT_REPORTS_DIR}/<run-id>)"
    )
    parser.add_argument(
        "--workspace-dir",
        help="Private workspace for the base release (default: <report-dir>.workspace)",
    )
    parser.add_argument(
        "--checkpoint", help="Checkpoint JSON path (default: <report-dir>/checkpoint.json)"
    )
    parser.add_argument(
        "--resume", action="store_true", help="Resume an exact identity-matched checkpoint"
    )
    parser.add_argument(
        "--protected-stack",
        action="append",
        default=[],
        metavar="NAME",
        help="Additional non-project CloudFormation stack to preserve exactly",
    )
    parser.add_argument(
        "--confirm-kms-key-deletion",
        action="store_true",
        help=(
            "Explicitly authorize scheduling only this run's exact retained EKS KMS keys "
            "(both generations) for deletion after stack teardown"
        ),
    )
    parser.add_argument(
        "--min-free-disk-gib",
        type=int,
        default=20,
        metavar="GIB",
        help="Free-space floor for the checkout, reports, home, and workspace (0 disables)",
    )
    parser.epilog = "Actions: " + ", ".join(registry)
    return parser


def _git_output(root: Path, *arguments: str) -> str:
    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - fixed git argv, no shell
        ["git", *arguments], cwd=root, capture_output=True, text=True, check=False
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _tag_commit(root: Path, tag: str) -> str:
    return _git_output(root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")


def _resolve_base(
    parser: argparse.ArgumentParser, root: Path, requested: str | None, expected_sha: str
) -> tuple[str, str]:
    """The base release tag and its commit: the named one, or the newest earlier release."""
    if requested:
        if not RELEASE_TAG_RE.fullmatch(requested):
            parser.error("--base-ref must name a release tag vMAJOR.MINOR.PATCH")
        commit = _tag_commit(root, requested)
        if not commit:
            parser.error(f"--base-ref {requested} is not a tag in {root}")
        return requested, commit
    merged = _git_output(root, "tag", "--list", "v*", "--merged", expected_sha).splitlines()
    for tag in reversed(parse_release_tags(merged)):
        commit = _tag_commit(root, tag.name)
        if commit and commit != expected_sha:
            return tag.name, commit
    parser.error(f"No release tag precedes {expected_sha} in {root}; pass --base-ref")


def _settings_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> UpgradeRunSettings:
    if not args.expected_account or not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be an exact 12-digit AWS account ID")
    if not args.expected_sha or not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_sha):
        parser.error("--expected-sha must be an exact 40-character commit SHA")
    if not args.expected_branch or not args.expected_branch.strip():
        parser.error("--expected-branch is required")
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("--run-id must be 1-80 safe filename characters")
    if args.min_free_disk_gib < 0:
        parser.error("--min-free-disk-gib must be >= 0 (0 disables the check)")
    root = repository_root(args.repo_root)
    expected_sha = args.expected_sha.lower()
    base_ref, base_commit = _resolve_base(parser, root, args.base_ref, expected_sha)
    run_id = args.run_id or (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + expected_sha[:12])
    report_dir = path_from_root(root, args.report_dir, DEFAULT_REPORTS_DIR.expanduser() / run_id)
    workspace_dir = path_from_root(
        root, args.workspace_dir, report_dir.with_name(f"{report_dir.name}.workspace")
    )
    checkpoint = path_from_root(root, args.checkpoint, report_dir / "checkpoint.json")
    protected = tuple(dict.fromkeys(("CDKToolkit", "GCOGitHubOIDCStack", *args.protected_stack)))
    return UpgradeRunSettings(
        run_id=run_id,
        repo_root=root,
        report_dir=report_dir,
        checkpoint_path=checkpoint,
        expected_account=args.expected_account,
        expected_sha=expected_sha,
        expected_branch=args.expected_branch.strip(),
        profile="configured",
        requested_actions=args.actions,
        protected_stack_names=protected,
        confirm_kms_key_deletion=args.confirm_kms_key_deletion,
        resume=args.resume,
        min_free_disk_gib=args.min_free_disk_gib,
        base_ref=base_ref,
        base_commit=base_commit,
        workspace_dir=workspace_dir,
    )


def _remove_finished_workspace(settings: UpgradeRunSettings, runner: LiveValidationRunner) -> None:
    """Delete the workspace once teardown completed; it has no use after that."""
    if not runner.checkpoint.destroyed:
        print(f"Workspace kept for resume or inspection: {settings.workspace_dir}")
        return
    try:
        removed = remove_workspace(Workspace(settings.workspace_dir), settings.run_id)
    except (OSError, RuntimeError) as exc:
        print(f"Could not remove the workspace {settings.workspace_dir}: {exc}", file=sys.stderr)
        return
    if removed:
        print(f"Removed the workspace {settings.workspace_dir}")


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.list_actions:
        for definition in build_action_registry().values():
            dependencies = ", ".join(definition.dependencies) or "none"
            print(f"{definition.name:16} {definition.description} [depends: {dependencies}]")
        return 0
    try:
        require_local_execution()
    except RuntimeError as exc:
        print(f"Upgrade validation could not start: {exc}", file=sys.stderr)
        return 1
    settings: UpgradeRunSettings | None = None
    try:
        settings = _settings_from_args(parser, args)
        runner = LiveValidationRunner(settings, registry=build_action_registry())
        runner.report.title = REPORT_TITLE
        runner.report.report_stem = REPORT_STEM
        code = runner.run()
        _remove_finished_workspace(settings, runner)
        return code
    except KeyboardInterrupt:
        print("Upgrade validation interrupted before the runner initialized", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"Upgrade validation could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        if settings is not None:
            report = ValidationReport(
                run_id=settings.run_id,
                identity=settings.identity(),
                selected_actions=list(settings.requested_actions),
                started_at=utc_now(),
                ended_at=utc_now(),
                status="failed",
                fatal_error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                title=REPORT_TITLE,
                report_stem=REPORT_STEM,
            )
            with contextlib.suppress(OSError):
                report.write(settings.report_dir)
        return 1


if __name__ == "__main__":
    sys.exit(main())
