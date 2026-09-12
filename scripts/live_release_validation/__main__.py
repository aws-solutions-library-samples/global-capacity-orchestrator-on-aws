"""Command-line entry point for live release validation."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

from gco.inference_proxy_config import (
    INFERENCE_PROXY_TLS_CPU_REQUEST_MILLICORES_DEFAULT,
    INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION_DEFAULT,
)

from .checks.schedulers import OPTIONAL_SCHEDULERS
from .cli_args import path_from_root, repository_root, split_csv_names
from .models import (
    InferenceRuntimeSpec,
    RunCheckpoint,
    RunSettings,
    ValidationReport,
    ensure_private_run_directory,
    utc_now,
)
from .registry import build_action_registry
from .runner import LiveValidationRunner, require_local_execution

# Backwards-compatible aliases for this module's historical private helpers.
_repository_root = repository_root
_split_actions = split_csv_names
_path_from_root = path_from_root


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
        "--min-free-disk-gib",
        type=int,
        default=20,
        help=(
            "Free disk space (GiB) preflight requires on the checkout, report directory, "
            "and home volume before deploy builds container images; 0 disables the check"
        ),
    )
    parser.add_argument(
        "--confirm-kms-key-deletion",
        action="store_true",
        help=(
            "Explicitly authorize scheduling only this run's exact retained EKS "
            "KMS keys for deletion after stack teardown"
        ),
    )
    parser.add_argument(
        "--optional-schedulers",
        type=_split_actions,
        default=(),
        metavar="NAME[,NAME...]",
        help=(
            "Force-enable off-by-default schedulers for this run's deploy so the "
            "schedulers action can prove them (yunikorn, slurm, or all)"
        ),
    )
    parser.add_argument(
        "--inference-region",
        help="Deployed Region used by the inference action",
    )
    for framework, default_port in (("vllm", 8000), ("tgi", 8080)):
        parser.add_argument(
            f"--inference-{framework}-image",
            help=f"Immutable {framework} image reference containing @sha256:",
        )
        parser.add_argument(
            f"--inference-{framework}-model-id",
            help=f"Exact model identifier served by {framework}",
        )
        parser.add_argument(
            f"--inference-{framework}-model-revision",
            help=f"Full immutable 40-hex model commit served by {framework}",
        )
        parser.set_defaults(**{f"inference_{framework}_port": default_port})
    parser.add_argument("--inference-gpu-count", type=int, default=0)
    parser.add_argument(
        "--confirm-inference-deployment",
        action="store_true",
        help=(
            "Explicitly authorize the inference action to create and delete "
            "four strictly sequential vLLM/TGI endpoint scenarios"
        ),
    )
    parser.epilog = "Actions: " + ", ".join(registry)
    return parser


def _inference_selected(actions: tuple[str, ...]) -> bool:
    """Return whether dependency expansion will execute the inference action."""
    return "all" in actions or "inference" in actions


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
    if args.min_free_disk_gib < 0:
        parser.error("--min-free-disk-gib must be zero or positive")
    valid_optional = set(OPTIONAL_SCHEDULERS)
    unknown_schedulers = sorted(set(args.optional_schedulers) - valid_optional - {"all"})
    if unknown_schedulers:
        parser.error(
            "--optional-schedulers accepts "
            + ", ".join((*OPTIONAL_SCHEDULERS, "all"))
            + f"; got: {', '.join(unknown_schedulers)}"
        )
    if "all" in args.optional_schedulers and len(args.optional_schedulers) != 1:
        parser.error("--optional-schedulers 'all' cannot be combined with individual names")
    if _inference_selected(args.actions):
        required = (
            "inference_region",
            "inference_vllm_image",
            "inference_vllm_model_id",
            "inference_vllm_model_revision",
            "inference_tgi_image",
            "inference_tgi_model_id",
            "inference_tgi_model_revision",
        )
        for option in required:
            if not getattr(args, option):
                parser.error(f"--{option.replace('_', '-')} is required when inference runs")
        if not args.confirm_inference_deployment:
            parser.error(
                "--confirm-inference-deployment is required when the inference action runs"
            )
    if args.inference_gpu_count < 0:
        parser.error("--inference-gpu-count must be non-negative")


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
    inference_enabled = _inference_selected(args.actions)
    proxy_config: dict[str, object] = {
        "tls_proxy_cpu_request_millicores": (INFERENCE_PROXY_TLS_CPU_REQUEST_MILLICORES_DEFAULT),
        "tls_proxy_cpu_target_utilization_percentage": (
            INFERENCE_PROXY_TLS_CPU_TARGET_UTILIZATION_DEFAULT
        ),
    }
    if inference_enabled:
        try:
            cdk_config = json.loads((root / "cdk.json").read_text(encoding="utf-8"))
            context = cdk_config.get("context") if isinstance(cdk_config, dict) else None
            candidate = context.get("inference_proxy") if isinstance(context, dict) else None
            if candidate is not None and not isinstance(candidate, dict):
                parser.error("cdk.json context.inference_proxy must be an object or null")
            if isinstance(candidate, dict):
                proxy_config.update(candidate)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            parser.error(f"could not read inference_proxy settings from cdk.json: {error}")
    proxy_request = proxy_config["tls_proxy_cpu_request_millicores"]
    proxy_target = proxy_config["tls_proxy_cpu_target_utilization_percentage"]
    if inference_enabled and (type(proxy_request) is not int or type(proxy_target) is not int):
        parser.error("cdk.json inference_proxy TLS CPU settings must be integers")
    runtimes = (
        (
            InferenceRuntimeSpec(
                framework="vllm",
                image=args.inference_vllm_image or "",
                model_id=args.inference_vllm_model_id or "",
                model_revision=args.inference_vllm_model_revision or "",
                port=8000,
            ),
            InferenceRuntimeSpec(
                framework="tgi",
                image=args.inference_tgi_image or "",
                model_id=args.inference_tgi_model_id or "",
                model_revision=args.inference_tgi_model_revision or "",
                port=8080,
            ),
        )
        if inference_enabled
        else ()
    )
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
        min_free_disk_gib=args.min_free_disk_gib,
        confirm_kms_key_deletion=args.confirm_kms_key_deletion,
        resume=args.resume,
        optional_schedulers=(
            OPTIONAL_SCHEDULERS
            if "all" in args.optional_schedulers
            else tuple(sorted(set(args.optional_schedulers)))
        ),
        inference_enabled=inference_enabled,
        selected_region=args.inference_region or "",
        inference_runtimes=runtimes,
        proxy_tls_cpu_request=f"{proxy_request}m" if inference_enabled else "100m",
        proxy_tls_cpu_target=proxy_target if isinstance(proxy_target, int) else 70,
        gpu_count=args.inference_gpu_count,
        consent=args.confirm_inference_deployment,
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
            if settings.checkpoint_path.is_file():
                try:
                    checkpoint = RunCheckpoint.from_path(settings.checkpoint_path)
                except OSError, ValueError:
                    checkpoint = None
                if checkpoint is not None and checkpoint.deployment_attempted:
                    if checkpoint.identity == settings.identity():
                        recovery_argv = [
                            sys.executable,
                            "-m",
                            "scripts.live_release_validation",
                            *sys.argv[1:],
                        ]
                        if "--resume" not in recovery_argv:
                            recovery_argv.append("--resume")
                        report.cleanup = {
                            "needed": True,
                            "completed": False,
                            "blocked": (
                                "Runner construction failed after an identity-verified deployed "
                                "checkpoint was loaded; safe automatic destruction could not be "
                                "initialized."
                            ),
                            "recovery_command": shlex.join(recovery_argv),
                        }
                    else:
                        report.cleanup = {
                            "needed": True,
                            "completed": False,
                            "blocked": (
                                "A deployed checkpoint exists, but its identity does not match "
                                "this invocation. No cleanup authority was established; resume "
                                "with the original exact command and checkpoint identity."
                            ),
                        }
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
