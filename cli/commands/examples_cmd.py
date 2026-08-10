"""Example-manifest validation commands.

``gco examples validate`` wraps the example-job validation harness
(``scripts/example_job_validation``) exactly the way ``gco release
validate`` wraps the release harness: identity is derived from the
checkout, and the flags that remain are the ones a human must consciously
assert (target account, deploy/destroy consent, KMS deletion consent).
``--static-only`` needs none of those — it runs entirely offline.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from .release_cmd import CONSENT_FLAG, _repo_root, _run_git

_ACCOUNT_RE = re.compile(r"\d{12}")


def _fail(message: str) -> None:
    raise click.ClickException(message)


@click.group()
def examples() -> None:
    """Validate the shipped example manifests."""


@examples.command("validate")
@click.option(
    "--expected-account",
    default=None,
    metavar="ACCOUNT_ID",
    help="Exact 12-digit AWS account id this run may touch (required unless --static-only).",
)
@click.option(
    CONSENT_FLAG,
    "authorized",
    is_flag=True,
    default=False,
    help=(
        "Required consent for live runs: deploys real, paid infrastructure "
        "into the expected account and destroys it afterwards."
    ),
)
@click.option(
    "--confirm-kms-key-deletion",
    is_flag=True,
    default=False,
    help=(
        "Authorize scheduling this run's retained EKS KMS keys for their 7-day "
        "deletion window during cleanup. Required for live runs."
    ),
)
@click.option(
    "--examples",
    "selected",
    default=None,
    metavar="NAME[,NAME...]",
    help="Only validate these examples (file stems under examples/; default: all).",
)
@click.option(
    "--skip-examples",
    default=None,
    metavar="NAME[,NAME...]",
    help="Exclude these examples from the selection.",
)
@click.option(
    "--static-only",
    is_flag=True,
    default=False,
    help="Run only the offline checks (no AWS access, no consent flags needed).",
)
@click.option(
    "--actions",
    default="all",
    show_default=True,
    metavar="NAME[,NAME...]",
    help="Harness actions to run; dependencies are added automatically.",
)
@click.option("--run-id", default=None, help="Stable run id (default: UTC timestamp + SHA).")
@click.option(
    "--report-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Report directory (default: ~/gco-example-job-validation-reports/<run-id>).",
)
@click.option(
    "--resume",
    is_flag=True,
    default=False,
    help="Resume an interrupted run; requires the original --run-id and --report-dir.",
)
@click.option(
    "--protected-stack",
    multiple=True,
    metavar="NAME",
    help="Additional non-project CloudFormation stack to preserve exactly (repeatable).",
)
def examples_validate(
    expected_account: str | None,
    authorized: bool,
    confirm_kms_key_deletion: bool,
    selected: str | None,
    skip_examples: str | None,
    static_only: bool,
    actions: str,
    run_id: str | None,
    report_dir: Path | None,
    resume: bool,
    protected_stack: tuple[str, ...],
) -> None:
    """Validate example manifests, live (deploy → run → destroy) or offline.

    Live runs execute every selected example through its documented
    submission path against freshly deployed infrastructure, then destroy
    everything and write per-example reports. ``--static-only`` runs the
    offline contract checks in seconds and is the minimum bar for ANY
    change under ``examples/`` (CI enforces it too); behavior changes also
    require a live run for the affected examples. See
    docs/EXAMPLE_VALIDATION.md.
    """
    repo_root = _repo_root()
    selection_args: list[str] = []
    if selected:
        selection_args.extend(["--examples", selected])
    if skip_examples:
        selection_args.extend(["--skip-examples", skip_examples])

    if static_only:
        command = [
            sys.executable,
            "-m",
            "scripts.example_job_validation",
            "--static-only",
            *selection_args,
        ]
        result = subprocess.run(command, cwd=repo_root, check=False)
        sys.exit(result.returncode)

    if not expected_account or not _ACCOUNT_RE.fullmatch(expected_account):
        _fail("--expected-account must be an exact 12-digit AWS account id (or use --static-only)")
    if not authorized:
        _fail(
            "Refusing to run without explicit consent. Add "
            f"{CONSENT_FLAG} to acknowledge that this deploys and destroys real "
            f"infrastructure in account {expected_account}."
        )
    selected_actions = {name.strip() for name in actions.split(",") if name.strip()}
    if not selected_actions:
        _fail("--actions must name at least one action")
    deploy_selected = bool(selected_actions & {"all", "deploy"}) or bool(
        selected_actions - {"preflight", "baseline", "static"}
    )
    if deploy_selected and not confirm_kms_key_deletion:
        _fail(
            "The selected actions imply the deploy action, which creates retained "
            "EKS KMS keys; add --confirm-kms-key-deletion to authorize scheduling "
            "exactly this run's keys for deletion during cleanup."
        )
    if resume and (run_id is None or report_dir is None):
        _fail(
            "--resume replays an exact checkpoint identity: pass the original "
            "--run-id and --report-dir from the interrupted run."
        )

    expected_sha = _run_git(repo_root, "rev-parse", "HEAD")
    expected_branch = _run_git(repo_root, "symbolic-ref", "--short", "HEAD")
    resolved_run_id = run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + expected_sha[:12]
    )
    resolved_report_dir = report_dir or (
        Path.home() / "gco-example-job-validation-reports" / resolved_run_id
    )

    command = [
        sys.executable,
        "-m",
        "scripts.example_job_validation",
        "--repo-root",
        str(repo_root),
        "--expected-account",
        expected_account,
        "--expected-sha",
        expected_sha,
        "--expected-branch",
        expected_branch,
        "--actions",
        ",".join(sorted(selected_actions)),
        "--run-id",
        resolved_run_id,
        "--report-dir",
        str(resolved_report_dir),
        "--checkpoint",
        str(resolved_report_dir / "checkpoint.json"),
        *selection_args,
    ]
    if confirm_kms_key_deletion:
        command.append("--confirm-kms-key-deletion")
    if resume:
        command.append("--resume")
    for name in protected_stack:
        command.extend(["--protected-stack", name])

    click.echo(f"run-id:     {resolved_run_id}")
    click.echo(f"sha:        {expected_sha}")
    click.echo(f"branch:     {expected_branch}")
    click.echo(f"account:    {expected_account}")
    click.echo(f"actions:    {','.join(sorted(selected_actions))}")
    click.echo(f"examples:   {selected or 'all'}" + (f" minus {skip_examples}" if skip_examples else ""))
    click.echo(f"report-dir: {resolved_report_dir}")

    result = subprocess.run(command, cwd=repo_root, env=dict(os.environ), check=False)
    sys.exit(result.returncode)
