"""Whole-deployment upgrade orchestration behind ``gco upgrade``.

An upgrade moves a GCO installation — the checked-out source, the locally
installed CLI and toolchain, the ``gco-dev`` container image, and the deployed
CloudFormation stacks — to a tagged release in one pass:

1. **Resolve the release.** Fetch the tags of the configured git remote and pick
   the highest ``vMAJOR.MINOR.PATCH`` (or the tag named with ``--ref``).
2. **Move the checkout.** ``git checkout --detach <tag>`` with ``cdk.json``
   preserved byte-for-byte: the deployment's configuration is the operator's,
   not the release's, so it is snapshotted before the checkout and written back
   afterwards.
3. **Refresh the local install.** ``pip install -e .`` when the running CLI is
   the editable install of this checkout, ``npm ci`` when the checkout carries
   its own CDK toolchain, and a rebuild of the ``gco-dev`` image when a
   container runtime and the image are both present.
4. **Cycle the stacks.** Scale the workload tier to zero
   (:meth:`cli.stacks.StackManager.destroy_orchestrated` with
   ``keep_control_plane=True`` — the monitoring stack is first updated in place
   so it stops referencing the regional stacks), then run the ordinary
   deploy-all: the global and API Gateway stacks are updated in place and the
   regional stacks, their API bridges and the monitoring stack are recreated
   from the new release.

Step 4 deletes every regional stack, and with it the data those stacks own
(EFS and FSx file systems, Valkey, Aurora, in-cluster volumes, the
regional-shared bucket unless retained). ``docs/UPGRADING.md`` describes the
back-up-to-the-cluster-shared-bucket / upgrade / restore procedure; the
command's confirmation prompt repeats the essentials.

The functions here take plain arguments and return dataclasses so the Click
layer (:mod:`cli.commands.upgrade_cmd`) stays a thin veneer and every branch
can be exercised without git, pip, or AWS.
"""

from __future__ import annotations

import re
import shutil
import subprocess  # fixed argv only, never a shell string
import sys
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from ._container_runtime import container_runtime_error_message, detect_container_runtime

#: Release tags are the immutable ``vMAJOR.MINOR.PATCH`` tags release-publish.yml creates.
RELEASE_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")

#: The git remote whose tags define "the latest release" unless ``--remote`` says otherwise.
DEFAULT_REMOTE = "origin"

#: The dev-container image ``scripts/setup-dev-alias.sh`` builds and the shell function runs.
DEFAULT_DEV_IMAGE = "gco-dev"

#: Files that belong to the deployment rather than the release and survive the checkout.
PRESERVED_FILES: tuple[str, ...] = ("cdk.json",)

#: Wall-clock cap for one git/pip/npm/image-build subprocess.
_TOOL_TIMEOUT_SECONDS = 1800.0

Logger = Callable[[str], None]


class UpgradeError(RuntimeError):
    """A precondition or step of the upgrade failed; the message is operator-facing."""


def _quiet(_message: str) -> None:
    """Default progress sink."""


# =============================================================================
# Release tags
# =============================================================================


@dataclass(frozen=True, order=True)
class ReleaseTag:
    """One ``vMAJOR.MINOR.PATCH`` tag, ordered by version."""

    version: tuple[int, int, int]
    name: str

    @classmethod
    def parse(cls, name: str) -> ReleaseTag | None:
        """Return the tag when ``name`` has the release shape, else ``None``."""
        match = RELEASE_TAG_RE.match(name.strip())
        if match is None:
            return None
        major, minor, patch = (int(part) for part in match.groups())
        return cls(version=(major, minor, patch), name=name.strip())

    @property
    def dotted(self) -> str:
        """The version without its ``v`` prefix, as ``VERSION`` and ``gco --version`` print it."""
        return ".".join(str(part) for part in self.version)


def parse_release_tags(names: Iterable[str]) -> list[ReleaseTag]:
    """Keep only release-shaped tags, sorted ascending by version."""
    tags = [tag for tag in (ReleaseTag.parse(name) for name in names) if tag is not None]
    return sorted(set(tags))


