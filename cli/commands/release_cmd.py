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

``gco release validate-upgrade`` wraps the upgrade harness
(``scripts/upgrade_validation``) the same way: it deploys the previous
release with that release's own ``gco``, upgrades it to this checkout with
``gco upgrade``, verifies the result, and destroys it (see
docs/UPGRADE_VALIDATION.md).

Both commands, and ``gco examples validate``, run their harness through
``_run_harness``, which passes Ctrl-C, ``SIGTERM`` and ``SIGHUP`` on to the
harness and waits for the cleanup they start instead of killing it.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

import click

from gco.eks_capabilities_config import EKS_CAPABILITY_TYPES

from .._image_reference import immutable_sha256_digest

_ACCOUNT_RE = re.compile(r"\d{12}")
#: ``--kubernetes-version``: a ``major.minor`` the way cdk.json spells it (``1.36``).
_KUBERNETES_MINOR_RE = re.compile(r"\d+\.\d+")

#: The consent flag's exact name, referenced from error messages and docs.
CONSENT_FLAG = "--i-understand-this-deploys-and-destroys-infrastructure"

#: After passing an interrupt on to a harness, how long the command waits for
#: the cleanup it starts: the teardown of everything the run deployed, the
#: final inventory and the report. A teardown normally takes under an hour;
#: this leaves room for slow stack deletions and the harness's retries.
HARNESS_INTERRUPT_TIMEOUT_SECONDS = 6 * 60 * 60
#: Once that has passed, the command interrupts the harness again, which stops
#: the teardown and still writes the report, and kills it if it has not exited
#: this long afterwards (the harness gives the commands it runs up to two
#: minutes to stop).
HARNESS_REPORT_TIMEOUT_SECONDS = 5 * 60
#: How often the wait wakes to pass on the signals that arrived.
_HARNESS_POLL_SECONDS = 0.2
#: What ``signal.signal`` returns and accepts.
_SignalHandler = Callable[[int, FrameType | None], Any] | int | None
#: The signals every harness turns into its controlled cleanup.
_HARNESS_INTERRUPTS: tuple[int, ...] = tuple(
    signum
    for signum in (getattr(signal, name, None) for name in ("SIGINT", "SIGTERM", "SIGHUP"))
    if signum is not None
)


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


def _describe_seconds(seconds: float) -> str:
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds / 3600:g} hours"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds / 60:g} minutes"
    return f"{seconds:g} seconds"


def _notice(message: str) -> None:
    # A closed terminal (the SIGHUP case) must not end the wait for the harness.
    with suppress(OSError):
        click.echo(f"gco: {message}", err=True)


def _signal_harness(process: subprocess.Popen[bytes], signum: int) -> None:
    """Send ``signum`` to the harness's process group, as a terminal would."""
    # killpg raises ProcessLookupError, or PermissionError on macOS when only
    # the group's zombie leader is left: the harness has exited, and the next
    # wait reaps it.
    with suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signum)


def _exit_status(returncode: int) -> int:
    """The harness's exit status; ``128 + N`` when signal N ended it, as in a shell."""
    return returncode if returncode >= 0 else 128 - returncode


