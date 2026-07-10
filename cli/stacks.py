"""
Stack management for GCO CLI.

Provides commands for deploying, updating, and managing CDK stacks.
This is the largest CLI module (~1600 lines) because it orchestrates the
full deployment lifecycle including container runtime detection, CDK
bootstrapping, Lambda source synchronization, and parallel regional deploys.

This module handles:
    - Container runtime detection (Docker, Finch, Podman) with automatic fallback
    - CDK bootstrap across all target regions (idempotent)
    - Lambda source synchronization (copies handler code + dependencies before synth)
    - CDK stack deployment with proper dependency ordering:
        1. Global stack (Global Accelerator, DynamoDB)
        2. API Gateway stack (auth secret, Lambda proxy)
        3. Regional stacks in parallel (EKS, VPC, ALB per region)
        4. Monitoring stack (CloudWatch dashboards, alarms)
    - Parallel deployment of regional stacks via ThreadPoolExecutor
    - Stack destruction in reverse dependency order
    - FSx for Lustre enable/disable toggle
    - kubectl access configuration (EKS access entries + kubeconfig)

Key Design Decisions:
    - Regional stacks deploy in parallel for speed; global/API/monitoring are sequential
    - Lambda build directories are synced before every deploy to avoid stale code
    - Container runtime is auto-detected; CDK_DOCKER env var overrides
    - All destructive operations require -y/--yes confirmation
    - Stack status is read from CloudFormation, not cached locally

Environment Variables:
    CDK_DOCKER: Override container runtime (default: auto-detect Docker/Finch/Podman)
    AWS_REGION: Default region for single-region operations
"""

from __future__ import annotations

import logging
import os
import shutil
import site
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Flowchart(s) generated from this file:
#   * ``StackManager.deploy_orchestrated`` -> ``diagrams/code_diagrams/cli/stacks.StackManager_deploy_orchestrated.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager_deploy_orchestrated.png``)
#   * ``StackManager.destroy_orchestrated`` -> ``diagrams/code_diagrams/cli/stacks.StackManager_destroy_orchestrated.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager_destroy_orchestrated.png``)
#   * ``StackManager._mirror_images_if_enabled`` -> ``diagrams/code_diagrams/cli/stacks.StackManager__mirror_images_if_enabled.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager__mirror_images_if_enabled.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


if TYPE_CHECKING:
    from .config import GCOConfig

logger = logging.getLogger(__name__)


@dataclass
class StackInfo:
    """Information about a CDK stack."""

    name: str
    status: str
    region: str
    created_time: datetime | None = None
    updated_time: datetime | None = None
    outputs: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "region": self.region,
            "created_time": self.created_time.isoformat() if self.created_time else None,
            "updated_time": self.updated_time.isoformat() if self.updated_time else None,
            "outputs": self.outputs,
            "tags": self.tags,
        }


def _safe_rmtree(path: Path) -> None:
    """Remove a directory tree, handling broken symlinks on macOS.

    shutil.rmtree can fail with ``OSError: [Errno 66] Directory not empty``
    on macOS when pip-installed packages (e.g. botocore) contain broken
    symlinks or extended-attribute resource forks.

    Falls back to ``rm -rf`` via subprocess, but only after validating the
    path is a real directory under the project tree to avoid accidents.
    """
    resolved = path.resolve()

    # Safety: refuse to remove anything that isn't clearly a build artifact
    # inside the project.  The path must contain "lambda" and end with "-build".
    if "lambda" not in resolved.parts or not resolved.name.endswith("-build"):
        raise ValueError(f"Refusing to remove unexpected path: {resolved}")

    try:
        shutil.rmtree(str(resolved))
    except OSError:
        subprocess.run(["rm", "-rf", "--", str(resolved)], check=True)


# Container runtime detection lives in cli/_container_runtime.py so it can
# be shared between StackManager (CDK asset bundling) and ImageManager
# (gco images build/push). The uncached probe is imported from there;
# this module keeps its own small cache so existing tests that reset
# ``cli.stacks._container_runtime_cache`` continue to work without
# touching the new module's cache.
from cli._container_runtime import (  # noqa: E402
    _detect_container_runtime_uncached,
)

# Cached result for container runtime detection (None = not yet checked)
_container_runtime_cache: str | None = None
_container_runtime_checked: bool = False


def _detect_container_runtime() -> str | None:
    """
    Detect available container runtime for CDK asset bundling.

    Thin caching wrapper around the shared
    ``cli._container_runtime._detect_container_runtime_uncached`` probe.
    The cache state is held on this module so tests that patch or reset
    ``cli.stacks._container_runtime_cache`` keep working unchanged.
    """
    global _container_runtime_cache, _container_runtime_checked
    if _container_runtime_checked:
        return _container_runtime_cache

    _container_runtime_cache = _detect_container_runtime_uncached()
    _container_runtime_checked = True
    return _container_runtime_cache