def latest_release(tags: Sequence[ReleaseTag]) -> ReleaseTag:
    """Return the highest release tag or explain that there is none."""
    if not tags:
        raise UpgradeError(
            "No release tags (vMAJOR.MINOR.PATCH) were found on the remote. Check that the "
            "remote is the GCO repository (or a fork that carries its tags) and that "
            "'git fetch --tags' succeeds."
        )
    return tags[-1]


def resolve_target(tags: Sequence[ReleaseTag], ref: str | None) -> ReleaseTag:
    """Pick the upgrade target: the latest release, or the explicitly named one."""
    if ref is None:
        return latest_release(tags)
    wanted = ReleaseTag.parse(ref)
    if wanted is None:
        raise UpgradeError(
            f"--ref must name a release tag such as v8.1.0 (got {ref!r}); gco upgrade only "
            "moves between tagged releases."
        )
    for tag in tags:
        if tag == wanted:
            return tag
    known = ", ".join(tag.name for tag in tags[-5:]) or "none"
    raise UpgradeError(f"Release tag {wanted.name} does not exist on the remote (latest: {known}).")


# =============================================================================
# The checkout
# =============================================================================


@dataclass
class CheckoutState:
    """What ``git`` says about the checkout before anything is changed."""

    root: Path
    head: str
    ref: str
    version_file: str | None
    dirty_paths: list[str] = field(default_factory=list)
    preserved_modified: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["root"] = str(self.root)
        return data


def _run(
    argv: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = _TOOL_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed-argv tool, translating a missing binary or a timeout into UpgradeError."""
    try:
        return subprocess.run(  # fixed argv, no shell
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UpgradeError(f"{argv[0]} is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise UpgradeError(f"{' '.join(argv)} did not finish within {timeout:.0f}s") from exc


def run_git(root: Path, *args: str) -> str:
    """Run ``git -C <root> <args>`` and return stdout, raising UpgradeError on failure."""
    result = _run(["git", "-C", str(root), *args])
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or f"exit status {result.returncode}"
        raise UpgradeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout


def find_checkout_root(start: Path | None = None) -> Path:
    """Locate the GCO checkout: the nearest ancestor holding both ``cdk.json`` and ``.git``.

    Deploying needs the CDK app (``cdk.json``/``app.py``) and moving between
    releases needs git history, so an installed-only CLI (``uv tool``, ``pipx``
    from a git URL) running outside a clone is refused with the fix spelled out.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "cdk.json").is_file() and (candidate / ".git").exists():
            return candidate
    raise UpgradeError(
        "gco upgrade must run inside a git checkout of the GCO repository (a directory tree "
        "holding cdk.json and .git). Clone the repository, cd into it, and run the command "
        "again; the CLI you are running does not need to come from that checkout."
    )


def inspect_checkout(root: Path) -> CheckoutState:
    """Record HEAD, the symbolic ref, the VERSION file, and every local modification."""
    head = run_git(root, "rev-parse", "--short", "HEAD").strip()
    ref = run_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if ref == "HEAD":
        exact = _run(["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"])
        ref = exact.stdout.strip() if exact.returncode == 0 and exact.stdout.strip() else head
    version_path = root / "VERSION"
    version_file = (
        version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else None
    )

    dirty: list[str] = []
    preserved: list[str] = []
    for line in run_git(root, "status", "--porcelain", "--untracked-files=no").splitlines():
        # Porcelain v1: two status letters, a space, then the path (renames as
        # ``old -> new``; the new name is the one that is modified).
        path = line[3:].split(" -> ")[-1].strip()
        if path in PRESERVED_FILES:
            preserved.append(path)
        else:
            dirty.append(path)
    return CheckoutState(
        root=root,
        head=head,
        ref=ref,
        version_file=version_file,
        dirty_paths=sorted(dirty),
        preserved_modified=sorted(preserved),
    )


def fetch_release_tags(root: Path, remote: str) -> list[ReleaseTag]:
    """Fetch the remote's tags and return the release-shaped ones, ascending."""
    run_git(root, "fetch", "--tags", "--quiet", remote)
    listed = run_git(root, "tag", "--list", "v*")
    return parse_release_tags(listed.splitlines())


