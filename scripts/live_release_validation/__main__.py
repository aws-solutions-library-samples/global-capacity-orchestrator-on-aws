"""Command-line entry point for live release validation."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from .models import (
    RunSettings,
    ValidationReport,
    ensure_private_run_directory,
    utc_now,
)
from .registry import build_action_registry
from .runner import LiveValidationRunner, require_local_execution


def _repository_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    else:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise ValueError("Run from a Git checkout or pass --repo-root")
        root = Path(result.stdout.strip()).resolve()
    if not (root / ".git").exists() or not (root / "cdk.json").is_file():
        raise ValueError(f"Not a GCO repository root: {root}")
    return root


def _split_actions(value: str) -> tuple[str, ...]:
    actions = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not actions:
        raise argparse.ArgumentTypeError("--actions must name at least one action")
    return actions


def _path_from_root(root: Path, value: str | None, default: Path) -> Path:
    path = Path(value).expanduser() if value else default
    candidate = path if path.is_absolute() else root / path
    return Path(os.path.abspath(os.fspath(candidate)))


def _build_parser() -> argparse.ArgumentParser:
    registry = build_action_registry()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.live_release_validation",
        description=(
            "Deploy, validate, and always destroy an exact GCO commit while producing "
            "local JSON and Markdown reports. Reports enumerate account-specific "
            "identifiers; post only a sanitized summary publicly."
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
        "--profile",
        choices=("configured", "single-region", "multi-region"),
        default="configured",
        help="Validate, but never rewrite, the topology in cdk.json",
    )
    parser.add_argument(
        "--actions",
        type=_split_actions,
        default=("all",),
        metavar="NAME[,NAME...]",
        help="Selectable actions; dependencies are added automatically (default: all)",
    )
    parser.add_argument("--list-actions", action="store_true", help="List actions and exit")
    parser.add_argument(
        "--run-id",
        help="Stable run/checkpoint identifier (default: UTC timestamp plus commit)",
    )
    parser.add_argument(
        "--report-dir",
        help="Report directory (default: .live-release-validation/<run-id>)",
    )
    parser.add_argument(
        "--checkpoint",
        help="Checkpoint JSON path (default: <report-dir>/checkpoint.json)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an exact identity-matched checkpoint",
    )
    parser.add_argument(
        "--protected-stack",
        action="append",
        default=[],
        metavar="NAME",
        help="Additional non-project CloudFormation stack to preserve exactly",
    )
    parser.add_argument("--max-workers", type=int, default=4)
    parser.add_argument("--job-timeout-seconds", type=int, default=1800)
    parser.add_argument("--queue-timeout-seconds", type=int, default=900)
    parser.add_argument("--poll-interval-seconds", type=int, default=10)
    parser.add_argument("--destroy-attempts", type=int, default=3)
    parser.add_argument("--destroy-retry-delay-seconds", type=int, default=30)
    parser.add_argument(
        "--confirm-kms-key-deletion",
        action="store_true",
        help=(
            "Explicitly authorize scheduling only this run's exact retained EKS "
            "KMS keys for deletion after stack teardown"
        ),
    )
    parser.epilog = "Actions: " + ", ".join(registry)
    return parser


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not args.expected_account or not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be an exact 12-digit AWS account ID")
    if not args.expected_sha or not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_sha):
        parser.error("--expected-sha must be an exact 40-character commit SHA")
    if not args.expected_branch or not args.expected_branch.strip():
        parser.error("--expected-branch is required")
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("--run-id must be 1-80 safe filename characters")
    for name in args.protected_stack:
        if not name or not re.fullmatch(r"[A-Za-z][-A-Za-z0-9]{0,127}", name):
            parser.error(f"Invalid --protected-stack name: {name!r}")
    for option in (
        "max_workers",
        "job_timeout_seconds",
        "queue_timeout_seconds",
        "poll_interval_seconds",
        "destroy_attempts",
        "destroy_retry_delay_seconds",
    ):
        if getattr(args, option) <= 0:
            parser.error(f"--{option.replace('_', '-')} must be positive")


def _settings_from_args(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> RunSettings:
    _validate_args(parser, args)
    root = _repository_root(args.repo_root)
    run_id = args.run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + args.expected_sha[:12].lower()
    )
    report_dir = _path_from_root(
        root,
        args.report_dir,
        Path(".live-release-validation") / run_id,
    )
    checkpoint = _path_from_root(
        root,
        args.checkpoint,
        report_dir / "checkpoint.json",
    )
    protected = tuple(dict.fromkeys(("CDKToolkit", "GCOGitHubOIDCStack", *args.protected_stack)))
    return RunSettings(
        run_id=run_id,
        repo_root=root,
        report_dir=report_dir,
        checkpoint_path=checkpoint,
        expected_account=args.expected_account,
        expected_sha=args.expected_sha.lower(),
        expected_branch=args.expected_branch.strip(),
        profile=args.profile,
        requested_actions=args.actions,
        protected_stack_names=protected,
        max_workers=args.max_workers,
        job_timeout_seconds=args.job_timeout_seconds,
        queue_timeout_seconds=args.queue_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
        destroy_attempts=args.destroy_attempts,
        destroy_retry_delay_seconds=args.destroy_retry_delay_seconds,
        confirm_kms_key_deletion=args.confirm_kms_key_deletion,
        resume=args.resume,
    )


def main() -> int:
    """Parse arguments and execute the live validation runner."""
    try:
        require_local_execution()
    except RuntimeError as exc:
        print(f"Live validation could not start: {exc}", file=sys.stderr)
        return 1

    parser = _build_parser()
    args = parser.parse_args()
    if args.list_actions:
        for definition in build_action_registry().values():
            dependencies = ", ".join(definition.dependencies) or "none"
            print(f"{definition.name:16} {definition.description} [depends: {dependencies}]")
        return 0

    settings: RunSettings | None = None
    try:
        settings = _settings_from_args(parser, args)
        return LiveValidationRunner(settings).run()
    except KeyboardInterrupt:
        print("Live validation interrupted before the runner initialized", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"Live validation could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
        if settings is not None:
            report = ValidationReport(
                run_id=settings.run_id,
                identity=settings.identity(),
                selected_actions=list(settings.requested_actions),
                started_at=utc_now(),
                ended_at=utc_now(),
                status="failed",
                fatal_error="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            )
            try:
                ensure_private_run_directory(settings.report_dir, settings.checkpoint_path)
                json_path, markdown_path = report.write(settings.report_dir)
            except (OSError, ValueError) as report_exc:
                print(
                    "Failure report was not written because the output directory is unsafe: "
                    f"{report_exc}",
                    file=sys.stderr,
                )
            else:
                print(f"JSON report: {json_path}", file=sys.stderr)
                print(f"Markdown report: {markdown_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