def _run_harness(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    resume_hint: str,
    interrupt_timeout: float = HARNESS_INTERRUPT_TIMEOUT_SECONDS,
    report_timeout: float = HARNESS_REPORT_TIMEOUT_SECONDS,
) -> int:
    """Run a validation harness to the end and return its exit status.

    Every harness turns SIGINT, SIGTERM and SIGHUP into a controlled cleanup:
    it tears down what the run deployed and writes its report, and a second
    interrupt stops the teardown but still writes the report.
    ``subprocess.run`` defeated that. On Ctrl-C the terminal interrupted the
    harness and this process together, and ``subprocess.run`` then gave the
    harness 0.25 seconds before killing it, so the deployment stayed up and
    no report was written.

    The harness now runs in its own session, so a terminal's signals reach
    this process only, and each interrupt this process receives is passed on
    to the harness's process group once, as the terminal would have delivered
    it. A signal ignored when the command started (``nohup`` ignores SIGHUP)
    stays ignored and is not passed on. The wait after the first interrupt is
    bounded: ``interrupt_timeout`` seconds later the harness is interrupted
    once more, and ``report_timeout`` seconds after that it is killed.
    ``resume_hint`` tells the operator how to finish the teardown then.
    """
    received: list[int] = []

    def record(signum: int, _frame: FrameType | None) -> None:
        received.append(signum)

    previous: dict[int, _SignalHandler] = {}
    for signum in _HARNESS_INTERRUPTS:
        if signal.getsignal(signum) != signal.SIG_IGN:
            previous[signum] = signal.signal(signum, record)
    try:
        # nosemgrep: dangerous-subprocess-use-audit - argv built by the commands in this package; list form, no shell
        process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        passed_on = 0
        stop_cleanup_at: float | None = None
        kill_at: float | None = None
        while True:
            try:
                return _exit_status(process.wait(timeout=_HARNESS_POLL_SECONDS))
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            while passed_on < len(received):
                signum = received[passed_on]
                passed_on += 1
                _signal_harness(process, signum)
                if stop_cleanup_at is None:
                    stop_cleanup_at = now + interrupt_timeout
                    _notice(
                        f"passed {signal.Signals(signum).name} on to the validation harness, "
                        "which tears down what this run deployed and writes its report. "
                        f"Waiting up to {_describe_seconds(interrupt_timeout)} for it; "
                        "interrupt again to stop the teardown (the report is still written)."
                    )
            if kill_at is not None and now >= kill_at:
                _signal_harness(process, signal.SIGKILL)
                _notice(f"the validation harness did not exit, so it was killed. {resume_hint}")
                return _exit_status(process.wait())
            if kill_at is None and stop_cleanup_at is not None and now >= stop_cleanup_at:
                _signal_harness(process, received[0])
                kill_at = now + report_timeout
                _notice(
                    "the validation harness is still running "
                    f"{_describe_seconds(interrupt_timeout)} after the interrupt; interrupting "
                    "it again to stop the teardown and write the report (waiting up to "
                    f"{_describe_seconds(report_timeout)})."
                )
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler if handler is not None else signal.SIG_DFL)