def checkout_is_at(root: Path, tag: ReleaseTag) -> bool:
    """Return whether HEAD already is the commit the release tag points at."""
    head = run_git(root, "rev-parse", "HEAD").strip()
    target = run_git(root, "rev-parse", f"{tag.name}^{{commit}}").strip()
    return head == target


def checkout_release(root: Path, tag: ReleaseTag, *, log: Logger = _quiet) -> dict[str, Any]:
    """Detach the checkout onto ``tag`` while preserving the deployment's own files.

    ``cdk.json`` is tracked, but its contents (project name, Regions, feature
    toggles) describe this deployment rather than the release, so its exact
    bytes are captured first, reset so git will switch branches, and written
    back once the release is checked out. Any other local modification aborts
    before the checkout — the operator decides what to do with real work.
    """
    state = inspect_checkout(root)
    if state.dirty_paths:
        raise UpgradeError(
            "The checkout has local modifications that gco upgrade will not touch: "
            + ", ".join(state.dirty_paths)
            + ". Commit or stash them (cdk.json is preserved automatically) and run again."
        )

    snapshots: dict[str, bytes] = {}
    for name in PRESERVED_FILES:
        path = root / name
        if path.is_file():
            snapshots[name] = path.read_bytes()
    if state.preserved_modified:
        # git refuses to switch when a modified tracked file differs between the
        # two commits; the bytes are safe in ``snapshots`` and come right back.
        run_git(root, "checkout", "--quiet", "--", *state.preserved_modified)

    log(f"Checking out {tag.name} (was {state.ref} at {state.head})...")
    run_git(root, "checkout", "--quiet", "--detach", tag.name)

    restored: list[str] = []
    for name, data in snapshots.items():
        path = root / name
        if not path.is_file() or path.read_bytes() != data:
            path.write_bytes(data)
            restored.append(name)
    return {
        "previous_ref": state.ref,
        "previous_head": state.head,
        "checked_out": tag.name,
        "preserved": sorted(snapshots),
        "restored": restored,
    }


# =============================================================================
# The local install (CLI, CDK toolchain, dev container)
# =============================================================================


@dataclass
class InstallProbe:
    """How the running CLI and its toolchain relate to the checkout."""

    cli_version: str
    cli_path: str
    cli_editable_from_checkout: bool
    python: str
    node_toolchain: bool
    container_runtime: str | None
    dev_image: str
    dev_image_present: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _cli_source_root() -> Path:
    """The directory the running ``cli`` package is imported from."""
    return Path(__file__).resolve().parent.parent


def image_exists(runtime: str, image: str) -> bool:
    """Return whether ``image`` is present in the runtime's local image store."""
    result = _run([runtime, "image", "inspect", image], timeout=60)
    return result.returncode == 0


def probe_install(root: Path, *, image: str = DEFAULT_DEV_IMAGE) -> InstallProbe:
    """Describe the running CLI, the checkout's toolchain, and the dev image."""
    runtime = detect_container_runtime()
    return InstallProbe(
        cli_version=__version__,
        cli_path=str(_cli_source_root()),
        cli_editable_from_checkout=_cli_source_root() == root.resolve(),
        python=sys.executable,
        node_toolchain=(root / "node_modules" / ".bin" / "cdk").exists(),
        container_runtime=runtime,
        dev_image=image,
        dev_image_present=bool(runtime) and image_exists(str(runtime), image),
    )


