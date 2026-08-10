"""CLI entry: ``python -m scripts.example_job_validation``.

Mirrors ``scripts.live_release_validation.__main__`` (same identity flags,
consent posture, checkpoint/resume semantics) plus example selection and a
fully offline ``--static-only`` mode.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from scripts.live_release_validation.cli_args import (
    path_from_root,
    repository_root,
    split_csv_names,
)
from scripts.live_release_validation.models import (
    ValidationReport,
    utc_now,
)
from scripts.live_release_validation.runner import (
    LiveValidationRunner,
    require_local_execution,
)

from .models import ExampleRunSettings
from .registry import build_action_registry
from .specs import EXAMPLE_SPECS
from .static_checks import run_static_checks

REPORT_TITLE = "GCO Example Job Validation"
REPORT_STEM = "example-job-validation"


def _split_names(value: str) -> tuple[str, ...]:
    names = tuple(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))
    if not names:
        raise argparse.ArgumentTypeError("expected at least one name")
    return names


def _build_parser() -> argparse.ArgumentParser:
    registry = build_action_registry()
    parser = argparse.ArgumentParser(
        prog="python -m scripts.example_job_validation",
        description=(
            "Deploy the configured GCO topology, run every selected example "
            "through its documented submission path, verify its success "
            "criteria, and always destroy what was deployed. Reports carry "
            "account-specific identifiers; post only sanitized summaries."
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
        "--actions",
        type=split_csv_names,
        default=("all",),
        metavar="NAME[,NAME...]",
        help="Selectable actions; dependencies are added automatically (default: all)",
    )
    parser.add_argument("--list-actions", action="store_true", help="List actions and exit")
    parser.add_argument(
        "--examples",
        type=_split_names,
        default=(),
        metavar="NAME[,NAME...]",
        help="Only validate these examples (file stems; default: every example)",
    )
    parser.add_argument(
        "--skip-examples",
        type=_split_names,
        default=(),
        metavar="NAME[,NAME...]",
        help="Exclude these examples from the selection",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run only the offline checks (no AWS access) and exit",
    )
    parser.add_argument("--run-id", help="Stable run/checkpoint identifier")
    parser.add_argument(
        "--report-dir", help="Report directory (default: .example-job-validation/<run-id>)"
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
            "Explicitly authorize scheduling only this run's exact retained EKS "
            "KMS keys for deletion after stack teardown"
        ),
    )
    parser.epilog = (
        "Actions: " + ", ".join(registry) + ". Examples: " + ", ".join(sorted(EXAMPLE_SPECS))
    )
    return parser


def _select_examples(parser: argparse.ArgumentParser, args: argparse.Namespace) -> tuple[str, ...]:
    selected = list(args.examples) if args.examples else sorted(EXAMPLE_SPECS)
    unknown = sorted({*args.examples, *args.skip_examples} - set(EXAMPLE_SPECS))
    if unknown:
        parser.error(f"Unknown example name(s): {', '.join(unknown)}")
    return tuple(name for name in selected if name not in set(args.skip_examples))


def _settings_from_args(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> ExampleRunSettings:
    if not args.expected_account or not re.fullmatch(r"\d{12}", args.expected_account):
        parser.error("--expected-account must be an exact 12-digit AWS account ID")
    if not args.expected_sha or not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_sha):
        parser.error("--expected-sha must be an exact 40-character commit SHA")
    if not args.expected_branch or not args.expected_branch.strip():
        parser.error("--expected-branch is required")
    if args.run_id and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", args.run_id):
        parser.error("--run-id must be 1-80 safe filename characters")
    root = repository_root(args.repo_root)
    run_id = args.run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + args.expected_sha[:12].lower()
    )
    report_dir = path_from_root(root, args.report_dir, Path(".example-job-validation") / run_id)
    checkpoint = path_from_root(root, args.checkpoint, report_dir / "checkpoint.json")
    protected = tuple(dict.fromkeys(("CDKToolkit", "GCOGitHubOIDCStack", *args.protected_stack)))
    return ExampleRunSettings(
        run_id=run_id,
        repo_root=root,
        report_dir=report_dir,
        checkpoint_path=checkpoint,
        expected_account=args.expected_account,
        expected_sha=args.expected_sha.lower(),
        expected_branch=args.expected_branch.strip(),
        profile="configured",
        requested_actions=args.actions,
        protected_stack_names=protected,
        confirm_kms_key_deletion=args.confirm_kms_key_deletion,
        resume=args.resume,
        selected_examples=_select_examples(parser, args),
    )


def _run_static_only(parser: argparse.ArgumentParser, args: argparse.Namespace) -> int:
    root = repository_root(args.repo_root)
    names = list(_select_examples(parser, args))
    findings = run_static_checks(root, names)
    failed = [finding for finding in findings if not finding.passed]
    for finding in findings:
        marker = "ok " if finding.passed else "FAIL"
        detail = f" — {finding.detail}" if finding.detail else ""
        print(f"[{marker}] {finding.example}: {finding.check}{detail}")
    print(f"{len(findings)} checks, {len(failed)} failed")
    return 1 if failed else 0


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.list_actions:
        for definition in build_action_registry().values():
            dependencies = ", ".join(definition.dependencies) or "none"
            print(f"{definition.name:16} {definition.description} [depends: {dependencies}]")
        return 0
    if args.static_only:
        return _run_static_only(parser, args)
    try:
        require_local_execution()
    except RuntimeError as exc:
        print(f"Example validation could not start: {exc}", file=sys.stderr)
        return 1
    settings: ExampleRunSettings | None = None
    try:
        settings = _settings_from_args(parser, args)
        runner = LiveValidationRunner(settings, registry=build_action_registry())
        runner.report.title = REPORT_TITLE
        runner.report.report_stem = REPORT_STEM
        return runner.run()
    except KeyboardInterrupt:
        print("Example validation interrupted before the runner initialized", file=sys.stderr)
        return 130
    except BaseException as exc:
        print(f"Example validation could not start: {type(exc).__name__}: {exc}", file=sys.stderr)
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