class StackManager:
    """Manages CDK stack operations."""

    def __init__(self, config: GCOConfig, project_root: Path | None = None):
        self.config = config
        self.project_root = project_root or self._find_project_root()
        self._cdk_path = self._find_cdk()

        # Ensure Lambda build directory exists before any CDK synth
        self._ensure_lambda_build()

    def _find_project_root(self) -> Path:
        """Find the project root by looking for cdk.json."""
        current = Path.cwd()
        for parent in [current] + list(current.parents):
            if (parent / "cdk.json").exists():
                return parent
        return current

    def _find_cdk(self) -> str:
        """Find CDK executable."""
        # Check if cdk is in PATH
        try:
            result = subprocess.run(["which", "cdk"], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass

        # Check common locations
        for path in ["/usr/local/bin/cdk", "~/.npm-global/bin/cdk"]:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        # Fall back to npx
        return "npx cdk"

    def _ensure_lambda_build(self) -> None:
        """Ensure the Lambda build directories exist for CDK synthesis.

        Called from __init__ so the directory is ready before any CDK synth
        (even ``cdk list`` needs it because ``app.py`` references the asset).

        This does a lightweight check — only builds if the directory is
        missing or incomplete. It does NOT do a full rebuild (no rmtree).
        The full rebuild happens in ``_rebuild_lambda_packages()``, which
        is called by ``deploy()`` before the actual CDK deploy.
        """
        kubectl_source = self.project_root / "lambda" / "kubectl-applier-simple"
        kubectl_build = self.project_root / "lambda" / "kubectl-applier-simple-build"
        helm_source = self.project_root / "lambda" / "helm-installer"
        helm_build = self.project_root / "lambda" / "helm-installer-build"

        # Only build if the directory is missing or deps aren't installed
        if kubectl_source.exists() and (
            not kubectl_build.exists() or not (kubectl_build / "yaml").exists()
        ):
            self._build_kubectl_lambda()

        if helm_source.exists() and not helm_build.exists():
            self._build_helm_installer_lambda()

    def _check_and_fix_stuck_stack(self, stack_name: str) -> None:
        """Check if a stack is in a stuck state and auto-recover.

        Stacks can get stuck in REVIEW_IN_PROGRESS, ROLLBACK_COMPLETE, or
        other non-deployable states. This detects those and cleans up so
        the next deploy can succeed.
        """
        import boto3

        region = self._get_deploy_region(stack_name)
        if not region:
            return

        try:
            cfn = boto3.client("cloudformation", region_name=region)
            response = cfn.describe_stacks(StackName=stack_name)
            status = response["Stacks"][0]["StackStatus"]

            stuck_states = {
                "REVIEW_IN_PROGRESS",
                "ROLLBACK_COMPLETE",
                "ROLLBACK_FAILED",
                "CREATE_FAILED",
                "DELETE_FAILED",
            }

            if status in stuck_states:
                print(f"  Stack {stack_name} is in {status} state, cleaning up...")
                cfn.delete_stack(StackName=stack_name)
                waiter = cfn.get_waiter("stack_delete_complete")
                waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
                print(f"  Stack {stack_name} cleaned up, will recreate on deploy")

        except Exception as e:
            logger.debug("Stack pre-check for %s: %s", stack_name, e)
            # Stack doesn't exist or can't be described — fine, deploy will create it

    def _diagnose_deploy_failure(self, stack_name: str) -> None:
        """Fetch CloudFormation events after a failed deploy and print diagnostics.

        Gives users actionable information instead of just the CDK error message.
        """
        import boto3

        region = self._get_deploy_region(stack_name)
        if not region:
            return

        try:
            cfn = boto3.client("cloudformation", region_name=region)

            # Get recent events
            response = cfn.describe_stack_events(StackName=stack_name)
            events = response.get("StackEvents", [])

            # Filter to failed events
            failed = [
                e
                for e in events[:20]
                if "FAILED" in e.get("ResourceStatus", "")
                or "ROLLBACK" in e.get("ResourceStatus", "")
            ]

            if failed:
                print(f"\n  CloudFormation failure details for {stack_name}:")
                for event in failed[:5]:
                    resource = event.get("LogicalResourceId", "unknown")
                    status = event.get("ResourceStatus", "unknown")
                    reason = event.get("ResourceStatusReason", "no reason given")
                    print(f"    {resource}: {status}")
                    print(f"      {reason}")

            # Check stack status for actionable advice
            try:
                stack_resp = cfn.describe_stacks(StackName=stack_name)
                status = stack_resp["Stacks"][0]["StackStatus"]

                advice = {
                    "REVIEW_IN_PROGRESS": (
                        "Stack is stuck in REVIEW_IN_PROGRESS. "
                        "Run: aws cloudformation delete-stack "
                        f"--stack-name {stack_name} --region {region}"
                    ),
                    "ROLLBACK_COMPLETE": (
                        "Stack rolled back. Delete it and retry: "
                        f"aws cloudformation delete-stack "
                        f"--stack-name {stack_name} --region {region}"
                    ),
                    "ROLLBACK_FAILED": (
                        "Stack rollback failed. Delete with --retain: "
                        f"aws cloudformation delete-stack "
                        f"--stack-name {stack_name} --region {region}"
                    ),
                    "UPDATE_ROLLBACK_COMPLETE": (
                        "Update rolled back but stack is stable. "
                        "Check the events above and retry the deploy."
                    ),
                }

                if status in advice:
                    print(f"\n  Suggested fix: {advice[status]}")

            except Exception as e:
                logger.debug("Failed to parse stack events: %s", e)

        except Exception as e:
            logger.debug("Failed to diagnose deploy failure for %s: %s", stack_name, e)
            # Best effort — don't fail the deploy further

    def _sync_lambda_sources(self) -> None:
        """
        Sync latest handler code and manifests into the build directory.

        Called at the start of ``deploy()`` to ensure CDK picks up the
        latest source changes. Only copies files — does NOT rebuild pip
        deps or rmtree. Runs once per StackManager instance.
        """
        if getattr(self, "_lambda_sources_synced", False):
            return
        self._lambda_sources_synced = True

        source_dir = self.project_root / "lambda" / "kubectl-applier-simple"
        build_dir = self.project_root / "lambda" / "kubectl-applier-simple-build"

        if not source_dir.exists() or not build_dir.exists():
            return

        # Sync handler.py
        source_handler = source_dir / "handler.py"
        build_handler = build_dir / "handler.py"
        if source_handler.exists():
            shutil.copy2(source_handler, build_handler)

        # Sync manifests directory
        source_manifests = source_dir / "manifests"
        build_manifests = build_dir / "manifests"
        if source_manifests.exists():
            build_manifests.mkdir(parents=True, exist_ok=True)
            for manifest_file in source_manifests.glob("*.yaml"):
                shutil.copy2(manifest_file, build_manifests / manifest_file.name)

    def _rebuild_lambda_packages(self) -> None:
        """Full rebuild of Lambda packages for deploy.

        Nukes the build directories and recreates them from scratch with
        fresh pip deps, handler code, and manifests. Called once at the
        start of a deploy to ensure CDK picks up all changes.

        Runs once per StackManager instance.
        """
        if getattr(self, "_lambda_packages_rebuilt", False):
            return
        self._lambda_packages_rebuilt = True
        self._build_lambda_packages()

    def _build_lambda_packages(self) -> None:
        """Build Lambda packages for kubectl-applier and helm-installer.

        Creates build directories with fresh copies of handler code, manifests,
        charts config, and pip dependencies. This ensures CDK always picks up
        the latest content regardless of Docker/asset caching.
        """
        self._build_kubectl_lambda()
        self._build_helm_installer_lambda()

    def _build_kubectl_lambda(self) -> None:
        """Build the kubectl-applier-simple Lambda package."""
        source_dir = self.project_root / "lambda" / "kubectl-applier-simple"
        build_dir = self.project_root / "lambda" / "kubectl-applier-simple-build"
        requirements = source_dir / "requirements.txt"

        if not source_dir.exists() or not requirements.exists():
            return

        print("  Building kubectl-applier-simple Lambda package...")

        # Clean stale build directory to avoid broken symlinks from previous
        # pip installs (botocore/data/ is a common source of dangling symlinks
        # that cause CDK's asset fingerprinting to fail with ENOENT).
        if build_dir.exists():
            _safe_rmtree(build_dir)

        # Create build directory
        build_dir.mkdir(parents=True, exist_ok=True)

        # Copy handler and manifests (always overwrite to prevent stale content)
        shutil.copy2(source_dir / "handler.py", build_dir / "handler.py")
        build_manifests = build_dir / "manifests"
        if (source_dir / "manifests").exists():
            if build_manifests.exists():
                shutil.rmtree(build_manifests)
            shutil.copytree(source_dir / "manifests", build_manifests, dirs_exist_ok=True)

        # Install pip dependencies for Lambda runtime
        import sys

        result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - static list: sys.executable + pip args + Path objects, no user input
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                str(requirements),
                "-t",
                str(build_dir),
                "--upgrade",
                "--platform",
                "manylinux2014_x86_64",
                "--only-binary=:all:",
                "--quiet",
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  Warning: pip install failed: {result.stderr[:200]}")
        else:
            print("  Lambda package built successfully")

    def _build_helm_installer_lambda(self) -> None:
        """Build the helm-installer Lambda Docker context.

        Copies all source files into a clean build directory so CDK always
        detects changes to charts.yaml, handler.py, Dockerfile, etc.
        """
        source_dir = self.project_root / "lambda" / "helm-installer"
        build_dir = self.project_root / "lambda" / "helm-installer-build"

        if not source_dir.exists():
            return

        print("  Building helm-installer Lambda package...")

        # Clean and recreate build directory
        if build_dir.exists():
            _safe_rmtree(build_dir)
        build_dir.mkdir(parents=True, exist_ok=True)

        # Copy all source files into the build directory
        for item in source_dir.iterdir():
            if item.name == "__pycache__":
                continue
            if item.is_file():
                shutil.copy2(item, build_dir / item.name)
            elif item.is_dir():
                shutil.copytree(item, build_dir / item.name, dirs_exist_ok=True)

        print("  Helm installer package built successfully")

    def _get_python_path(self) -> str:
        """
        Get PYTHONPATH that includes the current Python's site-packages.

        This is critical for pipx installations where CDK runs `python3 app.py`
        using the system Python, which doesn't have aws_cdk installed.
        By setting PYTHONPATH, we ensure CDK's subprocess can find our modules.
        """
        # Get all site-packages directories from the current Python
        site_packages = site.getsitepackages()

        # Also include user site-packages if available
        user_site = site.getusersitepackages()
        if user_site and os.path.isdir(user_site):
            site_packages.append(user_site)

        # Include the directory containing the current module (for editable installs)
        current_module_dir = Path(__file__).parent.parent
        if current_module_dir.exists():
            site_packages.append(str(current_module_dir))

        # Combine with existing PYTHONPATH if any
        existing_path = os.environ.get("PYTHONPATH", "")
        all_paths = site_packages + ([existing_path] if existing_path else [])

        return os.pathsep.join(all_paths)

    def _run_cdk(
        self,
        command: list[str],
        capture_output: bool = False,
        env: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a CDK command.

        Args:
            command: CDK subcommand argv (e.g. ``["destroy", "gco-us-east-1", "--force"]``).
            capture_output: Capture stdout / stderr instead of streaming.
            env: Extra env vars merged onto the parent process environment.
            timeout: Wall-clock timeout in seconds. ``None`` (default) waits
                forever — preserving the old behaviour for ``synth`` / ``list``.
                When set, on timeout we send SIGTERM, give the CDK process up
                to 30 seconds to exit cleanly, then SIGKILL, and finally
                re-raise ``subprocess.TimeoutExpired`` so callers can decide
                how to handle a hung subprocess. ``deploy()`` and ``destroy()``
                pass a per-stack budget so a wedged ``cdk destroy`` (e.g. its
                post-delete polling loop hanging after CloudFormation has
                already finished) can't block the orchestrator forever.
        """
        full_env = os.environ.copy()

        # Inject PYTHONPATH so CDK's python3 subprocess can find aws_cdk
        # This is essential for pipx installations
        full_env["PYTHONPATH"] = self._get_python_path()

        if env:
            full_env.update(env)

        cdk_cmd = self._cdk_path.split() + command

        try:
            if capture_output:
                return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - cdk_cmd is a list of static CDK subcommands, no user-controlled shell injection
                    cdk_cmd,
                    cwd=self.project_root,
                    capture_output=True,
                    text=True,
                    env=full_env,
                    timeout=timeout,
                )
            return subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - cdk_cmd is a list of static CDK subcommands, no user-controlled shell injection
                cdk_cmd,
                cwd=self.project_root,
                env=full_env,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            # subprocess.run already sent SIGKILL and reaped the child by
            # the time TimeoutExpired propagates. Surface the timeout so
            # callers (deploy / destroy) can verify post-state via CFN.
            logger.warning(
                "cdk command timed out after %ss: %s",
                timeout,
                " ".join(cdk_cmd),
            )
            raise

    def list_stacks(self) -> list[str]:
        """List all available CDK stacks."""
        result = self._run_cdk(["list"], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to list stacks: {result.stderr}")
        return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]

    def synth(self, stack_name: str | None = None, quiet: bool = True) -> str:
        """Synthesize CloudFormation templates."""
        cmd = ["synth"]
        if stack_name:
            cmd.append(stack_name)
        if quiet:
            cmd.append("--quiet")

        result = self._run_cdk(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"CDK synth failed: {result.stderr}")
        return str(result.stdout)

    def diff(self, stack_name: str | None = None) -> str:
        """Show diff between deployed and local stacks."""
        cmd = ["diff", "--no-color"]
        if stack_name:
            cmd.append(stack_name)

        result = self._run_cdk(cmd, capture_output=True)
        # diff returns non-zero if there are differences, which is expected
        return str(result.stdout or result.stderr)

    def deploy(
        self,
        stack_name: str | None = None,
        require_approval: bool = True,
        all_stacks: bool = False,
        outputs_file: str | None = None,
        parameters: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
        progress: str = "events",
        output_dir: str | None = None,
        exclusively: bool = False,
    ) -> bool:
        """Deploy CDK stacks.

        Args:
            stack_name: Name of the stack to deploy
            require_approval: Whether to require approval for changes
            all_stacks: Deploy all stacks
            outputs_file: File to write outputs to
            parameters: CDK parameters
            tags: Tags to apply to stacks
            progress: Progress display type
            output_dir: Custom CDK output directory (for parallel deployments)
            exclusively: Pass ``--exclusively`` to CDK so only the named
                stack is evaluated, not its transitive dependencies. Used by
                ``deploy_orchestrated`` once earlier phases have already
                deployed the globals — re-synthesizing them every phase
                forces custom resources (notably KubectlApplyManifests)
                to re-run each time, adding minutes per phase for no
                actual change.
        """
        # Full rebuild of Lambda packages on first deploy call (once per session),
        # then sync latest source on subsequent calls
        self._rebuild_lambda_packages()
        self._sync_lambda_sources()

        # Check for stuck stacks and auto-recover
        if stack_name:
            self._check_and_fix_stuck_stack(stack_name)

        # Ensure container runtime is available for building images
        runtime = _detect_container_runtime()
        if not runtime:
            raise RuntimeError(
                "No container runtime found. Please install Docker, Finch, or Podman.\n"
                "  - Docker: https://docs.docker.com/get-docker/\n"
                "  - Finch: brew install finch && finch vm init\n"
                "  - Podman: https://podman.io/getting-started/installation"
            )

        # Mirror third-party images into ECR before the regional stack's Helm
        # install runs (see volcano_image_mirror in cdk.json). Runs only for
        # regional stacks, only when enabled, and skips images already present —
        # so a fresh deploy seeds the mirror automatically and repeat deploys are
        # a no-op. Raises before any CDK call if mirroring fails, so we never
        # deploy a config whose images aren't in place.
        self._mirror_images_if_enabled(stack_name=stack_name, all_stacks=all_stacks)

        # Auto-bootstrap the target region if needed
        if stack_name:
            region = self._get_deploy_region(stack_name)
            if region and not self.ensure_bootstrapped(region):
                raise RuntimeError(
                    f"Region {region} could not be bootstrapped. "
                    "Run 'gco stacks bootstrap --region "
                    f"{region}' manually to diagnose."
                )

        cmd = ["deploy"]

        if all_stacks:
            cmd.append("--all")
        elif stack_name:
            cmd.append(stack_name)

        # --exclusively tells CDK to deploy *only* the named stack, not its
        # transitive dependencies. deploy_orchestrated sets this once the
        # earlier phases (global, api-gateway) are already in place so that
        # the regional and monitoring phases don't re-synthesize and
        # re-evaluate globals on every pass.
        if exclusively and stack_name and not all_stacks:
            cmd.append("--exclusively")

        if not require_approval:
            cmd.extend(["--require-approval", "never"])

        if outputs_file:
            cmd.extend(["--outputs-file", outputs_file])

        if parameters:
            for key, value in parameters.items():
                cmd.extend(["--parameters", f"{key}={value}"])

        if tags:
            for key, value in tags.items():
                cmd.extend(["--tags", f"{key}={value}"])

        cmd.extend(["--progress", progress])

        # Use custom output directory for parallel deployments
        if output_dir:
            cmd.extend(["--output", output_dir])

        # Set CDK_DOCKER env var if not already set
        env = {"CDK_DOCKER": runtime} if not os.environ.get("CDK_DOCKER") else None

        # Per-stack wall-clock cap so a wedged ``cdk deploy`` (e.g. an
        # IAM eventual-consistency wait that never completes) can't block
        # the orchestrator forever. Default 60 minutes — long enough for
        # a fresh EKS cluster cold start. Override via
        # GCO_CDK_DEPLOY_TIMEOUT_SECONDS.
        timeout_s = float(os.environ.get("GCO_CDK_DEPLOY_TIMEOUT_SECONDS", "3600"))

        try:
            result = self._run_cdk(cmd, env=env, timeout=timeout_s)
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            print(
                f"  cdk deploy timed out after {timeout_s}s for "
                f"{stack_name or 'all stacks'}; verifying CloudFormation state..."
            )
            success = False

        # Reconcile against CloudFormation. If the stack is in a terminal
        # CREATE/UPDATE_COMPLETE state, the deploy actually succeeded
        # despite cdk's exit code or timeout.
        if stack_name and not all_stacks and not success:
            cfn_status = self._get_stack_status(stack_name)
            # cdk may have died on a transient client-side error (e.g. a
            # ``read EADDRNOTAVAIL`` socket failure) while CloudFormation kept
            # working. If the stack is still mid-operation, wait for it to
            # settle to a terminal state before judging — a status read taken
            # the instant cdk exits can otherwise catch a stack seconds before
            # it reaches CREATE_COMPLETE and report a false failure.
            if cfn_status is not None and cfn_status.endswith("_IN_PROGRESS"):
                print(
                    f"  cdk exited non-zero but {stack_name} is {cfn_status} in "
                    "CloudFormation; waiting for the operation to settle..."
                )
                cfn_status = self._wait_for_stack_settle(stack_name)
            if cfn_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                print(
                    f"  cdk reported a non-zero exit but {stack_name} is in "
                    f"{cfn_status} in CloudFormation — treating as success."
                )
                success = True

        # Conversely, when cdk reports success, confirm CloudFormation actually
        # landed in a terminal success state. A zero cdk exit can still mask a
        # stack that silently rolled back (e.g. UPDATE_ROLLBACK_COMPLETE) or is
        # otherwise not in a healthy COMPLETE state — verifying the AWS-side
        # truth keeps deploy() from reporting a rolled-back stack as deployed.
        # A None status (lookup failed / transient) leaves cdk's verdict intact;
        # we only override on a *known* non-success state. No-op deploys stay in
        # CREATE_COMPLETE/UPDATE_COMPLETE, so this never false-fails them — we
        # deliberately don't require LastUpdatedTime to advance.
        if success and stack_name and not all_stacks:
            cfn_status = self._get_stack_status(stack_name)
            if cfn_status is not None and cfn_status not in (
                "CREATE_COMPLETE",
                "UPDATE_COMPLETE",
            ):
                print(
                    f"  cdk reported success but {stack_name} is in {cfn_status} "
                    f"in CloudFormation — treating as a failed deploy."
                )
                success = False

        if not success and stack_name:
            self._diagnose_deploy_failure(stack_name)

        # After deploying gco-analytics, automatically redeploy
        # gco-api-gateway to wire in the /studio/* routes (the API gateway
        # imports the Cognito pool ARN and presigned-URL Lambda ARN from
        # the analytics stack).
        if success and stack_name and "analytics" in stack_name and not all_stacks:
            print("  Updating gco-api-gateway with analytics routes...")
            self.deploy(
                stack_name="gco-api-gateway",
                require_approval=require_approval,
                exclusively=True,
            )

        return success

    def destroy(
        self,
        stack_name: str | None = None,
        all_stacks: bool = False,
        force: bool = False,
        output_dir: str | None = None,
    ) -> bool:
        """Destroy CDK stacks.

        If the target stack exists in CloudFormation but isn't in the CDK
        app (e.g. because a toggle was disabled), temporarily enables the
        toggle so CDK can synthesize and destroy the stack properly. This
        ensures custom resource cleanup handlers (like the analytics
        cleanup Lambda) fire during deletion.

        Args:
            stack_name: Name of the stack to destroy
            all_stacks: Destroy all stacks
            force: Skip confirmation prompts
            output_dir: Custom CDK output directory (for parallel deployments)
        """
        # Image-registry pre-destroy guards. Only fires for the global
        # stack (where the registry lives) and only when the operator
        # has explicitly chosen ``removal_policy: "destroy"``. The
        # default ``retain`` posture is a no-op here. See
        # ``_image_registry_destroy_preflight`` for the exact rules.
        if (
            stack_name == "gco-global"
            and not all_stacks
            and not self._image_registry_destroy_preflight(force=force)
        ):
            return False

        # If destroying a specific stack that exists in CloudFormation but
        # might not be in the CDK app, temporarily enable its toggle.
        toggle_restored = False
        if (
            stack_name
            and not all_stacks
            and "analytics" in stack_name
            and self._stack_exists_in_cloudformation(stack_name)
        ):
            toggle_restored = self._ensure_analytics_enabled_for_destroy()

        # The analytics stack exports values (e.g. Cognito pool ARN) that
        # gco-api-gateway imports. CloudFormation blocks deletion of stacks
        # with consumed exports. To break the dependency, redeploy the API
        # gateway with analytics disabled first, then destroy analytics.
        if stack_name and not all_stacks and "analytics" in stack_name:
            safe_to_destroy = self._remove_api_gateway_analytics_dependency()
            if not safe_to_destroy:
                # Restore analytics toggle before bailing out.
                if toggle_restored:
                    self._restore_analytics_disabled()
                print(
                    "  Aborting gco-analytics destroy: gco-api-gateway still "
                    "imports analytics exports. Fix the API gateway and retry."
                )
                return False

        cmd = ["destroy"]

        if all_stacks:
            cmd.append("--all")
        elif stack_name:
            cmd.append(stack_name)
            # --exclusively prevents CDK from cascading the destroy to
            # dependent stacks (e.g. destroying gco-analytics should not
            # also destroy gco-api-gateway just because it references the
            # presigned-URL Lambda ARN).
            cmd.append("--exclusively")

        if force:
            cmd.append("--force")

        if output_dir:
            cmd.extend(["--output", output_dir])

        # Per-stack wall-clock cap so a wedged ``cdk destroy`` (its
        # post-delete polling loop hanging after CloudFormation has
        # already finished) can't block the orchestrator forever. Default
        # 45 minutes — enough for an EKS regional teardown which can take
        # 25-30 minutes when SG / ENI cleanup serialises. Override via
        # GCO_CDK_DESTROY_TIMEOUT_SECONDS.
        timeout_s = float(os.environ.get("GCO_CDK_DESTROY_TIMEOUT_SECONDS", "2700"))

        try:
            result = self._run_cdk(cmd, timeout=timeout_s)
            cdk_succeeded = result.returncode == 0
        except subprocess.TimeoutExpired:
            # CDK hung. Verify the AWS-side state below — if the stack
            # is gone in CloudFormation, the destroy actually succeeded
            # and the timeout was just CDK's polling loop wedged.
            print(
                f"  cdk destroy timed out after {timeout_s}s; verifying "
                f"CloudFormation state for {stack_name}..."
            )
            cdk_succeeded = False

        # Restore the toggle if we changed it
        if toggle_restored:
            self._restore_analytics_disabled()

        # Reconcile against CloudFormation. Three outcomes:
        # 1. cdk succeeded AND stack is gone → success
        # 2. stack is gone (regardless of cdk return code or timeout) → success
        # 3. stack still exists → fall back to direct CFN delete or surface failure
        if stack_name and not all_stacks:
            still_present = self._stack_exists_in_cloudformation(stack_name)
            if not still_present:
                # Whatever cdk did or didn't do, AWS confirms the stack
                # is gone. Treat as success.
                if not cdk_succeeded:
                    print(
                        f"  cdk reported a non-zero exit but {stack_name} is "
                        f"already deleted in CloudFormation — treating as success."
                    )
                return True
            # Stack still there. If cdk succeeded but CFN still has it,
            # fall back to a direct CFN delete (existing behaviour).
            if cdk_succeeded:
                return self._cloudformation_delete_stack(stack_name)
            # cdk failed AND stack is still present → real failure.
            return False

        return cdk_succeeded

    # ------------------------------------------------------------------
    # Image registry pre-destroy guards
    # ------------------------------------------------------------------
    def _read_images_config(self) -> dict[str, Any]:
        """Read the ``images`` block from cdk.json with defaults applied.

        Mirrors the parser in ``gco/stacks/global_stack.py`` so the CLI
        can reason about the same fields without importing the CDK
        module (which pulls aws_cdk and the full constructs surface).
        Defaults stay aligned with the global-stack parser; any value
        that fails validation (e.g. an unexpected ``removal_policy``)
        is silently coerced to ``"retain"`` here so the CLI never blocks
        on a typo — the actual deploy-time validation is the global
        stack's responsibility.
        """
        import json

        cdk_json_path = _find_cdk_json()
        if not cdk_json_path:
            return {
                "removal_policy": "retain",
                "empty_on_delete": False,
            }
        try:
            with open(cdk_json_path, encoding="utf-8") as f:
                ctx = json.load(f).get("context", {}) or {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read cdk.json for images config: %s", exc)
            return {"removal_policy": "retain", "empty_on_delete": False}

        raw = ctx.get("images") or {}
        removal_policy = str(raw.get("removal_policy", "retain")).strip().lower()
        if removal_policy not in ("retain", "destroy"):
            removal_policy = "retain"
        return {
            "removal_policy": removal_policy,
            "empty_on_delete": bool(raw.get("empty_on_delete", False)),
        }

    def _build_image_registry_inventory(self) -> dict[str, Any]:
        """Aggregate repo / tag / size / reference counts for the registry.

        Returns a dict shape suitable for printing to the operator. Best
        effort: a missing ImageManager dependency or an AWS error
        produces a partially-populated dict rather than raising.
        """
        inventory: dict[str, Any] = {
            "repo_count": 0,
            "tag_count": 0,
            "total_bytes": 0,
            "endpoint_refs": 0,
            "job_refs": 0,
        }
        try:
            from cli.images import ImageManager
        except Exception as exc:  # noqa: BLE001
            logger.debug("ImageManager import failed during preflight: %s", exc)
            return inventory

        try:
            manager = ImageManager(config=self.config)
            repos = manager.list_repos()
            inventory["repo_count"] = len(repos)
            # Repos this deployment owns live under ``<project_name>/`` (#139).
            repo_prefix = f"{self.config.project_name}/"
            for repo in repos:
                repo_name = repo.get("name", "")
                if not repo_name.startswith(repo_prefix):
                    continue
                short = repo_name.removeprefix(repo_prefix)
                try:
                    tags = manager.list_tags(short)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("list_tags failed for %s: %s", repo_name, exc)
                    continue
                inventory["tag_count"] += len(tags)
                for row in tags:
                    size = row.get("size_bytes")
                    if isinstance(size, int):
                        inventory["total_bytes"] += size
            try:
                inventory["endpoint_refs"] = len(manager._collect_inference_image_refs())
            except Exception as exc:  # noqa: BLE001
                logger.debug("inference ref collection failed: %s", exc)
            try:
                inventory["job_refs"] = len(manager._collect_recent_job_image_refs())
            except Exception as exc:  # noqa: BLE001
                logger.debug("job ref collection failed: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Image registry inventory failed: %s", exc)
        return inventory

    def _image_registry_destroy_preflight(self, *, force: bool) -> bool:
        """Validate the image-registry destroy posture before invoking CFN.

        Two rules:

          1. ``removal_policy: "destroy"`` AND ``empty_on_delete: false``
             → refuse with the literal helpful-error message pointing
             the operator at ``gco images cleanup --all`` or at flipping
             ``empty_on_delete: true``.

          2. ``removal_policy: "destroy"`` AND ``empty_on_delete: true``
             → print the inventory summary first. On a TTY the operator
             is also prompted for confirmation; non-TTY runs proceed
             (the operator presumably passed ``-y`` or is automating).

        Returns True when the destroy may proceed, False when it has
        been refused or declined.
        """
        cfg = self._read_images_config()
        if cfg["removal_policy"] != "destroy":
            return True

        if not cfg["empty_on_delete"]:
            print(
                "Repos under gco/* are not empty and empty_on_delete is "
                "false. Run 'gco images cleanup --all' first, or set "
                "images.empty_on_delete: true in cdk.json."
            )
            return False

        inventory = self._build_image_registry_inventory()
        gib = inventory["total_bytes"] / (1024**3) if inventory["total_bytes"] else 0.0
        print("Image registry inventory before destroy:")
        print(f"  repos:            {inventory['repo_count']}")
        print(f"  tags:             {inventory['tag_count']}")
        print(f"  total size:       {gib:.2f} GiB")
        print(f"  referencing endpoints: {inventory['endpoint_refs']}")
        print(f"  recent job refs:  {inventory['job_refs']}")

        # Already confirmed via -y, or non-interactive — proceed.
        if force or not sys.stdin.isatty():
            return True

        try:
            response = input("Destroy gco-global and delete every gco/* repo? [y/N]: ")
        except EOFError, KeyboardInterrupt:
            print("Aborted.")
            return False
        if response.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return False
        return True

    def _stack_exists_in_cloudformation(self, stack_name: str) -> bool:
        """Check whether a stack still exists in CloudFormation.

        Only ``DELETE_COMPLETE`` — or a missing stack, which
        ``describe_stacks`` surfaces as an error — counts as "gone". A stack
        in ``DELETE_FAILED`` or ``DELETE_IN_PROGRESS`` still exists with
        live/partial resources and must report as present.

        The previous ``"DELETE" not in status`` test was too broad: it also
        matched ``DELETE_FAILED`` and ``DELETE_IN_PROGRESS``, so a failed
        ``cdk destroy`` whose stack landed in ``DELETE_FAILED`` was reconciled
        as a success (the CLI printed "destroyed successfully" for a stack
        that was still very much present).
        """
        import boto3

        region = self._get_destroy_region(stack_name)
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            resp = cfn.describe_stacks(StackName=stack_name)
            status = resp["Stacks"][0]["StackStatus"]
            return bool(status != "DELETE_COMPLETE")
        except Exception:
            return False

    def _get_stack_status(self, stack_name: str) -> str | None:
        """Return the live CloudFormation status of ``stack_name`` or None.

        Used by ``deploy()`` to reconcile against AWS-side state when ``cdk
        deploy`` returns a non-zero exit code or times out — if the stack
        actually finished CREATE_COMPLETE or UPDATE_COMPLETE on the AWS
        side, the deploy succeeded regardless of what cdk reported.
        Returns None when the stack does not exist or the lookup itself
        fails (network blip, perms, etc.) so callers can treat the
        unknown case as 'cdk's verdict stands'.
        """
        import boto3

        try:
            region = self._get_destroy_region(stack_name)
            cfn = boto3.client("cloudformation", region_name=region)
            resp = cfn.describe_stacks(StackName=stack_name)
            return str(resp["Stacks"][0]["StackStatus"])
        except Exception:
            return None

    def _wait_for_stack_settle(self, stack_name: str, timeout: float | None = None) -> str | None:
        """Poll CloudFormation until ``stack_name`` leaves a ``*_IN_PROGRESS`` state.

        When ``cdk deploy`` dies on a transient client-side error (e.g. a
        ``read EADDRNOTAVAIL`` socket failure) the CloudFormation operation it
        started usually keeps running server-side. ``deploy()`` reconciles
        against CloudFormation, but a single status read taken the instant cdk
        exits can catch the stack mid-flight (``CREATE_IN_PROGRESS``) and give
        up only seconds before it would have reached ``CREATE_COMPLETE``. This
        helper waits out the in-progress window so the reconcile judges the
        *terminal* state instead of a transient one.

        Returns the terminal status string, the last status seen on timeout, or
        ``None`` if the status could not be read (so callers treat the unknown
        case as 'cdk's verdict stands').
        """
        import time

        if timeout is None:
            timeout = float(os.environ.get("GCO_CDK_SETTLE_TIMEOUT_SECONDS", "1200"))
        deadline = time.monotonic() + timeout
        status = self._get_stack_status(stack_name)
        while status is not None and status.endswith("_IN_PROGRESS"):
            if time.monotonic() >= deadline:
                break
            time.sleep(15.0)
            status = self._get_stack_status(stack_name)
        return status

    def _cloudformation_delete_stack(self, stack_name: str) -> bool:
        """Delete a stack directly via CloudFormation API."""
        import boto3

        region = self._get_destroy_region(stack_name)
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            cfn.delete_stack(StackName=stack_name)
            waiter = cfn.get_waiter("stack_delete_complete")
            waiter.wait(
                StackName=stack_name,
                WaiterConfig={"Delay": 15, "MaxAttempts": 120},
            )
            return True
        except Exception:
            return False

    def _get_destroy_region(self, stack_name: str) -> str:
        """Determine the region for a stack based on its name."""
        try:
            region = self._get_deploy_region(stack_name)
            return region or self.config.api_gateway_region
        except Exception:
            return self.config.api_gateway_region

    def _ensure_analytics_enabled_for_destroy(self) -> bool:
        """Temporarily enable analytics so CDK includes the stack for destroy."""
        try:
            current = get_analytics_config()
            if not current.get("enabled"):
                update_analytics_config({"enabled": True})
                return True
        except Exception as exc:
            logger.debug(
                "Failed to enable analytics toggle for destroy: %s",
                exc,
                exc_info=True,
            )
        return False

    def _restore_analytics_disabled(self) -> None:
        """Restore analytics toggle to disabled after destroy."""
        try:
            update_analytics_config({"enabled": False})
        except Exception as exc:
            logger.warning(
                "Failed to restore analytics toggle to disabled after destroy: %s",
                exc,
                exc_info=True,
            )

    def _remove_api_gateway_analytics_dependency(self) -> bool:
        """Redeploy gco-api-gateway with analytics disabled to drop cross-stack imports.

        The analytics stack exports values (Cognito pool ARN, presigned-URL
        Lambda ARN) that gco-api-gateway imports for the /studio/* routes.
        CloudFormation blocks deletion of stacks with consumed exports. By
        disabling analytics and redeploying the API gateway, the /studio/*
        routes are removed and the imports are dropped, unblocking the
        analytics stack deletion.

        Returns:
            True if the analytics stack is safe to destroy (either because
            no consumer remains or because the redeploy successfully
            dropped the imports). False if a consumer of the analytics
            exports still exists and the analytics destroy will fail.
        """
        # Fast path: if gco-api-gateway doesn't exist (or has already been
        # deleted/rolled-back into a non-consuming state), there's nothing
        # importing the analytics exports. Skip the redeploy entirely.
        if not self._stack_exists_in_cloudformation("gco-api-gateway"):
            logger.info(
                "gco-api-gateway does not exist in CloudFormation; "
                "skipping redeploy before analytics destroy."
            )
            return True

        # Second fast path: if the deployed api-gateway isn't actually
        # importing anything from the analytics stack, we don't need to
        # touch it. This happens when analytics was never fully wired up.
        if not self._api_gateway_imports_from_analytics():
            logger.info(
                "gco-api-gateway does not import any gco-analytics exports; "
                "skipping redeploy before analytics destroy."
            )
            return True

        try:
            # Temporarily disable analytics so CDK drops the /studio/* routes.
            current = get_analytics_config()
            was_enabled = current.get("enabled", False)
            if was_enabled:
                update_analytics_config({"enabled": False})

            print("  Updating gco-api-gateway to remove analytics routes...")
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_out:
                success = self.deploy(
                    stack_name="gco-api-gateway",
                    require_approval=False,
                    exclusively=True,
                    output_dir=tmp_out,
                )

            # Re-enable analytics so CDK can synthesize the analytics stack
            # for the destroy operation (custom resources need to fire).
            if was_enabled:
                update_analytics_config({"enabled": True})

            if not success:
                # The redeploy failed. That's only a real problem if the
                # api-gateway still imports analytics exports. Recheck:
                # the auto-cleanup of ROLLBACK_COMPLETE stacks may have
                # deleted the consumer entirely, in which case the destroy
                # can still proceed.
                if not self._api_gateway_imports_from_analytics():
                    logger.info(
                        "gco-api-gateway redeploy failed, but the stack no "
                        "longer imports analytics exports (likely deleted "
                        "during cleanup). Analytics destroy can proceed."
                    )
                    return True
                logger.error(
                    "Failed to redeploy gco-api-gateway to drop analytics "
                    "imports, and the stack still consumes analytics exports. "
                    "Destroying gco-analytics will fail with "
                    "'Export ... cannot be deleted as it is in use'. Fix "
                    "gco-api-gateway first (see events above) and retry."
                )
                return False

            return True
        except Exception as exc:
            logger.warning(
                "Failed to remove API gateway analytics dependency: %s",
                exc,
                exc_info=True,
            )
            # On unexpected exceptions, recheck whether imports remain.
            # Be permissive only if we can confirm the destroy is safe.
            try:
                return not self._api_gateway_imports_from_analytics()
            except Exception:
                return False

    def _api_gateway_imports_from_analytics(self) -> bool:
        """Return True if gco-api-gateway imports any exports from gco-analytics.

        Uses CloudFormation's ``list_exports`` + ``list_imports`` to detect
        cross-stack references at runtime. This is more reliable than
        inspecting the CDK app because it reflects what's actually
        deployed.
        """
        import boto3

        region = self._get_deploy_region("gco-analytics")
        if not region:
            return False

        try:
            cfn = boto3.client("cloudformation", region_name=region)
            # Collect every export whose owning stack is gco-analytics.
            analytics_exports: list[str] = []
            paginator = cfn.get_paginator("list_exports")
            for page in paginator.paginate():
                for export in page.get("Exports", []):
                    owner = export.get("ExportingStackId", "")
                    # ExportingStackId is a full ARN; match by stack name.
                    if ":stack/gco-analytics/" in owner:
                        analytics_exports.append(export["Name"])

            if not analytics_exports:
                return False

            # For each export, check whether gco-api-gateway is listed
            # as an importer. ``list_imports`` returns the stack names
            # that currently import the given export.
            import_paginator = cfn.get_paginator("list_imports")
            for export_name in analytics_exports:
                try:
                    for page in import_paginator.paginate(ExportName=export_name):
                        for importer in page.get("Imports", []):
                            if importer == "gco-api-gateway":
                                return True
                except Exception as exc:
                    # ``list_imports`` raises when an export has zero
                    # consumers — treat that as "not imported" and move on.
                    logger.debug(
                        "list_imports(%s) failed (likely no consumers): %s",
                        export_name,
                        exc,
                    )
            return False
        except Exception as exc:
            logger.debug(
                "Failed to check analytics imports for gco-api-gateway: %s",
                exc,
                exc_info=True,
            )
            # On failure to check, err on the side of attempting the
            # redeploy so we don't skip necessary cleanup.
            return True

    def bootstrap(
        self,
        account: str | None = None,
        region: str | None = None,
    ) -> bool:
        """Bootstrap CDK in an AWS account/region."""
        cmd = ["bootstrap"]

        if account and region:
            cmd.append(f"aws://{account}/{region}")
        elif region:
            cmd.append(f"aws://unknown-account/{region}")

        result = self._run_cdk(cmd)
        return result.returncode == 0

    def is_bootstrapped(self, region: str) -> bool:
        """Check if CDK has been bootstrapped in a region.

        Looks for the CDKToolkit CloudFormation stack which is created
        by ``cdk bootstrap``. Result is cached per region for the lifetime
        of this StackManager instance.
        """
        if not hasattr(self, "_bootstrap_cache"):
            self._bootstrap_cache: dict[str, bool] = {}

        if region in self._bootstrap_cache:
            return self._bootstrap_cache[region]

        import boto3

        cf = boto3.client("cloudformation", region_name=region)
        try:
            response = cf.describe_stacks(StackName="CDKToolkit")
            stacks = response.get("Stacks", [])
            if stacks:
                status = stacks[0].get("StackStatus", "")
                # Any non-deleted state counts as bootstrapped
                result = "DELETE" not in status
                self._bootstrap_cache[region] = result
                return result
        except ClientError:
            pass  # Stack doesn't exist — not bootstrapped
        except Exception as e:
            logger.debug("Failed to check CDK bootstrap in %s: %s", region, e)

        self._bootstrap_cache[region] = False
        return False

    def ensure_bootstrapped(self, region: str) -> bool:
        """Ensure a region is CDK-bootstrapped, auto-bootstrapping if needed.

        Returns True if the region is (or was successfully) bootstrapped.
        """
        if self.is_bootstrapped(region):
            return True

        print(f"ℹ Region {region} is not CDK-bootstrapped. Bootstrapping now...")
        success = self.bootstrap(region=region)
        if success:
            # Update cache so we don't re-check this region
            if not hasattr(self, "_bootstrap_cache"):
                self._bootstrap_cache = {}
            self._bootstrap_cache[region] = True
            print(f"✓ CDK bootstrapped in {region}")
        else:
            print(f"✗ Failed to bootstrap CDK in {region}")
        return success

    def _get_deploy_region(self, stack_name: str) -> str | None:
        """Determine the target AWS region for a given stack name."""
        from .config import _load_cdk_json

        cdk_regions = _load_cdk_json()

        region: str | None
        if stack_name == "gco-global":
            region = cdk_regions.get("global") or self.config.global_region
            return region
        if stack_name == "gco-api-gateway":
            region = cdk_regions.get("api_gateway") or self.config.api_gateway_region
            return region
        if stack_name == "gco-monitoring":
            region = cdk_regions.get("monitoring") or self.config.monitoring_region
            return region
        if stack_name == "gco-analytics":
            # The analytics stack shares the API gateway region so the
            # presigned-URL Lambda can hook into the existing /studio/*
            # routes on the same API Gateway.
            region = cdk_regions.get("api_gateway") or self.config.api_gateway_region
            return region

        # Regional stacks: gco-{region}
        prefix = "gco-"
        if stack_name.startswith(prefix):
            return stack_name[len(prefix) :]

        return None

    def _mirror_target_regions(self, stack_name: str | None, all_stacks: bool) -> list[str]:
        """Regional regions to auto-mirror images for on this deploy.

        Only regional stacks (``gco-<region>``) run a Helm install that needs the
        mirror; the named global / api-gateway / monitoring / analytics stacks do
        not. For ``--all`` the regional regions come straight from cdk.json
        (``deployment_regions.regional``) so no synth is required. Returns a
        de-duplicated, order-stable list.
        """
        # Derive the prefix from project_name (#139) so a non-``gco``
        # deployment's regional stacks (``<project>-<region>``) are still
        # recognised — otherwise the mirror would silently no-op and the
        # regional Volcano Helm install would have no images to pull.
        prefix = f"{self.config.project_name}-"
        named = {
            f"{prefix}global",
            f"{prefix}api-gateway",
            f"{prefix}monitoring",
            f"{prefix}analytics",
        }
        if all_stacks:
            from .config import _load_cdk_json

            regional = _load_cdk_json().get("regional") or []
            return list(dict.fromkeys(str(r) for r in regional))
        if stack_name and stack_name.startswith(prefix) and stack_name not in named:
            region = self._get_deploy_region(stack_name)
            return [region] if region else []
        return []

    def _mirror_images_if_enabled(self, stack_name: str | None, all_stacks: bool) -> None:
        """Mirror third-party images into ECR before a regional stack deploys.

        No-op unless ``volcano_image_mirror.enabled`` is set in cdk.json. Mirrors
        every relevant regional region (see :meth:`_mirror_target_regions`); the
        copy is idempotent and skips images already present, so a fresh deploy
        seeds the mirror automatically and repeat deploys cost only a few ECR
        describe calls. Raises **before** any CDK call if an enabled mirror fails,
        so a deploy never points a consumer (e.g. Volcano's ``image_registry``)
        at images that aren't in ECR yet.
        """
        from . import _image_mirror as image_mirror

        cfg = image_mirror.read_mirror_config()
        if not cfg["enabled"]:
            return

        regions = self._mirror_target_regions(stack_name, all_stacks)
        for region in regions:
            print(f"Mirroring third-party images into ECR for {region} ...")
            try:
                image_mirror.mirror_images(
                    region, ecr_namespace=cfg["ecr_namespace"], skip_existing=True
                )
            except Exception as exc:  # noqa: BLE001 - surface a clear, actionable failure
                raise RuntimeError(
                    f"Image mirror failed for region {region}: {exc}\n"
                    "volcano_image_mirror is enabled but the images could not be "
                    "mirrored into ECR. Fix the cause (container runtime / network / "
                    "credentials) or run "
                    f"'gco images mirror --region {region}' manually, "
                    "then retry. Aborting before CDK so the deploy never points a "
                    "consumer at images that aren't in ECR."
                ) from exc

    def get_outputs(self, stack_name: str, region: str) -> dict[str, str]:
        """Get stack outputs from CloudFormation."""
        import boto3

        cf = boto3.client("cloudformation", region_name=region)
        try:
            response = cf.describe_stacks(StackName=stack_name)
            if response["Stacks"]:
                stack = response["Stacks"][0]
                outputs: dict[str, str] = {}
                for output in stack.get("Outputs", []):
                    outputs[str(output["OutputKey"])] = str(output["OutputValue"])
                return outputs
        except Exception as e:
            logger.debug("Failed to get outputs for %s in %s: %s", stack_name, region, e)
        return {}

    def get_stack_status(self, stack_name: str, region: str) -> StackInfo | None:
        """Get detailed stack status from CloudFormation."""
        import boto3

        cf = boto3.client("cloudformation", region_name=region)
        try:
            response = cf.describe_stacks(StackName=stack_name)
            if response["Stacks"]:
                stack = response["Stacks"][0]
                return StackInfo(
                    name=stack["StackName"],
                    status=stack["StackStatus"],
                    region=region,
                    created_time=stack.get("CreationTime"),
                    updated_time=stack.get("LastUpdatedTime"),
                    outputs={o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])},
                    tags={t["Key"]: t["Value"] for t in stack.get("Tags", [])},
                )
        except Exception as e:
            logger.debug("Failed to get stack status for %s in %s: %s", stack_name, region, e)
        return None

    def deploy_orchestrated(
        self,
        require_approval: bool = True,
        outputs_file: str | None = None,
        parameters: dict[str, str] | None = None,
        tags: dict[str, str] | None = None,
        progress: str = "events",
        on_stack_start: Callable[[str], None] | None = None,
        on_stack_complete: Callable[[str, bool], None] | None = None,
        parallel: bool = False,
        max_workers: int = 4,
    ) -> tuple[bool, list[str], list[str]]:
        """
        Deploy all stacks in the correct order.

        Deploys global stacks first, then regional stacks (optionally in parallel),
        then the monitoring stack (which depends on regional stacks).

        Args:
            require_approval: Whether to require approval for changes
            outputs_file: File to write outputs to
            parameters: CDK parameters
            tags: Tags to apply to stacks
            progress: Progress display type
            on_stack_start: Callback(stack_name) called when starting a stack
            on_stack_complete: Callback(stack_name, success) called when stack completes
            parallel: Deploy regional stacks in parallel
            max_workers: Maximum number of parallel deployments (default: 4)

        Returns:
            Tuple of (overall_success, successful_stacks, failed_stacks)
        """
        stacks = self.list_stacks()
        ordered_stacks = get_stack_deployment_order(stacks)

        # Separate stacks into three groups:
        # 1. Pre-regional global stacks (gco-global, gco-api-gateway)
        # 2. Regional stacks (can be parallelized)
        # 3. Post-regional stacks (gco-monitoring - depends on regional stacks)
        pre_regional = {"gco-global", "gco-api-gateway"}
        post_regional = {"gco-monitoring"}

        pre_regional_stacks = [s for s in ordered_stacks if s in pre_regional]
        regional_stacks = [
            s for s in ordered_stacks if s not in pre_regional and s not in post_regional
        ]
        post_regional_stacks = [s for s in ordered_stacks if s in post_regional]

        successful: list[str] = []
        failed: list[str] = []

        # Phase 1: Deploy pre-regional global stacks sequentially
        for stack_name in pre_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)

            success = self.deploy(
                stack_name=stack_name,
                require_approval=require_approval,
                outputs_file=outputs_file,
                parameters=parameters,
                tags=tags,
                progress=progress,
            )

            if success:
                successful.append(stack_name)
            else:
                failed.append(stack_name)

            if on_stack_complete:
                on_stack_complete(stack_name, success)

            # Stop on failure to prevent cascading issues
            if not success:
                return False, successful, failed

        # Phase 2: Deploy regional stacks (parallel or sequential)
        # All regional stacks pass --exclusively: globals are already deployed
        # in Phase 1, so CDK doesn't need to re-evaluate them. Skipping that
        # re-evaluation avoids re-running custom resources (notably
        # KubectlApplyManifests) on the global stacks every time a regional
        # stack is deployed — that would otherwise re-apply manifests and
        # rollout-restart controllers for no actual change.
        if regional_stacks:
            if parallel and len(regional_stacks) > 1:
                # Parallel deployment of regional stacks
                successful_regional, failed_regional = self._deploy_stacks_parallel(
                    stacks=regional_stacks,
                    require_approval=require_approval,
                    outputs_file=outputs_file,
                    parameters=parameters,
                    tags=tags,
                    progress=progress,
                    on_stack_start=on_stack_start,
                    on_stack_complete=on_stack_complete,
                    max_workers=max_workers,
                )
                successful.extend(successful_regional)
                failed.extend(failed_regional)

                # Stop if any regional stack failed
                if failed_regional:
                    return False, successful, failed
            else:
                # Sequential deployment
                for stack_name in regional_stacks:
                    if on_stack_start:
                        on_stack_start(stack_name)

                    success = self.deploy(
                        stack_name=stack_name,
                        require_approval=require_approval,
                        outputs_file=outputs_file,
                        parameters=parameters,
                        tags=tags,
                        progress=progress,
                        exclusively=True,
                    )

                    if success:
                        successful.append(stack_name)
                    else:
                        failed.append(stack_name)

                    if on_stack_complete:
                        on_stack_complete(stack_name, success)

                    # Stop on failure
                    if not success:
                        return False, successful, failed

        # Phase 3: Deploy post-regional stacks (monitoring) sequentially.
        # Same rationale as Phase 2: every upstream stack is already
        # deployed, so --exclusively prevents a redundant pass over
        # global/api-gateway/regional.
        for stack_name in post_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)

            success = self.deploy(
                stack_name=stack_name,
                require_approval=require_approval,
                outputs_file=outputs_file,
                parameters=parameters,
                tags=tags,
                progress=progress,
                exclusively=True,
            )

            if success:
                successful.append(stack_name)
            else:
                failed.append(stack_name)

            if on_stack_complete:
                on_stack_complete(stack_name, success)

            if not success:
                return False, successful, failed

        return len(failed) == 0, successful, failed

    def _deploy_stacks_parallel(
        self,
        stacks: list[str],
        require_approval: bool,
        outputs_file: str | None,
        parameters: dict[str, str] | None,
        tags: dict[str, str] | None,
        progress: str,
        on_stack_start: Callable[[str], None] | None,
        on_stack_complete: Callable[[str, bool], None] | None,
        max_workers: int,
    ) -> tuple[list[str], list[str]]:
        """Deploy multiple stacks in parallel using separate CDK output directories."""
        import tempfile

        successful: list[str] = []
        failed: list[str] = []
        lock = Lock()

        def deploy_single(stack_name: str) -> tuple[str, bool]:
            # Use a unique output directory in /tmp for each parallel deployment
            # This avoids CDK copying cdk.out.* directories into assets
            output_dir = tempfile.mkdtemp(prefix=f"cdk-{stack_name}-")

            if on_stack_start:
                with lock:
                    on_stack_start(stack_name)

            success = self.deploy(
                stack_name=stack_name,
                require_approval=require_approval,
                outputs_file=outputs_file,
                parameters=parameters,
                tags=tags,
                progress=progress,
                output_dir=output_dir,
                exclusively=True,
            )

            # Clean up the temporary output directory
            try:
                import shutil

                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir)
            except Exception as e:
                logger.debug("Cleanup of %s failed: %s", output_dir, e)

            return stack_name, success

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(deploy_single, stack): stack for stack in stacks}

            for future in as_completed(futures):
                stack_name, success = future.result()

                with lock:
                    if success:
                        successful.append(stack_name)
                    else:
                        failed.append(stack_name)

                    if on_stack_complete:
                        on_stack_complete(stack_name, success)

        return successful, failed

    def destroy_orchestrated(
        self,
        force: bool = False,
        on_stack_start: Callable[[str], None] | None = None,
        on_stack_complete: Callable[[str, bool], None] | None = None,
        parallel: bool = False,
        max_workers: int = 4,
    ) -> tuple[bool, list[str], list[str]]:
        """
        Destroy all stacks in the correct order.

        Destroys monitoring stack first, then regional stacks (optionally in parallel),
        then global stacks.

        Args:
            force: Skip confirmation prompts
            on_stack_start: Callback(stack_name) called when starting a stack
            on_stack_complete: Callback(stack_name, success) called when stack completes
            parallel: Destroy regional stacks in parallel
            max_workers: Maximum number of parallel destructions (default: 4)

        Returns:
            Tuple of (overall_success, successful_stacks, failed_stacks)
        """
        stacks = self.list_stacks()

        # Image-registry pre-destroy guard — fires before ANY teardown so
        # operators don't end up with monitoring + regional + api-gateway
        # already destroyed when the global-stack ECR delete fails on a
        # non-empty repo. Skipped entirely under the default
        # ``removal_policy: "retain"`` posture. See
        # ``_image_registry_destroy_preflight`` for the exact rules.
        if not self._image_registry_destroy_preflight(force=force):
            return False, [], list(stacks)

        # Phase 0: Clean up backup vault recovery points so the global stack
        # can be deleted cleanly by CloudFormation.
        self._cleanup_backup_vault()

        # Separate stacks into three groups (reverse of deploy order):
        pre_regional = {"gco-global", "gco-api-gateway"}
        post_regional = {"gco-monitoring"}

        post_regional_stacks = [s for s in stacks if s in post_regional]
        regional_stacks = [s for s in stacks if s not in pre_regional and s not in post_regional]
        pre_regional_stacks = [s for s in stacks if s in pre_regional]

        # Sort regional stacks alphabetically (reversed for destroy)
        regional_stacks.sort(reverse=True)
        # Sort pre-regional stacks in reverse priority order
        pre_regional_order = {"gco-api-gateway": 1, "gco-global": 2}
        pre_regional_stacks.sort(key=lambda x: pre_regional_order.get(x, 0))

        successful: list[str] = []
        failed: list[str] = []

        # Phase 1: Destroy post-regional stacks (monitoring) first
        for stack_name in post_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)

            success = self.destroy(
                stack_name=stack_name,
                force=force,
            )

            if success:
                successful.append(stack_name)
            else:
                failed.append(stack_name)

            if on_stack_complete:
                on_stack_complete(stack_name, success)

        # Phase 2: Destroy regional stacks (parallel or sequential)
        # Each regional destroy has a background watchdog that proactively
        # deletes EKS-managed security groups + orphaned ENIs as soon as
        # they appear. Without this, CloudFormation's VPC delete step sits
        # in DELETE_IN_PROGRESS for ~10 min waiting for EKS to GC the
        # ``eks-cluster-sg-<cluster>-*`` security group and its cluster
        # ENIs. The SG is owned by the EKS service (not CloudFormation),
        # so CFN can't delete it directly — it just polls the VPC's
        # dependencies until they drain. See ``_start_eks_sg_watchdog``.
        watchdog_stops: dict[str, Event] = {}
        watchdog_threads: dict[str, Thread] = {}
        for stack_name in regional_stacks:
            stop_event = Event()
            thread = self._start_eks_sg_watchdog(stack_name, stop_event)
            watchdog_stops[stack_name] = stop_event
            watchdog_threads[stack_name] = thread

        if regional_stacks:
            if parallel and len(regional_stacks) > 1:
                # Parallel destruction of regional stacks
                successful_regional, failed_regional = self._destroy_stacks_parallel(
                    stacks=regional_stacks,
                    force=force,
                    on_stack_start=on_stack_start,
                    on_stack_complete=on_stack_complete,
                    max_workers=max_workers,
                )
                successful.extend(successful_regional)
                failed.extend(failed_regional)
            else:
                # Sequential destruction
                for stack_name in regional_stacks:
                    if on_stack_start:
                        on_stack_start(stack_name)

                    success = self.destroy(
                        stack_name=stack_name,
                        force=force,
                    )

                    if success:
                        successful.append(stack_name)
                    else:
                        failed.append(stack_name)

                    if on_stack_complete:
                        on_stack_complete(stack_name, success)

        # Stop all watchdogs and do one final cleanup pass per stack to
        # catch anything EKS re-created during the destroy flow.
        for stack_name in regional_stacks:
            watchdog_stops[stack_name].set()
            watchdog_threads[stack_name].join(timeout=5)
            self._cleanup_eks_security_groups(stack_name)

        # Phase 3: Destroy pre-regional global stacks last
        for stack_name in pre_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)

            success = self.destroy(
                stack_name=stack_name,
                force=force,
            )

            if success:
                successful.append(stack_name)
            else:
                failed.append(stack_name)

            if on_stack_complete:
                on_stack_complete(stack_name, success)

        return len(failed) == 0, successful, failed

    def _destroy_stacks_parallel(
        self,
        stacks: list[str],
        force: bool,
        on_stack_start: Callable[[str], None] | None,
        on_stack_complete: Callable[[str, bool], None] | None,
        max_workers: int,
    ) -> tuple[list[str], list[str]]:
        """Destroy multiple stacks in parallel using separate CDK output directories."""
        import tempfile

        successful: list[str] = []
        failed: list[str] = []
        lock = Lock()

        def destroy_single(stack_name: str) -> tuple[str, bool]:
            # Use a unique output directory in /tmp for each parallel destruction
            output_dir = tempfile.mkdtemp(prefix=f"cdk-{stack_name}-")

            if on_stack_start:
                with lock:
                    on_stack_start(stack_name)

            success = self.destroy(
                stack_name=stack_name,
                force=force,
                output_dir=output_dir,
            )

            # Clean up the temporary output directory
            try:
                import shutil

                if os.path.exists(output_dir):
                    shutil.rmtree(output_dir)
            except Exception as e:
                logger.debug("Cleanup of %s failed: %s", output_dir, e)

            return stack_name, success

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(destroy_single, stack): stack for stack in stacks}

            for future in as_completed(futures):
                stack_name, success = future.result()

                with lock:
                    if success:
                        successful.append(stack_name)
                    else:
                        failed.append(stack_name)

                    if on_stack_complete:
                        on_stack_complete(stack_name, success)

        return successful, failed

    def _cleanup_backup_vault(self) -> None:
        """Delete all recovery points from the GCO backup vault.

        This is called before destroy-all so CloudFormation can delete the
        backup vault cleanly. Without this, the vault deletion fails because
        it contains recovery points.
        """
        import boto3

        global_region = self.config.global_region
        project_name = self.config.project_name

        try:
            backup_client = boto3.client("backup", region_name=global_region)

            # Find the backup vault by listing vaults with the project prefix
            paginator = backup_client.get_paginator("list_backup_vaults")
            vault_name = None
            for page in paginator.paginate():
                for vault in page.get("BackupVaultList", []):
                    if project_name in vault["BackupVaultName"].lower():
                        vault_name = vault["BackupVaultName"]
                        break
                if vault_name:
                    break

            if not vault_name:
                return

            # Delete all recovery points in the vault
            rp_paginator = backup_client.get_paginator("list_recovery_points_by_backup_vault")
            deleted = 0
            for page in rp_paginator.paginate(BackupVaultName=vault_name):
                for rp in page.get("RecoveryPoints", []):
                    try:
                        backup_client.delete_recovery_point(
                            BackupVaultName=vault_name,
                            RecoveryPointArn=rp["RecoveryPointArn"],
                        )
                        deleted += 1
                    except Exception as e:
                        logger.debug("Failed to delete recovery point: %s", e)

            if deleted > 0:
                print(f"  Cleaned up {deleted} backup recovery points from {vault_name}")

        except Exception as e:
            print(f"  Warning: Backup vault cleanup failed (non-fatal): {e}")

    def cleanup_eks_security_groups(self) -> None:
        """Clean up EKS-managed security groups across all regional stacks.

        Called between destroy retries to remove orphaned security groups
        that block VPC deletion.
        """
        stacks = self.list_stacks()
        pre_regional = {"gco-global", "gco-api-gateway", "gco-monitoring"}
        regional_stacks = [s for s in stacks if s not in pre_regional]
        for stack_name in regional_stacks:
            self._cleanup_eks_security_groups(stack_name)

    def cleanup_orphaned_network_interfaces(self) -> None:
        """Report and clear resources that can block VPC deletion, across all
        regional stacks. Run between destroy retries.

        Generalizes ``cleanup_eks_security_groups`` (which force-deletes the
        ``eks-cluster-sg-*`` security group + its ENIs that EKS leaves behind)
        with a broader sweep: for each regional stack's VPC it enumerates every
        remaining network interface, categorizes them (Global Accelerator / ELB
        / EKS / other), deletes the ones that are safe to remove (detached and
        not service-managed), and prints a friendly summary of what it found and
        what the next retry is waiting on. Service-managed ENIs (Global
        Accelerator, ELB) are released asynchronously by AWS once the endpoint /
        load balancer is gone, so we report them rather than fight them.
        """
        stacks = self.list_stacks()
        pre_regional = {"gco-global", "gco-api-gateway", "gco-monitoring"}
        regional_stacks = [s for s in stacks if s not in pre_regional]
        for stack_name in regional_stacks:
            # Existing behaviour first: clear the EKS cluster SG + its ENIs.
            self._cleanup_eks_security_groups(stack_name)
            # Then report (and safely clear) anything else lingering in the VPC.
            summary = self._summarize_orphaned_enis(stack_name)
            self._print_orphaned_eni_summary(stack_name, summary)

    @staticmethod
    def _classify_orphaned_eni(eni: dict[str, Any]) -> str:
        """Bucket a network interface by which AWS service owns it.

        Uses ``InterfaceType`` first (authoritative for Global Accelerator and
        the load-balancer types) and falls back to the human ``Description``
        string for the EKS / ELB cases that present as a plain ``interface``.
        Returns one of ``global_accelerator`` / ``elb`` / ``eks`` / ``other``.
        """
        itype = str(eni.get("InterfaceType") or "").lower()
        desc = str(eni.get("Description") or "").lower()
        if (
            itype == "global_accelerator_managed"
            or "global_accelerator" in desc
            or "global accelerator" in desc
        ):
            return "global_accelerator"
        if itype in ("load_balancer", "network_load_balancer") or desc.startswith("elb "):
            return "elb"
        if "eks" in desc or "k8s" in desc or "kubernetes" in desc:
            return "eks"
        return "other"

    def _summarize_orphaned_enis(self, stack_name: str) -> dict[str, int]:
        """Inspect the stack's VPC(s) for lingering ENIs, categorize them, and
        best-effort delete the ones that are safe to remove.

        "Safe to remove" means ``Status == "available"`` (detached) and not
        ``RequesterManaged`` (i.e. not owned by a service like GA / ELB, which
        rejects manual deletion and releases the ENI on its own schedule).

        Returns a dict of counts: per-category totals plus ``deleted`` and
        ``vpcs``. Wholly best-effort — any AWS error degrades to the counts
        gathered so far rather than raising into the destroy flow.
        """
        import boto3

        region = stack_name.replace(f"{self.config.project_name}-", "", 1)
        summary: dict[str, int] = {
            "global_accelerator": 0,
            "elb": 0,
            "eks": 0,
            "other": 0,
            "deleted": 0,
            "vpcs": 0,
        }
        try:
            ec2 = boto3.client("ec2", region_name=region)
            vpcs = ec2.describe_vpcs(
                Filters=[{"Name": "tag:aws:cloudformation:stack-name", "Values": [stack_name]}]
            ).get("Vpcs", [])
        except Exception as e:  # noqa: BLE001
            logger.debug("ENI sweep: VPC lookup failed for %s: %s", stack_name, e)
            return summary

        for vpc in vpcs:
            summary["vpcs"] += 1
            vpc_id = vpc.get("VpcId")
            try:
                enis = ec2.describe_network_interfaces(
                    Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
                ).get("NetworkInterfaces", [])
            except Exception as e:  # noqa: BLE001
                logger.debug("ENI sweep: describe ENIs failed for %s: %s", vpc_id, e)
                continue

            for eni in enis:
                summary[self._classify_orphaned_eni(eni)] += 1
                detached = eni.get("Status") == "available"
                service_managed = bool(eni.get("RequesterManaged", False))
                if detached and not service_managed:
                    eni_id = eni.get("NetworkInterfaceId")
                    try:
                        ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                        summary["deleted"] += 1
                        logger.debug("ENI sweep: deleted detached ENI %s in %s", eni_id, vpc_id)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("ENI sweep: delete of %s failed: %s", eni_id, e)
        return summary

    @staticmethod
    def _print_orphaned_eni_summary(stack_name: str, summary: dict[str, int]) -> None:
        """Print a friendly summary of what the ENI sweep found and handled."""
        categories = (
            ("global_accelerator", "Global Accelerator-managed"),
            ("elb", "ELB-managed"),
            ("eks", "EKS-managed"),
            ("other", "other"),
        )
        total = sum(summary.get(key, 0) for key, _ in categories)
        if total == 0:
            return
        breakdown = ", ".join(
            f"{summary[key]} {label}" for key, label in categories if summary.get(key)
        )
        print(f"  {stack_name}: {total} network interface(s) still in the VPC ({breakdown}).")
        if summary.get("deleted"):
            print(f"    Removed {summary['deleted']} detached interface(s).")
        remaining = total - summary.get("deleted", 0)
        if remaining > 0:
            print(
                f"    {remaining} still held by AWS — Global Accelerator / ELB release these "
                "asynchronously once the endpoint and load balancer are gone; the next retry "
                "proceeds once they drain."
            )

    def _cleanup_eks_security_groups(self, stack_name: str) -> None:
        """Clean up EKS-managed security groups that block VPC deletion.

        EKS creates a cluster security group (eks-cluster-sg-<cluster-name>-*)
        that is owned by EKS, not CloudFormation. When the stack is destroyed,
        this SG and its attached ENIs can linger, causing VPC deletion to fail.

        This method finds and deletes these orphaned security groups and their
        ENIs before the stack destroy runs.
        """
        import time as _time

        import boto3

        # Extract region from stack name (e.g., gco-us-east-1 -> us-east-1)
        project_name = self.config.project_name
        region = stack_name.replace(f"{project_name}-", "", 1)
        cluster_name = stack_name  # cluster name matches stack name

        try:
            ec2 = boto3.client("ec2", region_name=region)

            # Find EKS-managed security groups by name pattern
            response = ec2.describe_security_groups(
                Filters=[
                    {
                        "Name": "group-name",
                        "Values": [f"eks-cluster-sg-{cluster_name}-*"],
                    }
                ]
            )

            sgs = response.get("SecurityGroups", [])
            if not sgs:
                return

            for sg in sgs:
                sg_id = sg["GroupId"]
                sg_name = sg.get("GroupName", "")

                # First, detach and delete any ENIs using this SG
                eni_response = ec2.describe_network_interfaces(
                    Filters=[{"Name": "group-id", "Values": [sg_id]}]
                )

                for eni in eni_response.get("NetworkInterfaces", []):
                    eni_id = eni["NetworkInterfaceId"]
                    try:
                        if eni.get("Attachment"):
                            ec2.detach_network_interface(
                                AttachmentId=eni["Attachment"]["AttachmentId"],
                                Force=True,
                            )
                            _time.sleep(5)
                        ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                        logger.debug("Deleted ENI %s from SG %s", eni_id, sg_name)
                    except Exception as e:
                        logger.debug("Failed to delete ENI %s: %s", eni_id, e)

                # Now delete the security group
                try:
                    ec2.delete_security_group(GroupId=sg_id)
                    print(f"  Cleaned up EKS security group: {sg_name} ({sg_id})")
                except Exception as e:
                    logger.debug(
                        "Failed to delete SG %s: %s (will retry on next attempt)", sg_id, e
                    )

        except Exception as e:
            # Non-fatal — the retry loop will handle it
            logger.debug("EKS security group cleanup for %s failed: %s", stack_name, e)

    def _start_eks_sg_watchdog(self, stack_name: str, stop_event: Event) -> Thread:
        """Start a background thread that polls for orphaned EKS security groups.

        EKS creates an ``eks-cluster-sg-<cluster-name>-*`` security group that
        is owned by the EKS service (not CloudFormation). When the stack's
        EKS cluster resource deletes, the SG is supposed to GC along with its
        cluster ENIs — but on EKS Auto Mode there's a window where the SG
        lingers after the cluster is gone, blocking the subsequent VPC
        delete with ``DependencyViolation``. CloudFormation then sits in
        ``DELETE_IN_PROGRESS`` on the VPC for ~10 minutes retrying.

        This watchdog runs for the full duration of the regional-stack
        destroy, polling every 30 seconds. As soon as an orphaned SG appears
        (which only happens after the cluster delete has progressed past
        the cluster resource itself), it deletes the SG and any ENIs still
        attached. That unblocks the VPC delete immediately instead of
        waiting for EKS's own GC timer.

        The thread exits when ``stop_event`` is set by the orchestrator at
        the end of the regional phase.
        """

        def _watchdog() -> None:
            while not stop_event.is_set():
                try:
                    self._cleanup_eks_security_groups(stack_name)
                except Exception as e:
                    logger.debug(
                        "EKS SG watchdog tick for %s failed (non-fatal): %s",
                        stack_name,
                        e,
                    )
                # ``wait`` returns immediately when the event is set, so this
                # doubles as the sleep-and-shutdown-check in one call.
                stop_event.wait(timeout=30)

        thread = Thread(
            target=_watchdog,
            name=f"eks-sg-watchdog-{stack_name}",
            daemon=True,
        )
        thread.start()
        return thread


def get_stack_manager(config: GCOConfig) -> StackManager:
    """Factory function to get a StackManager instance."""
    return StackManager(config)


def get_stack_deployment_order(stacks: list[str]) -> list[str]:
    """
    Get the correct deployment order for stacks.

    Order: global stacks first, then regional stacks.
    Global stacks: gco-global, gco-api-gateway, gco-monitoring
    Regional stacks: gco-{region} (e.g., gco-us-east-1)
    """
    global_stacks = []
    regional_stacks = []

    # Define global stack priority (lower = deploy first)
    global_priority = {
        "gco-global": 1,
        "gco-api-gateway": 2,
        "gco-analytics": 2.5,
        "gco-monitoring": 3,
    }

    for stack in stacks:
        if stack in global_priority:
            global_stacks.append((global_priority[stack], stack))
        else:
            regional_stacks.append(stack)

    # Sort global stacks by priority, regional stacks alphabetically
    global_stacks.sort(key=lambda x: x[0])
    regional_stacks.sort()

    return [s[1] for s in global_stacks] + regional_stacks


def get_stack_destroy_order(stacks: list[str]) -> list[str]:
    """
    Get the correct destroy order for stacks.

    Order: regional stacks first, then global stacks (reverse of deploy).
    """
    deployment_order = get_stack_deployment_order(stacks)
    return list(reversed(deployment_order))


# =============================================================================
# Feature toggle helpers
# =============================================================================

_FSX_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "storage_capacity_gib": 1200,
    "deployment_type": "SCRATCH_2",
    "per_unit_storage_throughput": 200,
    "data_compression_type": "LZ4",
    "import_path": None,
    "export_path": None,
    "auto_import_policy": "NEW_CHANGED_DELETED",
}


def _find_cdk_json() -> Path | None:
    """Find cdk.json in current or parent directories."""
    current = Path.cwd()
    for parent in [current] + list(current.parents):
        cdk_path = parent / "cdk.json"
        if cdk_path.exists():
            return cdk_path
    return None


def get_fsx_config(region: str | None = None) -> dict[str, Any]:
    """Get current FSx for Lustre configuration from cdk.json.

    Args:
        region: Optional region to get config for. If provided, checks for
                region-specific overrides first.

    Returns:
        FSx configuration dictionary
    """
    return _get_feature_config("fsx_lustre", _FSX_DEFAULTS, region)


def update_fsx_config(settings: dict[str, Any], region: str | None = None) -> None:
    """Update FSx for Lustre configuration in cdk.json.

    Args:
        settings: FSx settings to update
        region: Optional region for region-specific config. If None, updates global config.
    """
    _update_feature_config("fsx_lustre", settings, _FSX_DEFAULTS, region)


# =============================================================================
# Generic feature toggle helpers (used by FSx, Valkey, Aurora, and future features)
# =============================================================================


def _get_feature_config(
    feature_key: str,
    default_config: dict[str, Any],
    region: str | None = None,
) -> dict[str, Any]:
    """Get configuration for a toggleable feature from cdk.json.

    Args:
        feature_key: The cdk.json context key (e.g. "valkey", "aurora_pgvector").
        default_config: Default configuration values when the key is missing.
        region: Optional region for region-specific overrides.

    Returns:
        Merged configuration dictionary.
    """
    cdk_json_path = _find_cdk_json()
    if not cdk_json_path:
        raise RuntimeError("cdk.json not found")

    import json

    with open(cdk_json_path, encoding="utf-8") as f:
        cdk_config = json.load(f)

    global_config = cdk_config.get("context", {}).get(feature_key, default_config)

    if region:
        region_key = f"{feature_key}_regions"
        region_overrides = cdk_config.get("context", {}).get(region_key, {})
        if region in region_overrides:
            merged = {**global_config, **region_overrides[region]}
            merged["region"] = region
            merged["is_region_specific"] = True
            return merged

    result = {**default_config, **global_config}
    result["is_region_specific"] = False
    return result


def _update_feature_config(
    feature_key: str,
    settings: dict[str, Any],
    default_config: dict[str, Any],
    region: str | None = None,
) -> None:
    """Update configuration for a toggleable feature in cdk.json.

    Args:
        feature_key: The cdk.json context key (e.g. "valkey", "aurora_pgvector").
        settings: Settings to update.
        default_config: Default configuration values when the key is missing.
        region: Optional region for region-specific config.
    """
    cdk_json_path = _find_cdk_json()
    if not cdk_json_path:
        raise RuntimeError("cdk.json not found")

    import json

    with open(cdk_json_path, encoding="utf-8") as f:
        cdk_config = json.load(f)

    if "context" not in cdk_config:
        cdk_config["context"] = {}

    if region:
        region_key = f"{feature_key}_regions"
        if region_key not in cdk_config["context"]:
            cdk_config["context"][region_key] = {}
        if region not in cdk_config["context"][region_key]:
            cdk_config["context"][region_key][region] = {}
        for key, value in settings.items():
            if value is not None or key == "enabled":
                cdk_config["context"][region_key][region][key] = value
    else:
        if feature_key not in cdk_config["context"]:
            cdk_config["context"][feature_key] = {**default_config}
        for key, value in settings.items():
            if value is not None or key == "enabled":
                cdk_config["context"][feature_key][key] = value

    with open(cdk_json_path, "w", encoding="utf-8") as f:
        json.dump(cdk_config, f, indent=2)


# =============================================================================
# Valkey configuration
# =============================================================================

_VALKEY_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "max_data_storage_gb": 5,
    "max_ecpu_per_second": 5000,
    "snapshot_retention_limit": 1,
}


def get_valkey_config(region: str | None = None) -> dict[str, Any]:
    """Get current Valkey Serverless configuration from cdk.json."""
    return _get_feature_config("valkey", _VALKEY_DEFAULTS, region)


def update_valkey_config(settings: dict[str, Any], region: str | None = None) -> None:
    """Update Valkey Serverless configuration in cdk.json."""
    _update_feature_config("valkey", settings, _VALKEY_DEFAULTS, region)


# =============================================================================
# Aurora pgvector configuration
# =============================================================================

_AURORA_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "min_acu": 0,
    "max_acu": 16,
    "backup_retention_days": 7,
    "deletion_protection": False,
}


def get_aurora_config(region: str | None = None) -> dict[str, Any]:
    """Get current Aurora pgvector configuration from cdk.json."""
    return _get_feature_config("aurora_pgvector", _AURORA_DEFAULTS, region)


def update_aurora_config(settings: dict[str, Any], region: str | None = None) -> None:
    """Update Aurora pgvector configuration in cdk.json."""
    _update_feature_config("aurora_pgvector", settings, _AURORA_DEFAULTS, region)


# =============================================================================
# Analytics environment configuration
# =============================================================================

_ANALYTICS_DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "hyperpod": {"enabled": False},
    "canvas": {"enabled": False},
    "cognito": {"domain_prefix": None, "removal_policy": "destroy"},
    "efs": {"removal_policy": "destroy"},
    "studio": {"user_profile_name_prefix": None},
}


def get_analytics_config() -> dict[str, Any]:
    """Get the analytics environment configuration from cdk.json.

    The analytics stack is single-region by construction (lives in the
    api-gateway region), so this helper does not accept a region argument.
    Returned dict is the defaults merged with any operator overrides from
    the ``context.analytics_environment`` block.
    """
    return _get_feature_config("analytics_environment", _ANALYTICS_DEFAULTS)


def update_analytics_config(settings: dict[str, Any]) -> None:
    """Update the analytics environment configuration in cdk.json.

    Mirrors ``update_valkey_config`` / ``update_aurora_config``. Nested
    keys under ``analytics_environment`` (``hyperpod``, ``canvas``,
    ``cognito``, ``efs``, ``studio``) are merged one level deep rather
    than replaced wholesale — ``enable --hyperpod`` must not clobber
    ``cognito.removal_policy``.
    """
    _update_feature_config("analytics_environment", settings, _ANALYTICS_DEFAULTS)