def _resume_hint(run_id: str, report_dir: Path) -> str:
    return (
        "Finish the teardown by running the same command again with "
        f"--resume --run-id {run_id} --report-dir {report_dir}."
    )


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
@click.option(
    "--inference-triton-image",
    default=None,
    help=(
        "Immutable Triton Inference Server @sha256 image. Its model is the "
        "harness-generated identity repository, uploaded to the model registry "
        "for the run and removed afterwards."
    ),
)
@click.option(
    "--inference-gpu-count",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help=(
        "GPUs each inference endpoint replica requests. Every runtime in the matrix "
        "is a GPU server, so this is never 0."
    ),
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
    "--eks-capabilities",
    "eks_capabilities",
    default=None,
    metavar="NAME[,NAME...]",
    help=(
        "Enable off-by-default EKS Capabilities (ack, kro, or all) for this run's "
        "deploy so the eks-capabilities action proves each one attaches ACTIVE."
    ),
)
@click.option(
    "--kubernetes-version",
    "kubernetes_version",
    default=None,
    metavar="MINOR",
    help=(
        "Deploy this run's regional EKS clusters at this Kubernetes minor (for example "
        "1.36) instead of cdk.json's kubernetes_version — the same CDK context a "
        "deployment sets in cdk.json. For a feature the run turns on that trails EKS: "
        "SageMaker HyperPod publishes its node AMI per minor, after the minor ships. "
        "The topology action then verifies this minor."
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
    inference_region: str | None,
    inference_vllm_image: str | None,
    inference_vllm_model_id: str | None,
    inference_vllm_model_revision: str | None,
    inference_sglang_image: str | None,
    inference_sglang_model_id: str | None,
    inference_sglang_model_revision: str | None,
    inference_triton_image: str | None,
    inference_gpu_count: int,
    optional_schedulers: str | None,
    eks_capabilities: str | None,
    kubernetes_version: str | None,
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

    Ctrl-C, SIGTERM and SIGHUP are passed on to the harness, which tears down
    what the run deployed and writes its report; the command waits up to 6
    hours for that, then stops the teardown. Interrupt again to stop it sooner
    (the report is still written).
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
    if kubernetes_version is not None and not _KUBERNETES_MINOR_RE.fullmatch(
        kubernetes_version.strip()
    ):
        _fail(
            "--kubernetes-version must be a Kubernetes minor the way cdk.json spells it "
            f"(for example 1.36); got {kubernetes_version!r}"
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
            "--inference-triton-image": inference_triton_image,
        }
        missing = [name for name, value in required_inference.items() if not value]
        if missing:
            _fail("The inference action requires " + ", ".join(missing) + ".")
        image_digests: list[str] = []
        for option, image in (
            ("--inference-vllm-image", inference_vllm_image),
            ("--inference-sglang-image", inference_sglang_image),
            ("--inference-triton-image", inference_triton_image),
        ):
            digest = immutable_sha256_digest(image)
            if digest is None:
                _fail(f"{option} must be an immutable lowercase @sha256: reference")
            image_digests.append(digest)
        if len(set(image_digests)) != 3:
            _fail("vLLM, SGLang and Triton inference images must have distinct immutable digests")
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
                "--inference-triton-image",
                str(inference_triton_image),
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
    if kubernetes_version is not None:
        command.extend(["--kubernetes-version", kubernetes_version.strip()])
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
    if kubernetes_version is not None:
        click.echo(
            f"kubernetes: {kubernetes_version.strip()} (this run's EKS minor, in place of cdk.json's)"
        )
    click.echo(f"report-dir: {resolved_report_dir}")
    if emulator_endpoint:
        click.echo(f"emulator:   {emulator_endpoint} (verified by the harness before use)")

    # Stream harness output directly; operators watch progress live and the
    # harness owns its own reporting/cleanup guarantees.
    sys.exit(
        _run_harness(
            command,
            cwd=repo_root,
            env=env,
            resume_hint=_resume_hint(resolved_run_id, resolved_report_dir),
        )
    )


@release.command("validate-upgrade")
@click.option(
    "--expected-account",
    required=True,
    metavar="ACCOUNT_ID",
    help="Exact 12-digit AWS account id this run may touch.",
)
@click.option(
    CONSENT_FLAG,
    "authorized",
    is_flag=True,
    default=False,
    help=(
        "Required consent: deploys the previous release into the expected account, "
        "upgrades it to this checkout, and destroys it afterwards."
    ),
)
@click.option(
    "--confirm-kms-key-deletion",
    is_flag=True,
    default=False,
    help=(
        "Authorize scheduling this run's retained EKS KMS keys (both the base and the "
        "upgraded generation) for their 7-day deletion window during cleanup."
    ),
)
@click.option(
    "--base-ref",
    default=None,
    metavar="vX.Y.Z",
    help="Release to deploy first and upgrade from (default: the newest earlier release).",
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
    help="Report directory (default: ~/gco-upgrade-validation-reports/<run-id>).",
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
def release_validate_upgrade(
    expected_account: str,
    authorized: bool,
    confirm_kms_key_deletion: bool,
    base_ref: str | None,
    actions: str,
    run_id: str | None,
    report_dir: Path | None,
    resume: bool,
    protected_stack: tuple[str, ...],
) -> None:
    """Validate `gco upgrade` from the previous release to this checkout, live.

    Deploys the base release from a private clone with that release's own
    gco, runs its `gco upgrade` to the checked-out commit, verifies the
    upgraded deployment and a sentinel it must preserve, then destroys
    everything and verifies the account is back to its baseline. Executes
    ``python -m scripts.upgrade_validation`` and exits with its code. See
    docs/UPGRADE_VALIDATION.md.

    Ctrl-C, SIGTERM and SIGHUP are passed on to the harness and its cleanup
    is awaited, as for `gco release validate`.
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
    # Only preflight, baseline, and prepare stop short of the deploy action.
    deploy_selected = bool(selected & {"all", "deploy"}) or bool(
        selected - {"preflight", "baseline", "prepare"}
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
        Path.home() / "gco-upgrade-validation-reports" / resolved_run_id
    )

    command = [
        sys.executable,
        "-m",
        "scripts.upgrade_validation",
        "--repo-root",
        str(repo_root),
        "--expected-account",
        expected_account,
        "--expected-sha",
        expected_sha,
        "--expected-branch",
        expected_branch,
        "--actions",
        ",".join(sorted(selected)),
        "--run-id",
        resolved_run_id,
        "--report-dir",
        str(resolved_report_dir),
        "--checkpoint",
        str(resolved_report_dir / "checkpoint.json"),
    ]
    if base_ref:
        command.extend(["--base-ref", base_ref])
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
    click.echo(f"base:       {base_ref or 'the newest earlier release'}")
    click.echo(f"actions:    {','.join(sorted(selected))}")
    click.echo(f"report-dir: {resolved_report_dir}")

    sys.exit(
        _run_harness(
            command,
            cwd=repo_root,
            env=dict(os.environ),
            resume_hint=_resume_hint(resolved_run_id, resolved_report_dir),
        )
    )
