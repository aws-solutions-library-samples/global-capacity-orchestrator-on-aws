"""Release lifecycle commands.

``gco release validate`` wraps the live release validation harness
(``scripts/live_release_validation``) so an operator runs one command
instead of exporting six environment variables and assembling a module
invocation by hand. The wrapper derives everything derivable — commit SHA,
branch, run id, report directory — and reserves flags for the things a
human must consciously assert:

* which account the run may touch (``--expected-account``); and
* that they understand it deploys and destroys paid infrastructure
  (``--i-understand-this-deploys-and-destroys-infrastructure``, plus
  ``--confirm-kms-key-deletion`` whenever the deploy action is selected).

There are deliberately NO interactive prompts: presence of the flags is the
consent, which makes the command automatable while keeping accidental
invocation implausible. The harness itself re-verifies every identity claim
(account, SHA, branch, clean worktree) before acting, so this wrapper adds
convenience on top of those guarantees rather than replacing them.

``--emulator-endpoint`` runs the identical harness against a local AWS
emulator (Floci) for CI rehearsal; the harness proves the endpoint is an
emulator before touching anything (see
``scripts/live_release_validation/emulator.py`` and docs/FLOCI_TESTING.md).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

_ACCOUNT_RE = re.compile(r"\d{12}")

#: The consent flag's exact name, referenced from error messages and docs.
CONSENT_FLAG = "--i-understand-this-deploys-and-destroys-infrastructure"


def _fail(message: str) -> None:
    raise click.ClickException(message)


def _run_git(repo_root: Path | None, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _fail(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _repo_root() -> Path:
    root = Path(_run_git(None, "rev-parse", "--show-toplevel"))
    if not (root / "cdk.json").is_file() or not (root / "scripts").is_dir():
        _fail(
            f"{root} is not a GCO checkout (cdk.json or scripts/ missing); "
            "run from inside the repository"
        )
    return root


@click.group()
def release() -> None:
    """Release validation lifecycle."""


@release.command("validate")
@click.option(
    "--expected-account",
    required=True,
    metavar="ACCOUNT_ID",
    help="Exact 12-digit AWS account id this run is allowed to touch.",
)
@click.option(
    CONSENT_FLAG,
    "authorized",
    is_flag=True,
    default=False,
    help=(
        "Required consent: the run deploys real, paid infrastructure into the "
        "expected account and destroys it afterwards. No prompt will ask again."
    ),
)
@click.option(
    "--confirm-kms-key-deletion",
    is_flag=True,
    default=False,
    help=(
        "Authorize scheduling this run's retained EKS KMS keys for their 7-day "
        "deletion window during cleanup. Required whenever the deploy action runs."
    ),
)
@click.option(
    "--actions",
    default="all",
    show_default=True,
    metavar="NAME[,NAME...]",
    help="Harness actions to run; dependencies are added automatically.",
)
@click.option(
    "--optional-schedulers",
    "optional_schedulers",
    default=None,
    metavar="NAME[,NAME...]",
    help=(
        "Force-enable off-by-default schedulers (yunikorn, slurm, or all) for "
        "this run's deploy so the schedulers action proves them too."
    ),
)
@click.option(
    "--profile",
    type=click.Choice(["configured", "single-region", "multi-region"]),
    default="configured",
    show_default=True,
    help="Topology profile to validate against cdk.json (never rewritten).",
)
@click.option("--run-id", default=None, help="Stable run id (default: UTC timestamp + SHA).")
@click.option(
    "--report-dir",
    default=None,
    type=click.Path(path_type=Path),
    help="Report directory (default: ~/gco-live-release-validation-reports/<run-id>).",
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
@click.option(
    "--emulator-endpoint",
    default=None,
    metavar="URL",
    help=(
        "Run the identical harness against a local AWS emulator (Floci) instead of "
        "real AWS. The harness verifies the endpoint is an emulator before acting."
    ),
)
def release_validate(
    expected_account: str,
    authorized: bool,
    confirm_kms_key_deletion: bool,
    actions: str,
    optional_schedulers: str | None,
    profile: str,
    run_id: str | None,
    report_dir: Path | None,
    resume: bool,
    protected_stack: tuple[str, ...],
    emulator_endpoint: str | None,
) -> None:
    """Run live release validation end to end without prompts.

    Derives the expected commit SHA and branch from the current checkout,
    generates a run id and a private report directory outside the worktree,
    and executes ``python -m scripts.live_release_validation``. Exits with
    the harness's exit code; reports land in the report directory.
    """
    if not _ACCOUNT_RE.fullmatch(expected_account):
        _fail("--expected-account must be an exact 12-digit AWS account id")
    if not authorized:
        _fail(
            "Refusing to run without explicit consent. Add "
            f"{CONSENT_FLAG} to acknowledge that this deploys and destroys real "
            "infrastructure in account " + expected_account + "."
        )
    selected = {name.strip() for name in actions.split(",") if name.strip()}
    if not selected:
        _fail("--actions must name at least one action")
    # Every action other than preflight/baseline transitively depends on
    # deploy, and the harness expands dependencies automatically — so any
    # such selection deploys real infrastructure and creates retained EKS
    # KMS keys, not just a literal `deploy`/`all`.
    deploy_selected = bool(selected & {"all", "deploy"}) or bool(
        selected - {"preflight", "baseline"}
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

    repo_root = _repo_root()
    expected_sha = _run_git(repo_root, "rev-parse", "HEAD")
    expected_branch = _run_git(repo_root, "symbolic-ref", "--short", "HEAD")
    resolved_run_id = run_id or (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + expected_sha[:12]
    )
    resolved_report_dir = report_dir or (
        Path.home() / "gco-live-release-validation-reports" / resolved_run_id
    )

    env = dict(os.environ)
    if emulator_endpoint:
        normalized = emulator_endpoint.rstrip("/")
        # The harness verifies these before acting; setting both here keeps a
        # single flag sufficient and makes a split-endpoint run impossible.
        env["GCO_LIVE_VALIDATION_EMULATOR"] = normalized
        env["AWS_ENDPOINT_URL"] = normalized

    command = [
        sys.executable,
        "-m",
        "scripts.live_release_validation",
        "--repo-root",
        str(repo_root),
        "--expected-account",
        expected_account,
        "--expected-sha",
        expected_sha,
        "--expected-branch",
        expected_branch,
        "--profile",
        profile,
        "--actions",
        ",".join(sorted(selected)),
        "--run-id",
        resolved_run_id,
        "--report-dir",
        str(resolved_report_dir),
        "--checkpoint",
        str(resolved_report_dir / "checkpoint.json"),
    ]
    if confirm_kms_key_deletion:
        command.append("--confirm-kms-key-deletion")
    if optional_schedulers:
        command.extend(["--optional-schedulers", optional_schedulers])
    if resume:
        command.append("--resume")
    for name in protected_stack:
        command.extend(["--protected-stack", name])

    click.echo(f"run-id:     {resolved_run_id}")
    click.echo(f"sha:        {expected_sha}")
    click.echo(f"branch:     {expected_branch}")
    click.echo(f"account:    {expected_account}")
    click.echo(f"actions:    {','.join(sorted(selected))}")
    if optional_schedulers:
        click.echo(f"schedulers: {optional_schedulers} (force-enabled for this run)")
    click.echo(f"report-dir: {resolved_report_dir}")
    if emulator_endpoint:
        click.echo(f"emulator:   {emulator_endpoint} (verified by the harness before use)")

    # Stream harness output directly; operators watch progress live and the
    # harness owns its own reporting/cleanup guarantees.
    result = subprocess.run(command, cwd=repo_root, env=env, check=False)
    sys.exit(result.returncode)