def refresh_python_install(root: Path, *, log: Logger = _quiet) -> dict[str, Any]:
    """Reinstall the checkout into the running interpreter so new pins take effect.

    ``python -m pip`` is tried first; interpreters created by ``uv`` ship
    without pip, so ``uv pip`` is the fallback when ``uv`` is on PATH.
    """
    pip_argv = [sys.executable, "-m", "pip", "install", "--quiet", "--no-input", "-e", str(root)]
    log("Refreshing the editable CLI install (pip install -e .)...")
    result = _run(pip_argv, cwd=root)
    if result.returncode == 0:
        return {"tool": "pip", "argv": pip_argv, "status": "ok"}

    uv = shutil.which("uv")
    if uv is not None:
        uv_argv = [uv, "pip", "install", "--quiet", "--python", sys.executable, "-e", str(root)]
        log("pip is unavailable in this interpreter; retrying with uv pip...")
        uv_result = _run(uv_argv, cwd=root)
        if uv_result.returncode == 0:
            return {"tool": "uv", "argv": uv_argv, "status": "ok"}
        result = uv_result
    detail = (result.stderr or result.stdout).strip()[-800:]
    raise UpgradeError(
        "Could not refresh the editable CLI install after checking out the release; the "
        "deploy would run the new app against old dependencies. Run "
        f"'{sys.executable} -m pip install -e {root}' (or 'uv pip install -e .') yourself, "
        f"then rerun 'gco upgrade --skip-checkout'. Installer output:\n{detail}"
    )


def refresh_node_toolchain(root: Path, *, log: Logger = _quiet) -> dict[str, Any]:
    """Reinstall the checkout-local CDK toolchain from the release's lockfile (best effort)."""
    npm = shutil.which("npm")
    if npm is None:
        return {"status": "skipped", "reason": "npm is not on PATH"}
    argv = [npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"]
    log("Refreshing the checkout's CDK toolchain (npm ci)...")
    result = _run(argv, cwd=root)
    if result.returncode == 0:
        return {"status": "ok", "argv": argv}
    detail = (result.stderr or result.stdout).strip()[-400:]
    return {
        "status": "warning",
        "argv": argv,
        "message": (
            "npm ci failed; the previously installed CDK CLI stays in place. If 'cdk' "
            f"versions now mismatch, run 'npm ci' in {root} by hand. Output:\n{detail}"
        ),
    }


def rebuild_dev_image(
    root: Path,
    *,
    runtime: str,
    image: str = DEFAULT_DEV_IMAGE,
    log: Logger = _quiet,
) -> dict[str, Any]:
    """Rebuild the dev-container image from the release's Dockerfile.dev (best effort).

    Mirrors ``scripts/setup-dev-alias.sh``'s build step. A failure is reported
    rather than fatal: the stack cycle does not depend on the image, and the
    setup script can be rerun to retry with its own registry retries.
    """
    dockerfile = root / "Dockerfile.dev"
    if not dockerfile.is_file():
        return {"status": "skipped", "reason": "Dockerfile.dev is not part of this release"}
    argv = [runtime, "build", "-f", str(dockerfile), "-t", image, str(root)]
    log(f"Rebuilding the {image} container image with {runtime} (this can take a few minutes)...")
    result = _run(argv, cwd=root, timeout=3600)
    if result.returncode == 0:
        return {"status": "ok", "image": image, "runtime": runtime}
    detail = (result.stderr or result.stdout).strip()[-400:]
    return {
        "status": "warning",
        "image": image,
        "runtime": runtime,
        "message": (
            f"{runtime} could not rebuild {image}; the current image keeps running the "
            "previous release until you rerun './scripts/setup-dev-alias.sh'. "
            f"Output:\n{detail}"
        ),
    }


# =============================================================================
# The plan and the stack cycle
# =============================================================================


@dataclass
class UpgradePlan:
    """Everything the confirmation prompt shows and the JSON document reports."""

    current_version: str
    target: ReleaseTag
    latest: ReleaseTag
    checkout: CheckoutState
    install: InstallProbe
    already_at_target: bool
    control_plane_stacks: list[str]
    workload_stacks: list[str]
    remote: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_version": self.current_version,
            "checkout_version": self.checkout.version_file,
            "target": self.target.name,
            "latest": self.latest.name,
            "remote": self.remote,
            "already_at_target": self.already_at_target,
            "checkout": self.checkout.to_dict(),
            "install": self.install.to_dict(),
            "control_plane_stacks": list(self.control_plane_stacks),
            "workload_stacks": list(self.workload_stacks),
        }


def split_stacks(stacks: Sequence[str], project_name: str) -> tuple[list[str], list[str]]:
    """Return ``(control_plane, workload)`` in the order the upgrade touches them."""
    from .stacks import get_stack_destroy_order

    workload = get_stack_destroy_order(
        list(stacks), project_name=project_name, keep_control_plane=True
    )
    control_plane = [stack for stack in stacks if stack not in set(workload)]
    return control_plane, workload


