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
from typing import NoReturn

import click

from gco.eks_capabilities_config import EKS_CAPABILITY_TYPES, GITOPS_SYNC_POLICIES

from .._image_reference import immutable_sha256_digest

_ACCOUNT_RE = re.compile(r"\d{12}")

#: The consent flag's exact name, referenced from error messages and docs.
CONSENT_FLAG = "--i-understand-this-deploys-and-destroys-infrastructure"


def _fail(message: str) -> NoReturn:
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
@click.option("--inference-region", default=None, help="Region for the inference matrix.")
@click.option("--inference-vllm-image", default=None, help="Immutable vLLM @sha256 image.")
@click.option("--inference-vllm-model-id", default=None, help="Exact vLLM model identifier.")
@click.option(
    "--inference-vllm-model-revision",
    default=None,
    help="Full immutable 40-hex vLLM model commit.",
)
@click.option("--inference-sglang-image", default=None, help="Immutable SGLang @sha256 image.")
@click.option("--inference-sglang-model-id", default=None, help="Exact SGLang model identifier.")
@click.option(
    "--inference-sglang-model-revision",
    default=None,
    help="Full immutable 40-hex SGLang model commit.",
)
@click.option("--inference-gpu-count", type=click.IntRange(min=0), default=0, show_default=True)
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
    "--eks-capabilities",
    "eks_capabilities",
    default=None,
    metavar="NAME[,NAME...]",
    help=(
        "Enable off-by-default EKS Capabilities (argocd, ack, kro, or all) for this "
        "run's deploy so the eks-capabilities action proves them. Self-contained: "
        "without --argocd-idc-instance-arn/--argocd-identity the argocd-identity "
        "action provisions an Identity Center account instance and group, and "
        "without --argocd-gitops-repo-url the GitOps hand-off uses the GCO-managed "
        "CodeCommit repository the run pushes its fixture into."
    ),
)
@click.option(
    "--argocd-idc-instance-arn",
    default=None,
    metavar="ARN",
    help="Existing IAM Identity Center instance for the hosted Argo CD (requires argocd).",
)
@click.option(
    "--argocd-idc-region",
    default=None,
    metavar="REGION",
    help=(
        "Region of the Identity Center instance, or where argocd-identity creates one "
        "(default: the first deployment Region)."
    ),
)
@click.option(
    "--argocd-identity",
    multiple=True,
    metavar="TYPE:ID",
    help=(
        "Existing Identity Center user or group granted the Argo CD ADMIN role "
        "(SSO_USER:<id> or SSO_GROUP:<id>; repeatable)."
    ),
)
@click.option(
    "--argocd-gitops-repo-url",
    default=None,
    metavar="URL",
    help=(
        "Point the GitOps hand-off at this operator repository (source: git) instead "
        "of the GCO-managed CodeCommit repository."
    ),
)
@click.option(
    "--argocd-gitops-revision",
    default=None,
    metavar="REVISION",
    help="Revision Argo CD syncs from --argocd-gitops-repo-url (default: the run's SHA).",
)
@click.option(
    "--argocd-gitops-path",
    default=None,
    metavar="PATH",
    help=(
        "Fixture directory Argo CD must sync into gco-jobs "
        "(default: the harness's examples/gitops/tenant-smoke)."
    ),
)
@click.option(
    "--argocd-gitops-sync-policy",
    type=click.Choice(list(GITOPS_SYNC_POLICIES)),
    default=None,
    help="Sync policy of the root Application (default: automated).",
)
@click.option(
    "--no-argocd-gitops",
    is_flag=True,
    default=False,
    help="Enable the Argo CD capability without the GitOps hand-off.",
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
    inference_region: str | None,
    inference_vllm_image: str | None,
    inference_vllm_model_id: str | None,
    inference_vllm_model_revision: str | None,
    inference_sglang_image: str | None,
    inference_sglang_model_id: str | None,
    inference_sglang_model_revision: str | None,
    inference_gpu_count: int,
    optional_schedulers: str | None,
    eks_capabilities: str | None,
    argocd_idc_instance_arn: str | None,
    argocd_idc_region: str | None,
    argocd_identity: tuple[str, ...],
    argocd_gitops_repo_url: str | None,
    argocd_gitops_revision: str | None,
    argocd_gitops_path: str | None,
    argocd_gitops_sync_policy: str | None,
    no_argocd_gitops: bool,
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
    inference_selected = bool(selected & {"all", "inference"})
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
    capability_names = {
        name.strip() for name in (eks_capabilities or "").split(",") if name.strip()
    }
    if eks_capabilities is not None and not capability_names:
        _fail("--eks-capabilities must name at least one capability")
    unknown_capabilities = sorted(capability_names - set(EKS_CAPABILITY_TYPES) - {"all"})
    if unknown_capabilities:
        _fail(
            "--eks-capabilities accepts "
            + ", ".join((*EKS_CAPABILITY_TYPES, "all"))
            + "; got: "
            + ", ".join(unknown_capabilities)
        )
    if "all" in capability_names and len(capability_names) != 1:
        _fail("--eks-capabilities 'all' cannot be combined with individual names")
    argocd_options = {
        "--argocd-idc-instance-arn": bool(argocd_idc_instance_arn),
        "--argocd-idc-region": bool(argocd_idc_region),
        "--argocd-identity": bool(argocd_identity),
        "--argocd-gitops-repo-url": bool(argocd_gitops_repo_url),
        "--argocd-gitops-revision": bool(argocd_gitops_revision),
        "--argocd-gitops-path": bool(argocd_gitops_path),
        "--argocd-gitops-sync-policy": bool(argocd_gitops_sync_policy),
        "--no-argocd-gitops": no_argocd_gitops,
    }
    stray_argocd = [name for name, given in argocd_options.items() if given]
    if stray_argocd and not capability_names & {"argocd", "all"}:
        # The harness would silently ignore these without the Argo CD capability;
        # a wrapper that exists to remove ambiguity must not let that pass.
        _fail(
            ", ".join(stray_argocd)
            + " configure the Argo CD capability; add --eks-capabilities argocd (or all)."
        )
    gitops_options = [
        name
        for name in (
            "--argocd-gitops-repo-url",
            "--argocd-gitops-revision",
            "--argocd-gitops-path",
            "--argocd-gitops-sync-policy",
        )
        if argocd_options[name]
    ]
    if no_argocd_gitops and gitops_options:
        _fail(
            ", ".join(gitops_options)
            + " configure the GitOps hand-off that --no-argocd-gitops disables; drop one side."
        )
    if inference_selected:
        required_inference = {
            "--inference-region": inference_region,
            "--inference-vllm-image": inference_vllm_image,
            "--inference-vllm-model-id": inference_vllm_model_id,
            "--inference-vllm-model-revision": inference_vllm_model_revision,
            "--inference-sglang-image": inference_sglang_image,
            "--inference-sglang-model-id": inference_sglang_model_id,
            "--inference-sglang-model-revision": inference_sglang_model_revision,
        }
        missing = [name for name, value in required_inference.items() if not value]
        if missing:
            _fail("The inference action requires " + ", ".join(missing) + ".")
        image_digests: list[str] = []
        for option, image in (
            ("--inference-vllm-image", inference_vllm_image),
            ("--inference-sglang-image", inference_sglang_image),
        ):
            digest = immutable_sha256_digest(image)
            if digest is None:
                _fail(f"{option} must be an immutable lowercase @sha256: reference")
            image_digests.append(digest)
        if len(set(image_digests)) != 2:
            _fail("vLLM and SGLang inference images must have distinct immutable digests")
        for option, revision in (
            ("--inference-vllm-model-revision", inference_vllm_model_revision),
            ("--inference-sglang-model-revision", inference_sglang_model_revision),
        ):
            if revision is None or not re.fullmatch(r"[0-9a-f]{40}", revision):
                _fail(f"{option} must be a full lowercase 40-hex commit")

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
    if inference_selected:
        command.extend(
            [
                "--inference-region",
                str(inference_region),
                "--inference-vllm-image",
                str(inference_vllm_image),
                "--inference-vllm-model-id",
                str(inference_vllm_model_id),
                "--inference-vllm-model-revision",
                str(inference_vllm_model_revision),
                "--inference-sglang-image",
                str(inference_sglang_image),
                "--inference-sglang-model-id",
                str(inference_sglang_model_id),
                "--inference-sglang-model-revision",
                str(inference_sglang_model_revision),
                "--inference-gpu-count",
                str(inference_gpu_count),
                "--confirm-inference-deployment",
            ]
        )
    if confirm_kms_key_deletion:
        command.append("--confirm-kms-key-deletion")
    if optional_schedulers:
        command.extend(["--optional-schedulers", optional_schedulers])
    if eks_capabilities:
        command.extend(["--eks-capabilities", eks_capabilities])
    for option, value in (
        ("--argocd-idc-instance-arn", argocd_idc_instance_arn),
        ("--argocd-idc-region", argocd_idc_region),
        ("--argocd-gitops-repo-url", argocd_gitops_repo_url),
        ("--argocd-gitops-revision", argocd_gitops_revision),
        ("--argocd-gitops-path", argocd_gitops_path),
        ("--argocd-gitops-sync-policy", argocd_gitops_sync_policy),
    ):
        if value:
            command.extend([option, value])
    for identity in argocd_identity:
        command.extend(["--argocd-identity", identity])
    if no_argocd_gitops:
        command.append("--no-argocd-gitops")
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
    if eks_capabilities:
        click.echo(f"capabilities: {eks_capabilities} (enabled for this run)")
    click.echo(f"report-dir: {resolved_report_dir}")
    if emulator_endpoint:
        click.echo(f"emulator:   {emulator_endpoint} (verified by the harness before use)")

    # Stream harness output directly; operators watch progress live and the
    # harness owns its own reporting/cleanup guarantees.
    result = subprocess.run(command, cwd=repo_root, env=env, check=False)
    sys.exit(result.returncode)