def build_plan(
    root: Path,
    *,
    project_name: str,
    stacks: Sequence[str],
    remote: str = DEFAULT_REMOTE,
    ref: str | None = None,
    image: str = DEFAULT_DEV_IMAGE,
    skip_fetch: bool = False,
) -> UpgradePlan:
    """Inspect the checkout, fetch the release tags, and lay out the upgrade."""
    checkout = inspect_checkout(root)
    if skip_fetch:
        tags = parse_release_tags(run_git(root, "tag", "--list", "v*").splitlines())
    else:
        tags = fetch_release_tags(root, remote)
    target = resolve_target(tags, ref)
    latest = latest_release(tags)
    control_plane, workload = split_stacks(stacks, project_name)
    return UpgradePlan(
        current_version=__version__,
        target=target,
        latest=latest,
        checkout=checkout,
        install=probe_install(root, image=image),
        already_at_target=checkout_is_at(root, target),
        control_plane_stacks=control_plane,
        workload_stacks=workload,
        remote=remote,
    )


def require_container_runtime() -> str:
    """Deploys build Lambda assets and mirror images; refuse early when nothing can."""
    runtime = detect_container_runtime()
    if not runtime:
        raise UpgradeError(container_runtime_error_message())
    return runtime


@dataclass
class StackCycleResult:
    """Outcome of the teardown/redeploy half of the upgrade."""

    destroyed: list[str] = field(default_factory=list)
    deployed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    teardown_attempts: int = 0
    phase_failed: str | None = None

    @property
    def ok(self) -> bool:
        return self.phase_failed is None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def run_stack_cycle(
    manager: Any,
    *,
    parallel: bool = False,
    max_workers: int = 4,
    on_stack_start: Callable[[str], None] | None = None,
    on_stack_complete: Callable[[str, bool], None] | None = None,
    log: Logger = _quiet,
    sleep: Callable[[float], None] | None = None,
    max_attempts: int = 3,
    retry_wait_seconds: float = 30.0,
) -> StackCycleResult:
    """Scale the workload tier to zero, then deploy-all from the new release.

    The teardown uses the same retry loop as ``gco stacks destroy-all``: a
    failed attempt clears the orphaned network interfaces that typically block
    VPC deletion, waits, and tries again. The redeploy is a single
    ``deploy_orchestrated`` pass — global and API Gateway stacks update in
    place, regional stacks and their bridges are recreated, monitoring is
    updated back to the full topology.
    """
    import time

    wait = sleep or time.sleep
    result = StackCycleResult()

    log("Phase 1/2: scaling the workload tier to zero (control plane stays)...")
    ok = False
    failed: list[str] = []
    for attempt in range(1, max_attempts + 1):
        result.teardown_attempts = attempt
        if attempt > 1:
            log("Clearing orphaned network interfaces that can block VPC deletion...")
            manager.cleanup_orphaned_network_interfaces()
            log(f"Teardown attempt {attempt}/{max_attempts} in {retry_wait_seconds:.0f}s...")
            wait(retry_wait_seconds)
        ok, destroyed, failed = manager.destroy_orchestrated(
            force=True,
            keep_control_plane=True,
            parallel=parallel,
            max_workers=max_workers,
            on_stack_start=on_stack_start,
            on_stack_complete=on_stack_complete,
        )
        for stack in destroyed:
            if stack not in result.destroyed:
                result.destroyed.append(stack)
        if ok:
            break
    if not ok:
        result.failed = list(failed)
        result.phase_failed = "teardown"
        return result

    log("Phase 2/2: updating the control plane in place and recreating the regional stacks...")
    ok, deployed, failed = manager.deploy_orchestrated(
        require_approval=False,
        parallel=parallel,
        max_workers=max_workers,
        on_stack_start=on_stack_start,
        on_stack_complete=on_stack_complete,
    )
    result.deployed = list(deployed)
    if not ok:
        result.failed = list(failed)
        result.phase_failed = "deploy"
    return result
