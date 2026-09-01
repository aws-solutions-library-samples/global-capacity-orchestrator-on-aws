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
        1. Global stack (partition-wide state, plus Global Accelerator in `aws`)
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

import errno
import hashlib
import importlib.util
import json
import logging
import math
import os
import shutil
import signal
import site
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Collection, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import Event, Lock, RLock, Thread, local
from typing import TYPE_CHECKING, Any, BinaryIO, Literal, TypedDict

from botocore.exceptions import ClientError

from gco.lambda_shared_sources import LAMBDA_SHARED_SOURCE_TARGETS
from gco.stacks.constants import (
    known_cloudformation_regions,
    validated_deployment_partition,
    validated_regional_deployment_regions,
)

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-09-01T14:42:56Z
# Generated from Git commit: 89b000378ed5a912a38c06f4feab2b029936ebcc
# Flowchart(s) generated from this file:
#   * ``StackManager.deploy_orchestrated`` -> ``diagrams/code_diagrams/cli/stacks.StackManager_deploy_orchestrated.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager_deploy_orchestrated.png``)
#   * ``StackManager.destroy_orchestrated`` -> ``diagrams/code_diagrams/cli/stacks.StackManager_destroy_orchestrated.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager_destroy_orchestrated.png``)
#   * ``StackManager._mirror_images_if_enabled`` -> ``diagrams/code_diagrams/cli/stacks.StackManager__mirror_images_if_enabled.html``
#     (PNG: ``diagrams/code_diagrams/cli/stacks.StackManager__mirror_images_if_enabled.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


if TYPE_CHECKING:
    from .config import GCOConfig

logger = logging.getLogger(__name__)

# Every writer that replaces cdk.json must participate in the same transaction
# lock. The process-local RLock handles threads and nested feature updates; the
# advisory process lock uses a stable directory descriptor on POSIX and a
# persistent sidecar file on Windows, so it survives ``os.replace`` of the
# configuration inode.
_CONFIG_LOCK_FILENAME = ".gco-config.lock"
_CONFIG_THREAD_LOCKS: dict[Path, Any] = {}
_CONFIG_THREAD_LOCKS_GUARD = Lock()
_CONFIG_LOCK_STATE = local()

# Python packages ``app.py`` imports at CDK synth time. They ship in the
# optional ``[cdk]`` extra (see pyproject.toml), NOT the base install, so a
# lightweight ``uvx`` / ``pip install`` of ``gco-cli`` that skips the extra
# cannot synthesize or deploy. ``StackManager._ensure_cdk_toolchain`` checks
# for these before invoking ``cdk`` so a missing toolchain is actionable.
_CDK_TOOLCHAIN_MODULES = ("aws_cdk", "cdk_nag")
_INFERENCE_STREAMING_PACKAGE_FILES = ("index.mjs", "package.json", "package-lock.json")
_KUBECTL_PACKAGE_INPUTS = ("handler.py", "requirements.txt", "manifests")
_LAMBDA_BUILD_MANIFEST = ".gco-build-manifest.json"
_LAMBDA_BUILD_MANIFEST_VERSION = 1
_LAMBDA_SOURCE_IGNORED_DIRECTORIES = frozenset({"__pycache__", ".mypy_cache", ".pytest_cache"})
_LAMBDA_SOURCE_IGNORED_FILES = frozenset({".DS_Store"})
_LAMBDA_SOURCE_COPY_IGNORE_PATTERNS = (
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".DS_Store",
    "*.pyc",
    "*.pyo",
)
_FILE_LOCK_RETRY_SECONDS = 0.05
# 15 minutes: comfortably above the longest legitimate hold (a cold publisher
# rebuild, minutes) while bounding the pathological one (an abandoned pytest
# session's session-long shared locks, indefinite).
_ASSET_LOCK_TIMEOUT_SECONDS_DEFAULT = 900.0
_CONFIG_LOCK_TIMEOUT_SECONDS_DEFAULT = 900.0
_FileLockPurpose = Literal["asset", "configuration"]
_CDK_ASSET_CONSUMER_MAX_ATTEMPTS = 3
_CLOUDFORMATION_DELETE_TIMEOUT_SECONDS = 7200.0
_CLOUDFORMATION_DELETE_POLL_SECONDS = 15.0
_CLOUDFORMATION_DELETE_HEARTBEAT_SECONDS = 60.0
_CLOUDFORMATION_SETTLE_UNKNOWN_TIMEOUT_SECONDS = 60.0
_CLOUDFORMATION_SETTLE_UNKNOWN_POLL_SECONDS = 5.0
_BOOTSTRAP_HEALTHY_STATUSES = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE"})
# CDK's ``prepare-change-set`` mode can return before a fresh CREATE change set
# is visible through CloudFormation's read path. Poll only that authoritative
# fresh-create absence window; every access, identity, tag, and ownership error
# still fails immediately. Sixteen reads at two-second intervals bound the
# eventual-consistency allowance to 30 seconds after the first attempt.
_STRICT_CHANGE_SET_INSPECTION_ATTEMPTS = 16
_STRICT_CHANGE_SET_INSPECTION_RETRY_SECONDS = 2.0
_LIVE_VALIDATION_PROVIDER_LOG_CONTEXT = "gco_live_validation_retain_provider_log_groups"

# LAMBDA_SHARED_SOURCE_TARGETS is imported from the dependency-light inventory
# shared by deploy packaging, diagram reconciliation, and commit-time guards.
StackAuthorizationCallback = Callable[[str, str, str], None]
CleanupOutcomeCallback = Callable[[str, dict[str, Any]], None]
ChangeSetPreparedCallback = Callable[[str, str, str, str, str], None]
PreparedChangeSetAuthority = Mapping[str, Mapping[str, Mapping[str, str]]]
EcrRepositoryCreatedCallback = Callable[[str, Mapping[str, Any]], None]


class _StackOperationSafetyKwargs(TypedDict):
    """Type-preserving keyword bundle shared by strict deploy and destroy calls."""

    allow_bootstrap: bool
    bootstrap_stacks: Mapping[str, Mapping[str, str]] | None
    expected_stack_ids: Mapping[str, str | None] | None
    prepared_change_sets: PreparedChangeSetAuthority | None
    authorize_stack: StackAuthorizationCallback | None
    strict_deployment_token: str | None
    on_change_set_prepared: ChangeSetPreparedCallback | None
    on_ecr_repository_created: EcrRepositoryCreatedCallback | None


@dataclass(frozen=True)
class _CdkAssetSpec:
    """One canonical generated asset consumed by the CDK application."""

    name: str
    source_directory: str
    build_directory: str
    source_inputs: tuple[str, ...] | None

    def paths(self, project_root: Path) -> tuple[Path, Path]:
        lambda_dir = project_root / "lambda"
        return lambda_dir / self.source_directory, lambda_dir / self.build_directory


_KUBECTL_CDK_ASSET = _CdkAssetSpec(
    name="kubectl-applier-simple",
    source_directory="kubectl-applier-simple",
    build_directory="kubectl-applier-simple-build",
    source_inputs=_KUBECTL_PACKAGE_INPUTS,
)
_HELM_CDK_ASSET = _CdkAssetSpec(
    name="helm-installer",
    source_directory="helm-installer",
    build_directory="helm-installer-build",
    source_inputs=None,
)
_INFERENCE_STREAMING_CDK_ASSET = _CdkAssetSpec(
    name="inference-streaming-proxy",
    source_directory="inference-streaming-proxy",
    build_directory="inference-streaming-proxy-build",
    source_inputs=_INFERENCE_STREAMING_PACKAGE_FILES,
)
_CDK_ASSET_SPECS = (
    _KUBECTL_CDK_ASSET,
    _HELM_CDK_ASSET,
    _INFERENCE_STREAMING_CDK_ASSET,
)


class _AssetThreadState(local):
    """Per-thread nesting state; each OS lock still spans the full process."""

    def __init__(self) -> None:
        self.held: dict[str, tuple[bool, int]] = {}
        self.active_consumers: dict[str, int] = {}


_asset_thread_state = _AssetThreadState()


def _asset_tree_paths(root: Path, source_inputs: tuple[str, ...] | None) -> Iterator[Path]:
    """Yield deterministic source or build-tree entries below ``root``."""
    selected: set[Path] = set()
    if source_inputs is None:
        selected.update(root.rglob("*"))
    else:
        for relative_name in source_inputs:
            path = root / relative_name
            if not path.exists() and not path.is_symlink():
                raise FileNotFoundError(path)
            selected.add(path)
            if path.is_dir() and not path.is_symlink():
                selected.update(path.rglob("*"))
    yield from sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def _asset_tree_digest(
    root: Path,
    *,
    source_inputs: tuple[str, ...] | None = None,
) -> str | None:
    """Hash every deployable entry in a source selection or complete build tree.

    The completion manifest and local cache files are excluded. Regular-file
    content, paths, modes, directory entries, and symlink targets are included
    so removing any installed transitive dependency invalidates the build.
    """
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    try:
        for path in _asset_tree_paths(root, source_inputs):
            relative = path.relative_to(root)
            if any(part in _LAMBDA_SOURCE_IGNORED_DIRECTORIES for part in relative.parts):
                continue
            if (
                path.name in _LAMBDA_SOURCE_IGNORED_FILES
                or path.name == _LAMBDA_BUILD_MANIFEST
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue

            metadata = path.lstat()
            relative_bytes = relative.as_posix().encode("utf-8")
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            digest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))

            if path.is_symlink():
                target = os.readlink(path).encode("utf-8")
                digest.update(b"L")
                digest.update(len(target).to_bytes(8, "big"))
                digest.update(target)
            elif path.is_dir():
                digest.update(b"D")
            elif path.is_file():
                file_digest = hashlib.sha256()
                with path.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        file_digest.update(chunk)
                digest.update(b"F")
                digest.update(file_digest.digest())
            else:
                return None
    except OSError, UnicodeError:
        return None
    return digest.hexdigest()


def _read_build_manifest(build_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((build_dir / _LAMBDA_BUILD_MANIFEST).read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _write_build_manifest(build_dir: Path, source_digest: str) -> None:
    """Write the completion marker only after the staged build is complete."""
    build_digest = _asset_tree_digest(build_dir)
    if build_digest is None:
        raise RuntimeError(f"Unable to hash completed Lambda asset {build_dir.name}")
    manifest = {
        "schema_version": _LAMBDA_BUILD_MANIFEST_VERSION,
        "source_digest": source_digest,
        "build_digest": build_digest,
    }
    manifest_path = build_dir / _LAMBDA_BUILD_MANIFEST
    with manifest_path.open("x", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _asset_build_is_fresh_unlocked(
    source_dir: Path,
    build_dir: Path,
    *,
    source_inputs: tuple[str, ...] | None,
) -> bool:
    manifest = _read_build_manifest(build_dir)
    if manifest is None or manifest.get("schema_version") != _LAMBDA_BUILD_MANIFEST_VERSION:
        return False
    source_digest = _asset_tree_digest(source_dir, source_inputs=source_inputs)
    build_digest = _asset_tree_digest(build_dir)
    return (
        source_digest is not None
        and build_digest is not None
        and manifest.get("source_digest") == source_digest
        and manifest.get("build_digest") == build_digest
    )


def _thread_asset_locks() -> dict[str, tuple[bool, int]]:
    """Return locks held by the current thread for safe nested consumers."""
    return _asset_thread_state.held


def _ensure_windows_lock_byte(lock_file: BinaryIO) -> None:
    """Ensure msvcrt has a real byte range to lock."""
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)


def _windows_lock_is_contended(exc: OSError) -> bool:
    return exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK} or getattr(
        exc,
        "winerror",
        None,
    ) in {32, 33, 36}


def _file_lock_timeout_seconds(purpose: _FileLockPurpose) -> float:
    """Return the bounded wait for one class of interprocess lock."""
    if purpose == "asset":
        env_name = "GCO_ASSET_LOCK_TIMEOUT_SECONDS"
        default = _ASSET_LOCK_TIMEOUT_SECONDS_DEFAULT
    else:
        env_name = "GCO_CONFIG_LOCK_TIMEOUT_SECONDS"
        default = _CONFIG_LOCK_TIMEOUT_SECONDS_DEFAULT

    raw = os.environ.get(env_name, "")
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return value


def _warn_file_lock_contended(
    lock_name: object,
    *,
    exclusive: bool,
    timeout: float,
    purpose: _FileLockPurpose,
) -> None:
    if purpose == "configuration":
        logger.warning(
            "Waiting up to %.0fs for the configuration lock on %s — another CLI "
            "or MCP process is updating cdk.json. Wait for it to finish; tune via "
            "GCO_CONFIG_LOCK_TIMEOUT_SECONDS.",
            timeout,
            lock_name,
        )
        return

    mode = "exclusive" if exclusive else "shared"
    logger.warning(
        "Waiting up to %.0fs for the %s asset lock on %s — another process holds "
        "it (a pytest session holds shared locks for its whole run; a "
        "deploy/synth/destroy holds the exclusive lock while rebuilding). "
        "Find the holder with `lsof %s`; tune via GCO_ASSET_LOCK_TIMEOUT_SECONDS.",
        timeout,
        mode,
        lock_name,
        lock_name,
    )


def _raise_file_lock_timeout(
    lock_name: object,
    *,
    timeout: float,
    purpose: _FileLockPurpose,
) -> None:
    if purpose == "configuration":
        raise TimeoutError(
            f"Timed out after {timeout:.0f}s waiting for the configuration lock on "
            f"{lock_name}. Another CLI or MCP process is updating cdk.json. Wait "
            "for it to finish and retry; raise GCO_CONFIG_LOCK_TIMEOUT_SECONDS "
            "to wait longer."
        )

    raise TimeoutError(
        f"Timed out after {timeout:.0f}s waiting for the asset lock on {lock_name}. "
        "Another process still holds it — often an abandoned pytest session, which "
        f"keeps shared locks until it exits. Find it with `lsof {lock_name}`, stop "
        "it, and retry; raise GCO_ASSET_LOCK_TIMEOUT_SECONDS to wait longer."
    )


def _posix_lock_is_contended(exc: OSError) -> bool:
    return isinstance(exc, BlockingIOError) or exc.errno in {errno.EACCES, errno.EAGAIN}


def _acquire_posix_flock(
    lock_fd: int,
    *,
    lock_name: object,
    exclusive: bool,
    purpose: _FileLockPurpose,
) -> None:
    """Acquire one POSIX flock with the shared warning and timeout contract."""
    import fcntl

    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
        return
    except OSError as exc:
        if not _posix_lock_is_contended(exc):
            raise

    timeout = _file_lock_timeout_seconds(purpose)
    deadline = time.monotonic() + timeout
    _warn_file_lock_contended(
        lock_name,
        exclusive=exclusive,
        timeout=timeout,
        purpose=purpose,
    )
    while True:
        try:
            fcntl.flock(lock_fd, operation | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if not _posix_lock_is_contended(exc):
                raise
            if time.monotonic() >= deadline:
                _raise_file_lock_timeout(
                    lock_name,
                    timeout=timeout,
                    purpose=purpose,
                )
            time.sleep(_FILE_LOCK_RETRY_SECONDS)


def _acquire_file_lock(
    lock_file: BinaryIO,
    *,
    exclusive: bool,
    purpose: _FileLockPurpose,
) -> None:
    """Acquire a platform-native interprocess lock, loudly and boundedly.

    The first attempt is non-blocking. On contention a purpose-specific warning
    names the lock file, then acquisition polls until the env-tunable deadline
    so a stuck holder produces an actionable error instead of an indefinite
    silent hang.
    """
    if os.name == "nt":
        import msvcrt

        msvcrt_api: Any = msvcrt
        _ensure_windows_lock_byte(lock_file)
        lock_name = getattr(lock_file, "name", "<unknown>")
        warned = False
        deadline: float | None = None
        timeout: float | None = None
        while True:
            lock_file.seek(0)
            try:
                # msvcrt exposes only exclusive byte-range locks. Serializing
                # Windows readers and writers preserves correctness while POSIX
                # keeps true shared-reader concurrency through flock below.
                msvcrt_api.locking(lock_file.fileno(), msvcrt_api.LK_NBLCK, 1)
                return
            except OSError as exc:
                if not _windows_lock_is_contended(exc):
                    raise
                if not warned:
                    timeout = _file_lock_timeout_seconds(purpose)
                    deadline = time.monotonic() + timeout
                    _warn_file_lock_contended(
                        lock_name,
                        exclusive=exclusive,
                        timeout=timeout,
                        purpose=purpose,
                    )
                    warned = True
                assert deadline is not None and timeout is not None
                if time.monotonic() >= deadline:
                    _raise_file_lock_timeout(
                        lock_name,
                        timeout=timeout,
                        purpose=purpose,
                    )
                time.sleep(_FILE_LOCK_RETRY_SECONDS)

    _acquire_posix_flock(
        lock_file.fileno(),
        lock_name=getattr(lock_file, "name", "<unknown>"),
        exclusive=exclusive,
        purpose=purpose,
    )


def _release_file_lock(lock_file: BinaryIO) -> None:
    """Release the matching platform-native interprocess lock."""
    if os.name == "nt":
        import msvcrt

        msvcrt_api: Any = msvcrt
        lock_file.seek(0)
        msvcrt_api.locking(lock_file.fileno(), msvcrt_api.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _lambda_asset_lock(build_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Serialize publishers and keep freshness reads off rename windows."""
    build_dir.parent.mkdir(parents=True, exist_ok=True)
    lock_path = build_dir.with_name(f".{build_dir.name}.lock")
    lock_key = os.path.normcase(os.path.abspath(lock_path))
    held = _thread_asset_locks()
    existing = held.get(lock_key)
    if existing is not None:
        held_exclusive, depth = existing
        if exclusive and not held_exclusive:
            raise RuntimeError(f"Cannot upgrade shared asset lock to exclusive: {lock_path}")
        held[lock_key] = (held_exclusive, depth + 1)
        try:
            yield
        finally:
            held[lock_key] = (held_exclusive, depth)
        return

    with lock_path.open("a+b") as lock_file:
        _acquire_file_lock(lock_file, exclusive=exclusive, purpose="asset")
        held[lock_key] = (exclusive, 1)
        try:
            yield
        finally:
            held.pop(lock_key, None)
            _release_file_lock(lock_file)


def _asset_build_is_fresh(
    source_dir: Path,
    build_dir: Path,
    *,
    source_inputs: tuple[str, ...] | None,
) -> bool:
    with _lambda_asset_lock(build_dir, exclusive=False):
        return _asset_build_is_fresh_unlocked(
            source_dir,
            build_dir,
            source_inputs=source_inputs,
        )


def _remove_asset_tree(path: Path) -> None:
    if path.exists() or path.is_symlink():
        _safe_rmtree(path)


def _recover_interrupted_asset_publish(build_dir: Path) -> None:
    """Restore a prior final tree and discard abandoned staging directories."""
    staging_dirs = list(build_dir.parent.glob(f".{build_dir.name}.staging-*"))
    backup_dirs = list(build_dir.parent.glob(f".{build_dir.name}.backup-*"))

    if not build_dir.exists() and backup_dirs:
        try:
            newest_backup = max(backup_dirs, key=lambda path: path.stat().st_mtime_ns)
        except OSError:
            newest_backup = backup_dirs[0]
        os.replace(newest_backup, build_dir)

    for path in [*staging_dirs, *backup_dirs]:
        _remove_asset_tree(path)


def _publish_staged_asset(staging_dir: Path, build_dir: Path) -> None:
    """Publish one complete staged tree with rollback to the previous final."""
    backup_dir = build_dir.with_name(f".{build_dir.name}.backup-{uuid.uuid4().hex}")
    had_previous = build_dir.exists()
    if had_previous:
        os.replace(build_dir, backup_dir)
    try:
        os.replace(staging_dir, build_dir)
    except Exception:
        if had_previous and backup_dir.exists() and not build_dir.exists():
            os.replace(backup_dir, build_dir)
        raise
    if backup_dir.exists():
        _remove_asset_tree(backup_dir)


def _prepare_lambda_asset(
    source_dir: Path,
    build_dir: Path,
    *,
    source_inputs: tuple[str, ...] | None,
    display_name: str,
    builder: Callable[[Path], None],
) -> bool:
    """Build and atomically publish an asset when its completion proof is stale.

    Freshness is checked under a *shared* lock first, so the common case —
    the asset is already source-current — never contends: concurrent pytest
    workers validate in parallel instead of serialising behind one writer,
    and a deploy/destroy against fresh assets never blocks on a pytest
    session's session-long shared locks. Only a genuinely stale asset
    escalates to the exclusive publisher lock, which re-checks freshness
    after acquisition (another publisher may have finished the same rebuild
    while this one waited).
    """
    if _asset_build_is_fresh(source_dir, build_dir, source_inputs=source_inputs):
        return False
    with _lambda_asset_lock(build_dir, exclusive=True):
        _recover_interrupted_asset_publish(build_dir)
        source_digest = _asset_tree_digest(source_dir, source_inputs=source_inputs)
        if source_digest is None:
            raise RuntimeError(f"{display_name} source inputs are incomplete or unreadable")
        if _asset_build_is_fresh_unlocked(
            source_dir,
            build_dir,
            source_inputs=source_inputs,
        ):
            return False

        print(f"  Building {display_name}...")
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f".{build_dir.name}.staging-", dir=build_dir.parent)
        )
        try:
            builder(staging_dir)
            if _asset_tree_digest(source_dir, source_inputs=source_inputs) != source_digest:
                raise RuntimeError(f"{display_name} sources changed while packaging")
            _write_build_manifest(staging_dir, source_digest)
            if not _asset_build_is_fresh_unlocked(
                source_dir,
                staging_dir,
                source_inputs=source_inputs,
            ):
                raise RuntimeError(f"{display_name} completion manifest verification failed")
            _publish_staged_asset(staging_dir, build_dir)
        finally:
            _remove_asset_tree(staging_dir)
        print(f"  {display_name} built successfully")
        return True


def _atomic_copy_file(source: Path, target: Path) -> None:
    """Replace one checked-in Lambda source copy without exposing partial bytes."""
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_write_bytes(target: Path, content: bytes, *, mode: int | None = None) -> None:
    """Atomically restore exact bytes without exposing a partial configuration."""
    temporary = target.with_name(f".{target.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_bytes(content)
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class ConfigMutationLockError(RuntimeError):
    """The shared cdk.json transaction lock could not be acquired."""


@contextmanager
def _config_process_lock(lock_key: Path) -> Iterator[None]:
    """Hold the platform-native process lock for one config directory."""
    if os.name == "nt":
        # Windows cannot open a directory for ``msvcrt.locking``. A persistent
        # sidecar in that directory gives every CLI/MCP process the same stable
        # inode even while cdk.json itself is atomically replaced.
        lock_path = lock_key / _CONFIG_LOCK_FILENAME
        lock_file: BinaryIO | None = None
        try:
            lock_file = lock_path.open("a+b")
            _acquire_file_lock(lock_file, exclusive=True, purpose="configuration")
        except OSError as exc:
            if lock_file is not None:
                lock_file.close()
            raise ConfigMutationLockError(
                f"could not lock configuration directory {lock_key}: {exc}"
            ) from exc

        try:
            yield
        finally:
            assert lock_file is not None
            try:
                _release_file_lock(lock_file)
            finally:
                lock_file.close()
        return

    # Keep the POSIX directory lock: unlike a lock on cdk.json, the descriptor
    # continues to identify the same object when an atomic writer replaces the
    # configuration file.
    import fcntl

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    lock_fd: int | None = None
    try:
        lock_fd = os.open(lock_key, flags)
        _acquire_posix_flock(
            lock_fd,
            lock_name=str(lock_key),
            exclusive=True,
            purpose="configuration",
        )
    except OSError as exc:
        if lock_fd is not None:
            os.close(lock_fd)
        raise ConfigMutationLockError(
            f"could not lock configuration directory {lock_key}: {exc}"
        ) from exc

    try:
        yield
    finally:
        assert lock_fd is not None
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)


@contextmanager
def _config_mutation_lock(path: Path) -> Iterator[None]:
    """Serialize a complete read/modify/replace transaction for ``path``.

    POSIX locks the stable directory descriptor; Windows locks a persistent
    sidecar in that directory. A thread-local held-set makes this context
    reentrant, which is required by analytics teardown: it holds the
    transaction across its temporary mutation and nested feature-toggle writes.
    """
    lock_key = path.parent.resolve()
    with _CONFIG_THREAD_LOCKS_GUARD:
        thread_lock = _CONFIG_THREAD_LOCKS.setdefault(lock_key, RLock())

    with thread_lock:
        held_directories = getattr(_CONFIG_LOCK_STATE, "held_directories", None)
        if held_directories is None:
            held_directories = set()
            _CONFIG_LOCK_STATE.held_directories = held_directories
        if lock_key in held_directories:
            yield
            return

        with _config_process_lock(lock_key):
            held_directories.add(lock_key)
            try:
                yield
            finally:
                held_directories.discard(lock_key)


@lru_cache(maxsize=1)
def _known_cloudformation_regions() -> frozenset[str]:
    """Return every AWS SDK-known Region that exposes CloudFormation."""
    return known_cloudformation_regions()


class CdkToolchainError(RuntimeError):
    """The CDK Python toolchain (``aws-cdk-lib`` / ``cdk-nag``) is not
    importable in the environment that will run ``cdk``.

    Raised before shelling out to ``cdk`` so operators get a clear install
    hint instead of the cryptic ``ImportError: cannot import name 'App' from
    'aws_cdk'`` that the ``python3 app.py`` synth subprocess would otherwise
    emit from a base (extra-less) install.
    """


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

    # Safety: refuse to remove anything that isn't clearly a final, staging,
    # or rollback Lambda build artifact inside the project tree.
    artifact_name = resolved.name
    is_final = artifact_name.endswith("-build")
    is_ephemeral = artifact_name.startswith(".") and (
        ".staging-" in artifact_name or ".backup-" in artifact_name
    )
    if "lambda" not in resolved.parts or not (is_final or is_ephemeral):
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


def prepare_cdk_assets(project_root: str | Path) -> None:
    """Prepare every ignored Lambda asset consumed by the CDK application.

    This is the shared entry point for build-only callers. CDK consumers must
    use :func:`cdk_asset_consumer` so the resulting paths remain immutable
    until app construction and synthesis finish.
    """
    manager = StackManager.__new__(StackManager)
    manager.project_root = Path(project_root)
    manager._ensure_lambda_build()


def _thread_asset_consumers() -> dict[str, int]:
    return _asset_thread_state.active_consumers


@contextmanager
def cdk_asset_consumer(project_root: str | Path) -> Iterator[None]:
    """Hold source-current generated assets stable through CDK synthesis.

    Preparation runs before any shared locks are acquired. The complete set of
    canonical paths is then locked in deterministic order and every completion
    manifest is revalidated while publishers are excluded. A stale observation
    releases all locks and retries preparation; repeated source churn fails
    closed instead of exposing CDK to a missing or mixed-version tree.
    """
    root = Path(project_root)
    root_key = os.path.normcase(os.path.abspath(root))
    active = _thread_asset_consumers()
    if root_key in active:
        active[root_key] += 1
        try:
            yield
        finally:
            active[root_key] -= 1
        return

    stale_assets: list[str] = []
    # Attempt 0 validates under shared locks without preparing anything: when
    # every asset is already source-current (always true in CI, where the
    # composite build action runs first, and true locally on any second run)
    # the consumer takes no exclusive lock and does one hash pass. Concurrent
    # consumers — xdist workers — therefore proceed in parallel instead of
    # serialising behind the publisher lock. Later attempts keep the original
    # prepare-then-revalidate budget for genuinely stale trees.
    for attempt in range(_CDK_ASSET_CONSUMER_MAX_ATTEMPTS + 1):
        if attempt:
            prepare_cdk_assets(root)
        resolved_assets = []
        for spec in _CDK_ASSET_SPECS:
            source_dir, build_dir = spec.paths(root)
            # Include a source-backed path even during the publisher's
            # final-to-backup rename gap, when the canonical build is absent.
            if source_dir.exists() or build_dir.exists():
                resolved_assets.append((spec, source_dir, build_dir))

        with ExitStack() as locks:
            for _spec, _source_dir, build_dir in sorted(
                resolved_assets,
                key=lambda item: str(item[2]),
            ):
                locks.enter_context(_lambda_asset_lock(build_dir, exclusive=False))

            stale_assets = [
                spec.name
                for spec, source_dir, build_dir in resolved_assets
                if not _asset_build_is_fresh_unlocked(
                    source_dir,
                    build_dir,
                    source_inputs=spec.source_inputs,
                )
            ]
            if stale_assets:
                continue

            active[root_key] = 1
            try:
                yield
            finally:
                active.pop(root_key, None)
            return

    names = ", ".join(stale_assets) or "unknown assets"
    raise RuntimeError(
        "Generated CDK assets changed repeatedly while acquiring consumer locks: "
        f"{names}. Stop concurrent source edits and retry."
    )


class StackManager:
    """Manages CDK stack operations."""

    def __init__(self, config: GCOConfig, project_root: Path | None = None):
        self.config = config
        self.project_root = project_root or self._find_project_root()
        # Resolve CDK only when a CDK-backed operation runs. CloudFormation-only
        # status/output commands must not require a local Node/CDK installation.
        self._cdk_path: str | None = None
        self._active_cdk_processes: dict[int, Any] = {}
        self._active_cdk_lock = Lock()
        self._cdk_cancel_event = Event()
        # Extra `--context key=value` pairs appended to every app-evaluating
        # CDK invocation (deploy/destroy/diff/list/synth). Set once via
        # set_extra_cdk_context; used by the live release validation harness
        # to force-enable optional Helm charts (helm_enabled_overrides) for a
        # run without mutating the checked-out cdk.json.
        self._extra_cdk_context: dict[str, str] = {}

    def set_extra_cdk_context(self, context: Mapping[str, str]) -> None:
        """Register `--context` pairs for every subsequent CDK invocation.

        Keys and values must be plain strings without shell metacharacters'
        risk (argv is passed as a list, never a shell string); a key that is
        empty or contains ``=`` is refused because it could not round-trip
        through the CDK CLI's ``key=value`` form unambiguously.
        """
        validated: dict[str, str] = {}
        for key, value in context.items():
            if not key or "=" in key:
                raise ValueError(f"Invalid CDK context key: {key!r}")
            validated[str(key)] = str(value)
        self._extra_cdk_context = validated

    def _find_project_root(self) -> Path:
        """Find the project root by looking for cdk.json."""
        current = Path.cwd()
        for parent in [current] + list(current.parents):
            if (parent / "cdk.json").exists():
                return parent
        return current

    def _find_cdk(self) -> str:
        """Find the dependency-locked CDK executable when available."""
        # Prefer the repository's locked tool when ``npm ci`` has populated it.
        local_cdk = self.project_root / "node_modules" / ".bin" / "cdk"
        if local_cdk.is_file():
            return str(local_cdk)

        # Fall back to PATH for installed distributions that do not include
        # the repository's root npm graph.
        try:
            result = subprocess.run(["which", "cdk"], capture_output=True, text=True, check=True)
            return result.stdout.strip()
        except subprocess.CalledProcessError:
            pass

        # Check common global-install locations.
        for path in ["/usr/local/bin/cdk", "~/.npm-global/bin/cdk"]:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                return expanded

        raise CdkToolchainError(
            "AWS CDK CLI is not installed. Run "
            "'npm ci --ignore-scripts --no-audit --no-fund' at the project root "
            "to install the dependency-locked CLI."
        )

    @staticmethod
    def _kubectl_build_is_fresh(source_dir: Path, build_dir: Path) -> bool:
        """Return whether the kubectl build has a valid full-tree completion proof."""
        return _asset_build_is_fresh(
            source_dir,
            build_dir,
            source_inputs=_KUBECTL_CDK_ASSET.source_inputs,
        )

    @staticmethod
    def _helm_build_is_fresh(source_dir: Path, build_dir: Path) -> bool:
        """Return whether the Helm build has a valid full-tree completion proof."""
        return _asset_build_is_fresh(
            source_dir,
            build_dir,
            source_inputs=_HELM_CDK_ASSET.source_inputs,
        )

    @staticmethod
    def _inference_streaming_build_is_fresh(source_dir: Path, build_dir: Path) -> bool:
        """Return whether the Node build has a valid full-tree completion proof."""
        return _asset_build_is_fresh(
            source_dir,
            build_dir,
            source_inputs=_INFERENCE_STREAMING_CDK_ASSET.source_inputs,
        )

    def _ensure_lambda_build(self) -> None:
        """Atomically prepare every generated Lambda asset when source-stale.

        Every builder takes its per-asset interprocess lock, repairs an
        interrupted publish, and rechecks freshness before doing installation
        work. Concurrent app evaluations therefore either reuse one complete
        final tree or publish another complete tree; they never share a
        directory while pip/npm/copy operations are mutating it.
        """
        for spec, builder in (
            (_KUBECTL_CDK_ASSET, self._build_kubectl_lambda),
            (_HELM_CDK_ASSET, self._build_helm_installer_lambda),
            (_INFERENCE_STREAMING_CDK_ASSET, self._build_inference_streaming_proxy_lambda),
        ):
            source_dir, _build_dir = spec.paths(self.project_root)
            if source_dir.exists():
                builder()

    def _check_and_fix_stuck_stack(
        self,
        stack_name: str,
        *,
        expected_stack_id: str | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        strict_ownership: bool = False,
    ) -> None:
        """Delete a stuck stack only after revalidating its immutable identity."""
        import boto3

        region = self._get_deploy_region(stack_name)
        if not region:
            if strict_ownership:
                raise RuntimeError(f"Could not resolve deploy Region for {stack_name}")
            return

        cfn = boto3.client("cloudformation", region_name=region)
        try:
            response = cfn.describe_stacks(StackName=stack_name)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if (
                error.get("Code") == "ValidationError"
                and "does not exist" in str(error.get("Message", "")).lower()
            ):
                return
            if strict_ownership:
                raise
            logger.debug("Stack pre-check for %s failed: %s", stack_name, exc)
            return
        except Exception as exc:
            if strict_ownership:
                raise
            logger.debug("Stack pre-check for %s failed: %s", stack_name, exc)
            return

        stacks = response.get("Stacks", [])
        if len(stacks) != 1:
            raise RuntimeError(f"CloudFormation returned an invalid identity for {stack_name}")
        stack = stacks[0]
        stack_id = str(stack.get("StackId") or "")
        if stack.get("StackName") != stack_name or not stack_id:
            raise RuntimeError(f"CloudFormation returned an invalid identity for {stack_name}")
        if strict_ownership and expected_stack_id is None:
            raise RuntimeError(
                f"Refusing to adopt uncheckpointed stack {region}:{stack_name} ({stack_id})"
            )
        if expected_stack_id is not None and stack_id != expected_stack_id:
            raise RuntimeError(
                f"Stack identity changed for {region}:{stack_name}; expected {expected_stack_id}, "
                f"found {stack_id}"
            )

        stuck_states = {
            "REVIEW_IN_PROGRESS",
            "ROLLBACK_COMPLETE",
            "ROLLBACK_FAILED",
            "CREATE_FAILED",
            "DELETE_FAILED",
        }
        status = str(stack.get("StackStatus") or "")
        if status not in stuck_states:
            return
        if authorize_stack is not None:
            authorize_stack(stack_name, region, stack_id)

        print(f"  Stack {stack_name} is in {status} state, cleaning up...")
        cfn.delete_stack(StackName=stack_id)
        waiter = cfn.get_waiter("stack_delete_complete")
        waiter.wait(StackName=stack_id, WaiterConfig={"Delay": 10, "MaxAttempts": 60})
        print(f"  Stack {stack_name} cleaned up, will recreate on deploy")

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
        """Atomically synchronize canonical shared files before asset ensures.

        Checked-in copies keep raw CDK evaluation deterministic. Deploy updates
        those copies before generated assets are checked, and never mutates a
        generated final build tree in place. The source->targets mapping lives
        in ``gco.lambda_shared_sources`` so deploy packaging, diagram
        reconciliation, and commit-time identity tests consume one
        dependency-light inventory.
        """
        if getattr(self, "_lambda_sources_synced", False):
            return

        for source_rel, target_rels in LAMBDA_SHARED_SOURCE_TARGETS.items():
            shared_source = self.project_root / source_rel
            if not shared_source.exists():
                continue
            for target_rel in target_rels:
                target = self.project_root / target_rel
                if target.parent.exists():
                    _atomic_copy_file(shared_source, target)
        self._lambda_sources_synced = True

    def _rebuild_lambda_packages(self) -> None:
        """Compatibility wrapper for a source-current atomic asset ensure."""
        if getattr(self, "_lambda_packages_rebuilt", False):
            return
        self._ensure_lambda_build()
        self._lambda_packages_rebuilt = True

    def _build_lambda_packages(self) -> None:
        """Source-check and atomically publish all generated Lambda packages."""
        self._build_kubectl_lambda()
        self._build_helm_installer_lambda()
        self._build_inference_streaming_proxy_lambda()

    def _build_kubectl_lambda(self) -> None:
        """Build the kubectl-applier-simple Lambda package."""
        source_dir, build_dir = _KUBECTL_CDK_ASSET.paths(self.project_root)
        requirements = source_dir / "requirements.txt"
        if not source_dir.is_dir() or not requirements.is_file():
            return

        def build(staging_dir: Path) -> None:
            shutil.copy2(source_dir / "handler.py", staging_dir / "handler.py")
            shutil.copy2(requirements, staging_dir / "requirements.txt")
            shutil.copytree(source_dir / "manifests", staging_dir / "manifests")
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - static pip arguments and project-owned paths
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    str(requirements),
                    "-t",
                    str(staging_dir),
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
                raise RuntimeError(
                    "kubectl Lambda dependency installation failed: " + result.stderr[:200]
                )

        _prepare_lambda_asset(
            source_dir,
            build_dir,
            source_inputs=_KUBECTL_CDK_ASSET.source_inputs,
            display_name="kubectl-applier-simple Lambda package",
            builder=build,
        )

    def _build_helm_installer_lambda(self) -> None:
        """Build the complete helm-installer Lambda Docker context."""
        source_dir, build_dir = _HELM_CDK_ASSET.paths(self.project_root)
        if not source_dir.is_dir():
            return

        def build(staging_dir: Path) -> None:
            shutil.copytree(
                source_dir,
                staging_dir,
                ignore=shutil.ignore_patterns(*_LAMBDA_SOURCE_COPY_IGNORE_PATTERNS),
                dirs_exist_ok=True,
            )

        _prepare_lambda_asset(
            source_dir,
            build_dir,
            source_inputs=_HELM_CDK_ASSET.source_inputs,
            display_name="helm-installer Lambda package",
            builder=build,
        )

    def _build_inference_streaming_proxy_lambda(self) -> None:
        """Build the Node.js streaming Lambda with its pinned AWS SDK clients."""
        source_dir, build_dir = _INFERENCE_STREAMING_CDK_ASSET.paths(self.project_root)
        if not source_dir.is_dir():
            return

        package_files = _INFERENCE_STREAMING_CDK_ASSET.source_inputs
        assert package_files is not None
        missing = [name for name in package_files if not (source_dir / name).is_file()]
        if missing:
            raise RuntimeError(
                "Inference streaming Lambda package is incomplete; missing: " + ", ".join(missing)
            )
        try:
            package_manager = str(
                json.loads((source_dir / "package.json").read_text(encoding="utf-8")).get(
                    "packageManager", ""
                )
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Unable to read the inference streaming Lambda npm pin") from exc
        required_npm = package_manager.removeprefix("npm@")
        version_parts = required_npm.split(".")
        if (
            not package_manager.startswith("npm@")
            or len(version_parts) != 3
            or any(not part.isdigit() for part in version_parts)
        ):
            raise RuntimeError(
                "Inference streaming Lambda packageManager must pin an exact npm version"
            )

        def build(staging_dir: Path) -> None:
            npm = shutil.which("npm")
            if npm is None:
                raise RuntimeError(
                    f"npm {required_npm} is required to package the inference streaming Lambda; "
                    "install the Node.js version pinned in .nvmrc"
                )
            try:
                version_result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - resolved executable and project-owned cwd
                    [npm, "--version"],
                    cwd=source_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("Unable to verify the npm packaging version") from exc
            actual_npm = version_result.stdout.strip()
            if version_result.returncode != 0 or actual_npm != required_npm:
                found = actual_npm or "unavailable"
                raise RuntimeError(
                    f"npm {required_npm} is required to package the inference streaming Lambda; "
                    f"found {found}. Run: npm install --global npm@{required_npm}"
                )

            for name in package_files:
                shutil.copy2(source_dir / name, staging_dir / name)
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - resolved npm path, static arguments, and project-owned cwd
                [
                    npm,
                    "ci",
                    "--omit=dev",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                ],
                cwd=staging_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    "Failed to install pinned inference streaming Lambda dependencies: "
                    + result.stderr[:500]
                )

        _prepare_lambda_asset(
            source_dir,
            build_dir,
            source_inputs=package_files,
            display_name="inference-streaming-proxy Lambda package",
            builder=build,
        )

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

    def _ensure_cdk_toolchain(self) -> None:
        """Preflight the CDK Python toolchain before invoking ``cdk``.

        Infra operations run ``python3 app.py`` (via the Node ``cdk`` CLI),
        which imports ``aws_cdk`` and ``cdk_nag``. Those ship in the optional
        ``[cdk]`` extra — a base ``uvx`` / ``pip`` install of ``gco-cli`` does
        not include them, so the synth subprocess fails with a cryptic
        ``ImportError: cannot import name 'App' from 'aws_cdk'``. Detect the
        missing toolchain up front and raise :class:`CdkToolchainError` with an
        actionable install hint instead.
        """
        missing = [m for m in _CDK_TOOLCHAIN_MODULES if importlib.util.find_spec(m) is None]
        if not missing:
            return
        raise CdkToolchainError(
            "CDK toolchain not available: cannot import "
            + ", ".join(missing)
            + ".\nInfrastructure operations (deploy / synth / diff / list / destroy / "
            "bootstrap) need the CDK Python packages installed in the SAME "
            "environment as the `gco` CLI, plus a repository checkout providing "
            "`app.py` and `cdk.json`.\n"
            "Install the `[cdk]` extra one of these ways:\n"
            '  - uv:  uv tool install "gco-cli[cdk] @ '
            'git+https://github.com/aws-solutions-library-samples/global-capacity-orchestrator-on-aws.git@<tag>"\n'
            '  - pip: pip install -e ".[cdk,mcp]"   (from a clone)\n'
            "  - or use the dev container (see QUICKSTART.md), which bundles the "
            "full toolchain.\n"
            "See gco_mcp/README.md (Setup) for the deploy-capable configuration."
        )

    @staticmethod
    def _terminate_cdk_process(process: Any) -> None:
        """Terminate one complete CDK process tree with a bounded grace period."""
        if process.poll() is not None:
            return

        if os.name == "nt":
            taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")

            def terminate_tree(*, force: bool) -> bool:
                if taskkill is None:
                    return False
                command = [taskkill, "/PID", str(process.pid), "/T"]
                if force:
                    command.append("/F")
                try:
                    result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit - resolved Windows system utility and numeric child PID
                        command,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except OSError, subprocess.TimeoutExpired:
                    return False
                return result.returncode == 0

            terminate_tree(force=False)
            try:
                process.wait(timeout=30)
            except OSError, subprocess.TimeoutExpired:
                terminate_tree(force=True)
                if process.poll() is None:
                    process.kill()
                process.wait()
            return

        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=30)
        except OSError, subprocess.TimeoutExpired:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                finally:
                    process.wait()

    def cancel_active_cdk_processes(self) -> None:
        """Prevent new CDK work and terminate every process group currently registered."""
        self._cdk_cancel_event.set()
        with self._active_cdk_lock:
            processes = list(self._active_cdk_processes.values())
        for process in processes:
            self._terminate_cdk_process(process)

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
        # Fail fast with an actionable message when the CDK Python toolchain
        # isn't importable (e.g. a base uvx/pip install without the [cdk]
        # extra), instead of letting the ``python3 app.py`` subprocess surface
        # a cryptic ImportError.
        self._ensure_cdk_toolchain()

        # These commands all evaluate app.py, including list and destroy. The
        # stack graph references ignored generated Lambda assets, so prepare
        # them centrally rather than relying on individual command wrappers.
        if command and command[0] in {"deploy", "destroy", "diff", "list", "synth"}:
            self._ensure_lambda_build()
            # Apply registered context overrides uniformly to every
            # app-evaluating command so deploy, destroy, and the stack listing
            # all synthesize the same graph (see set_extra_cdk_context).
            for key, value in sorted(self._extra_cdk_context.items()):
                command = [*command, "--context", f"{key}={value}"]

        full_env = os.environ.copy()

        # Inject PYTHONPATH so CDK's python3 subprocess can find aws_cdk
        # This is essential for pipx installations
        full_env["PYTHONPATH"] = self._get_python_path()

        if env:
            full_env.update(env)

        cdk_path = self._cdk_path
        if cdk_path is None:
            cdk_path = self._find_cdk()
            self._cdk_path = cdk_path
        cdk_cmd = [cdk_path, *command]

        if self._cdk_cancel_event.is_set():
            raise RuntimeError("CDK operation cancelled before process start")
        popen_kwargs: dict[str, Any] = {
            "cwd": self.project_root,
            "stdout": subprocess.PIPE if capture_output else None,
            "stderr": subprocess.PIPE if capture_output else None,
            "text": True,
            "env": full_env,
            "start_new_session": os.name == "posix",
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(
                subprocess,
                "CREATE_NEW_PROCESS_GROUP",
                0,
            )
        process = subprocess.Popen(  # nosemgrep: dangerous-subprocess-use-audit - static CDK argv, no shell
            cdk_cmd,
            **popen_kwargs,
        )
        with self._active_cdk_lock:
            self._active_cdk_processes[process.pid] = process
        if self._cdk_cancel_event.is_set():
            self._terminate_cdk_process(process)
            with self._active_cdk_lock:
                self._active_cdk_processes.pop(process.pid, None)
            raise RuntimeError("CDK operation cancelled during process start")

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._terminate_cdk_process(process)
            logger.warning(
                "cdk command timed out after %ss: %s",
                timeout,
                " ".join(cdk_cmd),
            )
            raise subprocess.TimeoutExpired(
                cdk_cmd,
                exc.timeout,
                output=exc.output,
                stderr=exc.stderr,
            ) from exc
        except BaseException:
            self._terminate_cdk_process(process)
            raise
        finally:
            with self._active_cdk_lock:
                self._active_cdk_processes.pop(process.pid, None)
        return subprocess.CompletedProcess(
            cdk_cmd,
            process.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )

    def list_stacks(self) -> list[str]:
        """List all available CDK stacks."""
        result = self._run_cdk(["list"], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to list stacks: {result.stderr}")
        return [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]

    def synth(self, stack_name: str | None = None, quiet: bool = True) -> str:
        """Synthesize CloudFormation templates from source-current assets."""
        self._ensure_lambda_build()
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
        """Show diff between deployed and source-current local stacks."""
        self._ensure_lambda_build()
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
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
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
        # Synchronize canonical checked-in copies first, then source-check and
        # atomically publish only stale generated assets. A deploy must never
        # destructively rebuild a fresh tree while another CDK process may be
        # fingerprinting it.
        self._sync_lambda_sources()
        self._ensure_lambda_build()

        strict_deployment = (
            strict_deployment_token is not None or on_change_set_prepared is not None
        )
        expected_stack_id: str | None = None
        prepared_change_set_records: Mapping[str, Mapping[str, str]] = {}
        change_set_name: str | None = None
        if strict_deployment:
            if not stack_name or all_stacks:
                raise RuntimeError("Strict deployment requires exactly one named stack")
            if not strict_deployment_token or on_change_set_prepared is None:
                raise RuntimeError(
                    "Strict deployment requires both a run token and a prepared-change-set callback"
                )
            if allow_bootstrap:
                raise RuntimeError("Strict deployment cannot auto-bootstrap a Region")
            if authorize_stack is None:
                raise RuntimeError("Strict deployment requires an exact stack authorizer")
            if expected_stack_ids is None or stack_name not in expected_stack_ids:
                raise RuntimeError(
                    f"Strict deployment lacks authoritative target state for {stack_name}"
                )
            expected_stack_id = expected_stack_ids[stack_name]
            if prepared_change_sets is None or stack_name not in prepared_change_sets:
                raise RuntimeError(
                    f"Strict deployment lacks prepared change-set history for {stack_name}"
                )
            prepared_change_set_records = prepared_change_sets[stack_name]
            change_set_name = self._strict_change_set_name(
                stack_name,
                strict_deployment_token,
            )

        # Validate bootstrap identity before any AWS mutation. In strict mode
        # this also revalidates the expected stack ARN (or authoritative
        # absence) before image mirroring or change-set preparation.
        if stack_name:
            region = self._get_deploy_region(stack_name)
            if not region:
                raise RuntimeError(f"Could not resolve deploy Region for {stack_name}")
            if allow_bootstrap:
                if not self.ensure_bootstrapped(region):
                    raise RuntimeError(
                        f"Region {region} could not be bootstrapped. "
                        "Run 'gco stacks bootstrap --region "
                        f"{region}' manually to diagnose."
                    )
            else:
                expected_bootstrap = (bootstrap_stacks or {}).get(region)
                if expected_bootstrap is None:
                    raise RuntimeError(
                        f"Strict deployment lacks a checkpointed CDKToolkit identity for {region}"
                    )
                self._validate_bootstrap_stack(region, expected_bootstrap)
            if strict_deployment:
                target = self._describe_stack_target(
                    stack_name,
                    expected_stack_id=expected_stack_id,
                    require_expected_identity=True,
                )
                if expected_stack_id is not None and target is None:
                    raise RuntimeError(
                        f"Checkpointed stack {expected_stack_id} is absent; refusing recreation"
                    )
                assert change_set_name is not None
                self._preflight_strict_change_set(
                    stack_name=stack_name,
                    change_set_name=change_set_name,
                    expected_stack_id=expected_stack_id,
                    prepared_change_sets=prepared_change_set_records,
                )

        # Name-based stuck-stack recovery is intentionally disabled for strict
        # deployments. A prepared change set must establish CREATE-vs-UPDATE
        # authority without deleting or adopting anything by name.
        if stack_name and not strict_deployment:
            self._check_and_fix_stuck_stack(
                stack_name,
                expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                authorize_stack=authorize_stack,
                strict_ownership=not allow_bootstrap,
            )

        # Ensure container runtime is available for building images
        runtime = _detect_container_runtime()
        if not runtime:
            from cli._container_runtime import container_runtime_error_message

            raise RuntimeError(container_runtime_error_message())

        # Mirror third-party images into ECR only after strict bootstrap and
        # target checks. Repository creation acknowledgements are persisted
        # synchronously by the live-validation callback before any image copy.
        self._mirror_images_if_enabled(
            stack_name=stack_name,
            all_stacks=all_stacks,
            repository_tags=tags,
            on_repository_created=on_ecr_repository_created,
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

        if strict_deployment:
            assert change_set_name is not None
            cmd.extend(
                [
                    "--method",
                    "prepare-change-set",
                    "--change-set-name",
                    change_set_name,
                    "--context",
                    f"{_LIVE_VALIDATION_PROVIDER_LOG_CONTEXT}=true",
                ]
            )

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

        # Timestamp (UTC) marking the start of this deploy attempt. The failure
        # reconciliation below uses it to tell a *fresh* CloudFormation
        # completion (cdk's client-side polling gave up just after CFN finished
        # — a real success) apart from a *stale* terminal state left by a
        # previous deploy (cdk failed before touching CloudFormation — a real
        # failure that must not be masked).
        deploy_start = datetime.now(UTC)

        try:
            result = self._run_cdk(cmd, env=env, timeout=timeout_s)
            success = result.returncode == 0
        except subprocess.TimeoutExpired:
            print(
                f"  cdk deploy timed out after {timeout_s}s for "
                f"{stack_name or 'all stacks'}; verifying CloudFormation state..."
            )
            success = False

        if self._cdk_cancel_event.is_set():
            raise RuntimeError("CDK deployment cancelled before AWS-side reconciliation")

        if strict_deployment:
            assert stack_name is not None
            assert change_set_name is not None
            assert on_change_set_prepared is not None
            try:
                success = self._execute_prepared_change_set(
                    stack_name=stack_name,
                    change_set_name=change_set_name,
                    expected_stack_id=expected_stack_id,
                    expected_tags=tags,
                    prepared_change_sets=prepared_change_set_records,
                    preparation_succeeded=success,
                    authorize_stack=authorize_stack,
                    on_change_set_prepared=on_change_set_prepared,
                    allow_noop=success,
                    timeout=timeout_s,
                )
            except Exception:
                self._diagnose_deploy_failure(stack_name)
                raise
            if not success:
                self._diagnose_deploy_failure(stack_name)

            if success and "analytics" in stack_name:
                api_gateway_stack = f"{self.config.project_name}-api-gateway"
                print(f"  Updating {api_gateway_stack} with analytics routes...")
                success = self.deploy(
                    stack_name=api_gateway_stack,
                    require_approval=require_approval,
                    outputs_file=outputs_file,
                    parameters=parameters,
                    tags=tags,
                    progress=progress,
                    exclusively=True,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=(f"{strict_deployment_token}-analytics-routes"),
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
            return success

        # Reconcile a cdk failure/timeout against CloudFormation. cdk's
        # client-side polling can give up (a transient ``read EADDRNOTAVAIL``
        # socket error, or our wall-clock timeout) while CloudFormation keeps
        # working server-side, so a non-zero exit does not always mean the
        # deploy failed. The trick is to reconcile without masking a *real*
        # failure by mistaking a stale terminal state for a fresh success.
        if stack_name and not all_stacks and not success:
            cfn_status = self._get_stack_status(stack_name)
            if cfn_status is not None and cfn_status.endswith("_IN_PROGRESS"):
                # CloudFormation is still mid-operation — observing that is
                # itself proof it ran an operation for this attempt. Wait for it
                # to settle and accept a terminal COMPLETE as a genuine success.
                print(
                    f"  cdk exited non-zero but {stack_name} is {cfn_status} in "
                    "CloudFormation; waiting for the operation to settle..."
                )
                settled_status = self._wait_for_stack_settle(stack_name)
                if settled_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                    print(
                        f"  cdk reported a non-zero exit but {stack_name} settled "
                        f"to {settled_status} in CloudFormation — treating as "
                        "success."
                    )
                    success = True
            elif cfn_status in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                # The stack is already terminal and CloudFormation is not
                # mid-flight. Two very different situations look identical on
                # status alone; only the stack's last-operation time tells them
                # apart:
                #   * cdk's polling gave up just *after* CloudFormation finished
                #     this attempt's operation — a genuine success whose
                #     last-update time is newer than when we started.
                #   * cdk failed *before* it ever touched CloudFormation (a
                #     synth error, a cloud-assembly schema mismatch, an
                #     asset/image build failure); the stack is merely sitting in
                #     a *previous* deploy's COMPLETE state, whose last-update
                #     time predates this attempt. Masking this is the
                #     false-success bug this guards against.
                last_op = self._get_stack_last_update_time(stack_name)
                if last_op is not None and last_op >= deploy_start:
                    print(
                        f"  cdk reported a non-zero exit but {stack_name} shows a "
                        f"fresh {cfn_status} in CloudFormation — treating as "
                        "success."
                    )
                    success = True
                else:
                    print(
                        f"  cdk failed and {stack_name} is {cfn_status}, but no "
                        "new CloudFormation operation ran for this attempt — cdk "
                        "failed before touching CloudFormation. Treating as a "
                        "failed deploy."
                    )

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
            api_gateway_stack = f"{self.config.project_name}-api-gateway"
            print(f"  Updating {api_gateway_stack} with analytics routes...")
            success = self.deploy(
                stack_name=api_gateway_stack,
                require_approval=require_approval,
                outputs_file=outputs_file,
                parameters=parameters,
                tags=tags,
                progress=progress,
                exclusively=True,
                allow_bootstrap=allow_bootstrap,
                bootstrap_stacks=bootstrap_stacks,
                expected_stack_ids=expected_stack_ids,
                authorize_stack=authorize_stack,
                on_ecr_repository_created=on_ecr_repository_created,
            )

        return success

    def destroy(
        self,
        stack_name: str | None = None,
        all_stacks: bool = False,
        force: bool = False,
        output_dir: str | None = None,
        expected_stack_id: str | None = None,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
    ) -> bool:
        """Destroy stacks while restoring any temporary config mutation exactly."""
        config_path: Path | None = None
        if stack_name and not all_stacks and "analytics" in stack_name:
            config_path = _find_cdk_json()
            if config_path is None:
                raise RuntimeError("cdk.json not found before analytics destroy")

        # Analytics teardown may temporarily enable a disabled stack in the CDK
        # app. Hold the shared configuration transaction through restore so a
        # concurrent CLI/MCP edit cannot be silently overwritten by the exact-
        # bytes rollback in ``finally``.
        lock_context = (
            _config_mutation_lock(config_path) if config_path is not None else nullcontext()
        )
        with lock_context:
            original_bytes: bytes | None = None
            original_mode: int | None = None
            if config_path is not None:
                original_bytes = config_path.read_bytes()
                original_mode = stat.S_IMODE(config_path.stat().st_mode)
            try:
                return self._destroy(
                    stack_name=stack_name,
                    all_stacks=all_stacks,
                    force=force,
                    output_dir=output_dir,
                    expected_stack_id=expected_stack_id,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
            finally:
                if config_path is not None and original_bytes is not None:
                    _atomic_write_bytes(config_path, original_bytes, mode=original_mode)

    def _destroy(
        self,
        stack_name: str | None = None,
        all_stacks: bool = False,
        force: bool = False,
        output_dir: str | None = None,
        expected_stack_id: str | None = None,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
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
        if all_stacks and (expected_stack_id is not None or expected_stack_ids is not None):
            raise RuntimeError("Identity-fenced teardown cannot use all_stacks=True")
        if stack_name is None and (expected_stack_id is not None or expected_stack_ids is not None):
            raise RuntimeError("Identity-fenced teardown requires exactly one named stack")

        strict_identity = expected_stack_id is not None or expected_stack_ids is not None
        if stack_name is not None and expected_stack_ids is not None:
            if stack_name not in expected_stack_ids:
                raise RuntimeError(
                    f"Strict teardown lacks authoritative target state for {stack_name}"
                )
            mapped_stack_id = expected_stack_ids[stack_name]
            if expected_stack_id is not None and expected_stack_id != mapped_stack_id:
                raise RuntimeError(f"Conflicting expected stack identities for {stack_name}")
            expected_stack_id = mapped_stack_id
        if strict_identity and authorize_stack is None:
            raise RuntimeError("Identity-fenced teardown requires an exact stack authorizer")

        # Image-registry pre-destroy guards. Only fires for the global
        # stack (where the registry lives) and only when the operator
        # has explicitly chosen ``removal_policy: "destroy"``. The
        # default ``retain`` posture is a no-op here. See
        # ``_image_registry_destroy_preflight`` for the exact rules.
        if (
            stack_name is not None
            and stack_name.endswith("-global")
            and not all_stacks
            and not self._image_registry_destroy_preflight(force=force)
        ):
            return False

        # Strict callers never enter CDK's name-based destroy or toggle-based
        # recovery paths. Analytics may first require one strict prepared
        # change set on the exact API stack to remove cross-stack imports.
        if strict_identity and stack_name and not all_stacks:
            if self._cdk_cancel_event.is_set():
                raise RuntimeError(f"Strict teardown cancelled before deleting {stack_name}")
            if "analytics" in stack_name:
                if expected_stack_ids is None:
                    raise RuntimeError(
                        "Identity-fenced analytics teardown requires the complete expected "
                        "stack identity map"
                    )
                safe_to_destroy = self._remove_api_gateway_analytics_dependency(
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=(
                        f"{strict_deployment_token}-drop-analytics-routes"
                        if strict_deployment_token is not None
                        else None
                    ),
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
                if not safe_to_destroy:
                    return False
            return self._cloudformation_delete_stack(
                stack_name,
                expected_stack_id=expected_stack_id,
                authorize_stack=authorize_stack,
                require_expected_identity=True,
            )

        # A regional API bridge disappears from the CDK app when its Region is
        # removed from configuration. Only this exact project-scoped shape with
        # an SDK-known CloudFormation Region may bypass CDK; configured bridges,
        # arbitrary suffixes, and every other stack keep the normal CDK path.
        if stack_name and not all_stacks:
            orphan_region = self._get_orphan_regional_api_region(stack_name)
            if orphan_region is not None:
                if not self._stack_exists_in_cloudformation(stack_name):
                    return True
                print(
                    f"  {stack_name} is absent from the configured CDK app; "
                    f"deleting it directly in {orphan_region}..."
                )
                return self._cloudformation_delete_stack(
                    stack_name,
                    expected_stack_id=expected_stack_id,
                    authorize_stack=authorize_stack,
                )

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
            safe_to_destroy = self._remove_api_gateway_analytics_dependency(
                allow_bootstrap=allow_bootstrap,
                bootstrap_stacks=bootstrap_stacks,
                expected_stack_ids=expected_stack_ids,
                prepared_change_sets=prepared_change_sets,
                authorize_stack=authorize_stack,
                strict_deployment_token=strict_deployment_token,
                on_change_set_prepared=on_change_set_prepared,
                on_ecr_repository_created=on_ecr_repository_created,
            )
            if not safe_to_destroy:
                # Restore analytics toggle before bailing out.
                if toggle_restored:
                    self._restore_analytics_disabled()
                project = self.config.project_name
                print(
                    f"  Aborting {project}-analytics destroy: {project}-api-gateway "
                    "still imports analytics exports. Fix the API gateway and retry."
                )
                return False

        # Non-strict callers may still use CDK's name-based path. Strict calls
        # returned above after exact-ARN deletion.
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
        # 90 minutes: a healthy EKS regional teardown has been observed
        # needing ~60 (the VPC Lambda ENI detach alone can serialise for
        # 20+ while CloudFormation keeps making progress), and the prior
        # 45-minute cap killed the poller mid-delete — the AWS-side
        # reconciliation below recovered, but the timeout should mark a
        # wedged CDK, not a normal teardown. Override via
        # GCO_CDK_DESTROY_TIMEOUT_SECONDS.
        timeout_s = float(os.environ.get("GCO_CDK_DESTROY_TIMEOUT_SECONDS", "5400"))

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

        if self._cdk_cancel_event.is_set():
            raise RuntimeError("CDK teardown cancelled before AWS-side reconciliation")

        # Restore the toggle if we changed it
        if toggle_restored:
            self._restore_analytics_disabled()

        # Reconcile against CloudFormation. A local CDK timeout/failure is not
        # an AWS failure when the delete operation is still healthy. Once AWS
        # reports DELETE_IN_PROGRESS, wait for bounded server-side convergence
        # instead of letting the orchestrator advance into dependent stacks.
        if stack_name and not all_stacks:
            still_present = self._stack_exists_in_cloudformation(stack_name)
            if not still_present:
                if not cdk_succeeded:
                    print(
                        f"  cdk reported a non-zero exit but {stack_name} is "
                        "already deleted in CloudFormation — treating as success."
                    )
                return True

            status = self._get_stack_status(stack_name, expected_stack_id)
            if status == "DELETE_IN_PROGRESS":
                return self._wait_for_stack_delete_convergence(
                    stack_name,
                    initial_status=status,
                )
            if status == "DELETE_FAILED":
                self._print_stack_delete_heartbeat(
                    stack_name,
                    status,
                    expected_stack_id,
                )
                print(f"  {stack_name} reached DELETE_FAILED; refusing to continue teardown.")
                return False

            # A zero CDK exit with a still-present, non-deleting stack is a rare
            # client-side false success. Start deletion directly and then use
            # the same bounded convergence loop. A non-zero exit in any other
            # state means CDK failed before it started a delete operation.
            if cdk_succeeded:
                return self._cloudformation_delete_stack(stack_name)
            print(
                f"  cdk failed and {stack_name} is still {status or 'in an unknown state'}; "
                "no active CloudFormation delete operation was confirmed."
            )
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
                f"Repos under {self.config.project_name}/* are not empty and "
                "empty_on_delete is false. Run 'gco images cleanup --all' "
                "first, or set images.empty_on_delete: true in cdk.json."
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
            response = input(
                f"Destroy {self.config.project_name}-global and delete every "
                f"{self.config.project_name}/* repo? [y/N]: "
            )
        except EOFError, KeyboardInterrupt:
            print("Aborted.")
            return False
        if response.strip().lower() not in ("y", "yes"):
            print("Aborted.")
            return False
        return True

    @staticmethod
    def _stack_missing(exc: ClientError) -> bool:
        error = exc.response.get("Error", {})
        return bool(
            error.get("Code") == "ValidationError"
            and "does not exist" in str(error.get("Message", "")).lower()
        )

    @staticmethod
    def _change_set_missing(exc: ClientError) -> bool:
        """Return whether CloudFormation authoritatively reports an absent change set."""
        return bool(exc.response.get("Error", {}).get("Code") == "ChangeSetNotFound")

    def _describe_stack_target(
        self,
        stack_name: str,
        *,
        expected_stack_id: str | None = None,
        require_expected_identity: bool = False,
    ) -> tuple[str, Any, dict[str, Any]] | None:
        """Resolve live/absent/tombstone/replacement state without name adoption."""
        import boto3

        region = self._get_destroy_region(stack_name)
        cfn = boto3.client("cloudformation", region_name=region)

        def describe(identifier: str) -> dict[str, Any] | None:
            try:
                response = cfn.describe_stacks(StackName=identifier)
            except ClientError as exc:
                if self._stack_missing(exc):
                    return None
                raise
            stacks = response.get("Stacks", [])
            if len(stacks) != 1:
                raise RuntimeError(f"CloudFormation returned an invalid identity for {stack_name}")
            stack = stacks[0]
            if not isinstance(stack, dict):
                raise RuntimeError(f"CloudFormation returned an invalid identity for {stack_name}")
            stack_id = str(stack.get("StackId") or "")
            if stack.get("StackName") != stack_name or not stack_id:
                raise RuntimeError(f"CloudFormation returned an invalid identity for {stack_name}")
            return stack

        exact = describe(expected_stack_id) if expected_stack_id else None
        if exact is not None and str(exact.get("StackStatus") or "") != "DELETE_COMPLETE":
            if str(exact.get("StackId") or "") != expected_stack_id:
                raise RuntimeError(f"Stack identity changed for {region}:{stack_name}")
            return region, cfn, exact

        by_name = describe(stack_name)
        if by_name is None or str(by_name.get("StackStatus") or "") == "DELETE_COMPLETE":
            return None
        actual_id = str(by_name.get("StackId") or "")
        if expected_stack_id is not None and actual_id != expected_stack_id:
            raise RuntimeError(
                f"Checkpointed stack {expected_stack_id} is absent or deleted but same-name "
                f"replacement {actual_id} exists; refusing adoption"
            )
        if expected_stack_id is None and require_expected_identity:
            raise RuntimeError(
                f"Refusing name-authorized access to uncheckpointed stack "
                f"{region}:{stack_name} ({actual_id})"
            )
        return region, cfn, by_name

    def _stack_exists_in_cloudformation(
        self,
        stack_name: str,
        expected_stack_id: str | None = None,
        *,
        require_expected_identity: bool = False,
    ) -> bool:
        """Return whether the exact live target exists, rejecting replacements."""
        target = self._describe_stack_target(
            stack_name,
            expected_stack_id=expected_stack_id,
            require_expected_identity=require_expected_identity,
        )
        return target is not None

    def _get_stack_status(
        self,
        stack_name: str,
        stack_identifier: str | None = None,
    ) -> str | None:
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
            resp = cfn.describe_stacks(StackName=stack_identifier or stack_name)
            return str(resp["Stacks"][0]["StackStatus"])
        except Exception:
            return None

    def _get_stack_last_update_time(self, stack_name: str) -> datetime | None:
        """Return the UTC time of ``stack_name``'s most recent CloudFormation
        operation, or None if the stack is absent or the lookup fails.

        Uses ``LastUpdatedTime`` when the stack has been updated at least once,
        falling back to ``CreationTime`` for a stack that has only ever been
        created. ``deploy()`` compares this against the moment the deploy
        attempt started to decide whether a cdk failure/timeout that leaves the
        stack ``*_COMPLETE`` reflects a *fresh* operation (cdk's polling merely
        gave up early — success) or a *stale* one left by a previous deploy
        (cdk failed before touching CloudFormation — a real failure). A None
        return keeps the conservative 'cdk's failure stands' verdict.
        """
        import boto3

        try:
            region = self._get_destroy_region(stack_name)
            cfn = boto3.client("cloudformation", region_name=region)
            resp = cfn.describe_stacks(StackName=stack_name)
            stack = resp["Stacks"][0]
            last_op = stack.get("LastUpdatedTime") or stack.get("CreationTime")
            return last_op if isinstance(last_op, datetime) else None
        except Exception:
            return None

    def _wait_for_stack_settle(
        self,
        stack_name: str,
        timeout: float | None = None,
        stack_identifier: str | None = None,
    ) -> str | None:
        """Poll CloudFormation until a stack settles, retrying transient unknown reads."""
        import time

        if timeout is None:
            timeout = float(os.environ.get("GCO_CDK_SETTLE_TIMEOUT_SECONDS", "1200"))
        deadline = time.monotonic() + timeout
        unknown_started: float | None = None
        status = self._get_stack_status(stack_name, stack_identifier)
        while True:
            now = time.monotonic()
            if self._cdk_cancel_event.is_set():
                return status
            if status is None:
                if unknown_started is None:
                    unknown_started = now
                unknown_deadline = min(
                    deadline,
                    unknown_started + _CLOUDFORMATION_SETTLE_UNKNOWN_TIMEOUT_SECONDS,
                )
                if now >= unknown_deadline:
                    return None
                time.sleep(
                    min(
                        _CLOUDFORMATION_SETTLE_UNKNOWN_POLL_SECONDS,
                        unknown_deadline - now,
                    )
                )
                status = self._get_stack_status(stack_name, stack_identifier)
                continue
            unknown_started = None
            if not status.endswith("_IN_PROGRESS") or now >= deadline:
                return status
            time.sleep(min(15.0, deadline - now))
            status = self._get_stack_status(stack_name, stack_identifier)

    def _get_latest_stack_event(
        self,
        stack_name: str,
        stack_identifier: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest CloudFormation event for delete heartbeats."""
        import boto3

        try:
            region = self._get_destroy_region(stack_name)
            cfn = boto3.client("cloudformation", region_name=region)
            events = cfn.describe_stack_events(StackName=stack_identifier or stack_name).get(
                "StackEvents", []
            )
            return events[0] if events else None
        except Exception:
            logger.debug("Could not read delete events for %s", stack_name, exc_info=True)
            return None

    def _print_stack_delete_heartbeat(
        self,
        stack_name: str,
        status: str | None,
        stack_identifier: str | None = None,
    ) -> None:
        """Print the latest AWS-side state while a long delete converges."""
        event = self._get_latest_stack_event(stack_name, stack_identifier)
        if not event:
            print(f"  {stack_name}: CloudFormation status {status or 'unknown'}")
            return

        timestamp = event.get("Timestamp")
        timestamp_text = (
            timestamp.isoformat() if isinstance(timestamp, datetime) else str(timestamp or "")
        )
        logical_id = str(event.get("LogicalResourceId") or stack_name)
        resource_status = str(event.get("ResourceStatus") or status or "unknown")
        reason = " ".join(str(event.get("ResourceStatusReason") or "").split())
        if len(reason) > 400:
            reason = reason[:397] + "..."
        suffix = f" — {reason}" if reason else ""
        print(
            f"  {stack_name}: {status or 'unknown'}; latest event "
            f"{timestamp_text} {logical_id} {resource_status}{suffix}"
        )

    def _wait_for_stack_delete_convergence(
        self,
        stack_name: str,
        *,
        timeout: float | None = None,
        poll_interval: float = _CLOUDFORMATION_DELETE_POLL_SECONDS,
        heartbeat_interval: float = _CLOUDFORMATION_DELETE_HEARTBEAT_SECONDS,
        initial_status: str = "DELETE_IN_PROGRESS",
        expected_stack_id: str | None = None,
        require_expected_identity: bool = False,
    ) -> bool:
        """Wait for an AWS-side stack delete to finish without trusting CDK polling.

        The caller must already have evidence that a delete operation started.
        Transient status-read failures are tolerated after that proof, but a
        terminal ``DELETE_FAILED`` or the overall deadline fails closed.
        """
        if timeout is None:
            try:
                timeout = float(
                    os.environ.get(
                        "GCO_CLOUDFORMATION_DELETE_TIMEOUT_SECONDS",
                        str(_CLOUDFORMATION_DELETE_TIMEOUT_SECONDS),
                    )
                )
            except ValueError:
                timeout = _CLOUDFORMATION_DELETE_TIMEOUT_SECONDS
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("CloudFormation delete timeout must be positive and finite")
        if (
            not math.isfinite(poll_interval)
            or not math.isfinite(heartbeat_interval)
            or poll_interval <= 0
            or heartbeat_interval <= 0
        ):
            raise ValueError("CloudFormation delete polling intervals must be positive and finite")

        deadline = time.monotonic() + timeout
        next_heartbeat = time.monotonic()
        status: str | None = initial_status
        last_printed_status: str | None = None

        while True:
            if self._cdk_cancel_event.is_set():
                logger.warning("CloudFormation delete wait cancelled for %s", stack_name)
                return False
            try:
                if not self._stack_exists_in_cloudformation(
                    stack_name,
                    expected_stack_id=expected_stack_id,
                    require_expected_identity=require_expected_identity,
                ):
                    print(f"  {stack_name} is absent from CloudFormation.")
                    return True
            except RuntimeError:
                raise
            except Exception:
                logger.debug(
                    "CloudFormation presence check failed for %s",
                    stack_name,
                    exc_info=True,
                )

            now = time.monotonic()
            if status == "DELETE_COMPLETE":
                return True
            if status == "DELETE_FAILED":
                self._print_stack_delete_heartbeat(
                    stack_name,
                    status,
                    expected_stack_id,
                )
                return False
            if status not in (None, "DELETE_IN_PROGRESS"):
                self._print_stack_delete_heartbeat(
                    stack_name,
                    status,
                    expected_stack_id,
                )
                print(
                    f"  {stack_name} left DELETE_IN_PROGRESS without being deleted; "
                    "refusing to continue teardown."
                )
                return False
            if now >= deadline:
                self._print_stack_delete_heartbeat(
                    stack_name,
                    status,
                    expected_stack_id,
                )
                print(
                    f"  Timed out after {timeout:.0f}s waiting for {stack_name} "
                    "to disappear from CloudFormation."
                )
                return False
            if status != last_printed_status or now >= next_heartbeat:
                self._print_stack_delete_heartbeat(
                    stack_name,
                    status,
                    expected_stack_id,
                )
                last_printed_status = status
                next_heartbeat = now + heartbeat_interval

            time.sleep(min(poll_interval, max(0.0, deadline - now)))
            status = self._get_stack_status(stack_name, expected_stack_id)

    def _cloudformation_delete_stack(
        self,
        stack_name: str,
        *,
        expected_stack_id: str | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        require_expected_identity: bool = False,
    ) -> bool:
        """Delete an immediately revalidated stack by immutable ARN."""
        if self._cdk_cancel_event.is_set():
            raise RuntimeError(f"CloudFormation deletion cancelled before {stack_name}")
        target = self._describe_stack_target(
            stack_name,
            expected_stack_id=expected_stack_id,
            require_expected_identity=require_expected_identity,
        )
        if target is None:
            return True
        region, cfn, stack = target
        stack_id = str(stack["StackId"])
        status = str(stack.get("StackStatus") or "")
        if authorize_stack is not None:
            authorize_stack(stack_name, region, stack_id)
        if status == "DELETE_IN_PROGRESS":
            return self._wait_for_stack_delete_convergence(
                stack_name,
                initial_status=status,
                expected_stack_id=stack_id,
                require_expected_identity=require_expected_identity,
            )
        try:
            cfn.delete_stack(StackName=stack_id)
        except Exception:
            logger.debug("Direct CloudFormation delete failed for %s", stack_id, exc_info=True)
            return False
        return self._wait_for_stack_delete_convergence(
            stack_name,
            expected_stack_id=stack_id,
            require_expected_identity=require_expected_identity,
        )

    def _validated_regional_api_region(self, stack_name: str) -> str | None:
        """Return an exact project bridge's SDK-known CloudFormation Region."""
        bridge_prefix = f"{self.config.project_name}-regional-api-"
        if not stack_name.startswith(bridge_prefix):
            return None

        region = stack_name[len(bridge_prefix) :]
        if not region:
            return None
        try:
            return region if region in _known_cloudformation_regions() else None
        except Exception:
            logger.debug(
                "Could not validate regional API bridge Region for %s",
                stack_name,
                exc_info=True,
            )
            return None

    def _configured_regional_api_regions(
        self,
    ) -> tuple[frozenset[str], str] | None:
        """Read valid root regions and their partition for orphan deletion.

        Returning ``None`` means the configuration could not prove anything:
        missing, unreadable, malformed, wrong-project, incomplete, empty, and
        duplicate Region configurations all fail closed under the same contract
        used by :class:`ConfigLoader`.
        """
        path = self.project_root / "cdk.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            context = data.get("context")
            if not isinstance(context, dict):
                return None
            configured_project = context.get("project_name")
            if (
                not isinstance(configured_project, str)
                or not configured_project
                or configured_project != self.config.project_name
            ):
                return None
            deployment_regions = context.get("deployment_regions")
            if not isinstance(deployment_regions, dict):
                return None

            known_regions = _known_cloudformation_regions()
            for key in ("global", "api_gateway", "monitoring"):
                region = deployment_regions.get(key)
                if not isinstance(region, str) or region not in known_regions:
                    return None

            try:
                regional = validated_regional_deployment_regions(
                    deployment_regions.get("regional"),
                    known_regions=known_regions,
                )
                deployment_partition = validated_deployment_partition(
                    (
                        deployment_regions["global"],
                        deployment_regions["api_gateway"],
                        deployment_regions["monitoring"],
                        *regional,
                    )
                )
            except RuntimeError, ValueError:
                return None
            return frozenset(regional), deployment_partition
        except OSError, UnicodeError, json.JSONDecodeError, TypeError:
            logger.debug(
                "Could not read authoritative regional configuration from %s",
                path,
                exc_info=True,
            )
            return None

    def _get_orphan_regional_api_region(self, stack_name: str) -> str | None:
        """Return a bridge Region only when valid root config proves it absent.

        This result authorizes bypassing CDK and deleting a stack directly via
        CloudFormation. Merely failing a normal configuration lookup can never
        be interpreted as proof that the stack is orphaned.
        """
        region = self._validated_regional_api_region(stack_name)
        if region is None:
            return None
        configured = self._configured_regional_api_regions()
        if configured is None:
            return None
        configured_regions, deployment_partition = configured
        if region in configured_regions:
            return None
        try:
            candidate_partition = validated_deployment_partition((region,))
        except RuntimeError, ValueError:
            return None
        if candidate_partition != deployment_partition:
            return None
        return region

    def _get_destroy_region(self, stack_name: str) -> str:
        """Determine a configured or cryptographically bounded destroy Region.

        Deploy resolution intentionally requires bridge Regions to remain in
        ``cdk.json``. A removed bridge may still resolve through the orphan
        path, but that path validates the exact project-scoped name, SDK-known
        CloudFormation Region, authoritative root configuration, and matching
        AWS partition. Reconciliation must reuse that same proof rather than
        trusting a bridge-shaped suffix independently.
        """
        try:
            region = self._get_deploy_region(stack_name)
        except Exception:
            logger.debug(
                "Configured deploy Region lookup failed for %s; checking orphan shape",
                stack_name,
                exc_info=True,
            )
        else:
            if region:
                return region

        orphan_region = self._get_orphan_regional_api_region(stack_name)
        return orphan_region or self.config.api_gateway_region

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

    def _remove_api_gateway_analytics_dependency(
        self,
        *,
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
    ) -> bool:
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
        api_gateway_stack = f"{self.config.project_name}-api-gateway"
        analytics_stack = f"{self.config.project_name}-analytics"

        strict_identity = expected_stack_ids is not None
        if strict_identity:
            assert expected_stack_ids is not None
            if api_gateway_stack not in expected_stack_ids:
                raise RuntimeError(
                    f"Strict teardown lacks authoritative target state for {api_gateway_stack}"
                )
            api_gateway_expected_id = expected_stack_ids[api_gateway_stack]
        else:
            api_gateway_expected_id = None

        # Fast path: if the api-gateway stack doesn't exist (or has already
        # been deleted/rolled-back into a non-consuming state), there's
        # nothing importing the analytics exports. Skip the redeploy entirely.
        if not self._stack_exists_in_cloudformation(
            api_gateway_stack,
            expected_stack_id=api_gateway_expected_id,
            require_expected_identity=strict_identity,
        ):
            logger.info(
                "%s does not exist in CloudFormation; skipping redeploy before analytics destroy.",
                api_gateway_stack,
            )
            return True

        # Second fast path: if the deployed api-gateway isn't actually
        # importing anything from the analytics stack, we don't need to
        # touch it. This happens when analytics was never fully wired up.
        if not self._api_gateway_imports_from_analytics():
            logger.info(
                "%s does not import any %s exports; skipping redeploy before analytics destroy.",
                api_gateway_stack,
                analytics_stack,
            )
            return True

        if strict_identity and (
            not strict_deployment_token or on_change_set_prepared is None or authorize_stack is None
        ):
            raise RuntimeError(
                "Strict analytics teardown cannot remove API imports without "
                "prepared-change-set authority"
            )

        try:
            # Temporarily disable analytics so CDK drops the /studio/* routes.
            current = get_analytics_config()
            was_enabled = current.get("enabled", False)
            if was_enabled:
                update_analytics_config({"enabled": False})

            print(f"  Updating {api_gateway_stack} to remove analytics routes...")
            import tempfile

            with tempfile.TemporaryDirectory() as tmp_out:
                success = self.deploy(
                    stack_name=api_gateway_stack,
                    require_approval=False,
                    exclusively=True,
                    output_dir=tmp_out,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
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
                        "%s redeploy failed, but the stack no longer imports "
                        "analytics exports (likely deleted during cleanup). "
                        "Analytics destroy can proceed.",
                        api_gateway_stack,
                    )
                    return True
                logger.error(
                    "Failed to redeploy %s to drop analytics imports, and the "
                    "stack still consumes analytics exports. Destroying %s will "
                    "fail with 'Export ... cannot be deleted as it is in use'. "
                    "Fix %s first (see events above) and retry.",
                    api_gateway_stack,
                    analytics_stack,
                    api_gateway_stack,
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

        analytics_stack = f"{self.config.project_name}-analytics"
        api_gateway_stack = f"{self.config.project_name}-api-gateway"

        region = self._get_deploy_region(analytics_stack)
        if not region:
            return False

        try:
            cfn = boto3.client("cloudformation", region_name=region)
            # Collect every export whose owning stack is the analytics stack.
            analytics_exports: list[str] = []
            paginator = cfn.get_paginator("list_exports")
            for page in paginator.paginate():
                for export in page.get("Exports", []):
                    owner = export.get("ExportingStackId", "")
                    # ExportingStackId is a full ARN; match by stack name.
                    if f":stack/{analytics_stack}/" in owner:
                        analytics_exports.append(export["Name"])

            if not analytics_exports:
                return False

            # For each export, check whether the api-gateway stack is
            # listed as an importer. ``list_imports`` returns the stack
            # names that currently import the given export.
            import_paginator = cfn.get_paginator("list_imports")
            for export_name in analytics_exports:
                try:
                    for page in import_paginator.paginate(ExportName=export_name):
                        for importer in page.get("Imports", []):
                            if importer == api_gateway_stack:
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
                "Failed to check analytics imports for %s: %s",
                api_gateway_stack,
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

    def _validate_bootstrap_stack(
        self,
        region: str,
        expected: Mapping[str, str],
    ) -> None:
        """Require the exact preflighted CDKToolkit ARN and healthy status."""
        import boto3

        expected_id = str(expected.get("stack_id") or "")
        expected_status = str(expected.get("status") or "")
        if not expected_id or expected_status not in _BOOTSTRAP_HEALTHY_STATUSES:
            raise RuntimeError(f"Invalid checkpointed CDKToolkit identity for {region}")
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            response = cfn.describe_stacks(StackName=expected_id)
        except Exception as exc:
            raise RuntimeError(
                f"Could not revalidate checkpointed CDKToolkit {expected_id} in {region}"
            ) from exc
        stacks = response.get("Stacks", [])
        if len(stacks) != 1:
            raise RuntimeError(f"CDKToolkit {expected_id} returned an invalid identity")
        stack = stacks[0]
        actual_id = str(stack.get("StackId") or "")
        actual_status = str(stack.get("StackStatus") or "")
        if stack.get("StackName") != "CDKToolkit" or actual_id != expected_id:
            raise RuntimeError(f"CDKToolkit identity changed in {region}")
        if actual_status != expected_status or actual_status not in _BOOTSTRAP_HEALTHY_STATUSES:
            raise RuntimeError(
                f"CDKToolkit {expected_id} status changed from {expected_status} "
                f"to {actual_status or 'unknown'}"
            )

    @staticmethod
    def _strict_change_set_name(stack_name: str, token: str) -> str:
        """Return one deterministic, run-scoped CloudFormation change-set name."""
        safe_token = "".join(
            character if character.isascii() and character.isalnum() else "-" for character in token
        )
        safe_token = "-".join(part for part in safe_token.split("-") if part)
        digest = hashlib.sha256(f"{token}:{stack_name}".encode()).hexdigest()[:16]
        namespace = "gco"
        max_token_length = 128 - len(namespace) - len(digest) - 2
        safe_token = (safe_token or "live-validation")[:max_token_length]
        return f"{namespace}-{safe_token}-{digest}"

    def _preflight_strict_change_set(
        self,
        *,
        stack_name: str,
        change_set_name: str,
        expected_stack_id: str | None,
        prepared_change_sets: Mapping[str, Mapping[str, str]],
    ) -> None:
        """Reject an existing deterministic change set without checkpoint authority."""
        import boto3

        region = self._get_deploy_region(stack_name)
        if not region:
            raise RuntimeError(f"Could not resolve deploy Region for {stack_name}")
        cfn = boto3.client("cloudformation", region_name=region)
        try:
            change_set = cfn.describe_change_set(
                ChangeSetName=change_set_name,
                StackName=stack_name,
            )
        except ClientError as exc:
            if self._change_set_missing(exc):
                return
            # DescribeChangeSet reports a stack-style ValidationError when both
            # the deterministic change set and its fresh target stack are absent.
            # The target was authoritatively checked immediately above; only an
            # empty create history can safely interpret this as "not prepared".
            if expected_stack_id is None and not prepared_change_sets and self._stack_missing(exc):
                return
            raise RuntimeError(
                f"Could not preflight strict change set {change_set_name} for {stack_name}"
            ) from exc

        change_set_id = str(change_set.get("ChangeSetId") or "")
        observed_change_set_name = str(change_set.get("ChangeSetName") or "")
        stack_id = str(change_set.get("StackId") or "")
        if not change_set_id or not stack_id or observed_change_set_name != change_set_name:
            raise RuntimeError(
                f"Existing strict change set {change_set_name} omitted immutable identities"
            )
        self._validate_strict_change_set_arns(
            stack_name=stack_name,
            change_set_name=change_set_name,
            stack_id=stack_id,
            change_set_id=change_set_id,
            region=region,
        )
        prepared_record = prepared_change_sets.get(change_set_id)
        if prepared_record is None:
            raise RuntimeError(
                f"Existing strict change set {change_set_id} lacks checkpoint authority"
            )
        recorded_change_set_id = str(prepared_record.get("change_set_id") or "")
        recorded_stack_id = str(prepared_record.get("stack_id") or "")
        recorded_type = str(prepared_record.get("change_set_type") or "")
        if (
            expected_stack_id is None
            or stack_id != expected_stack_id
            or recorded_change_set_id != change_set_id
            or recorded_stack_id != stack_id
            or recorded_type not in {"CREATE", "UPDATE"}
        ):
            raise RuntimeError(f"Existing strict change-set authority changed for {stack_name}")

    @staticmethod
    def _validate_strict_change_set_arns(
        *,
        stack_name: str,
        change_set_name: str,
        stack_id: str,
        change_set_id: str,
        region: str,
    ) -> None:
        """Require both prepared identities to be exact, related CloudFormation ARNs."""

        def split_arn(identifier: str, label: str) -> tuple[str, str, str, str]:
            parts = identifier.split(":", 5)
            if (
                len(parts) != 6
                or parts[0] != "arn"
                or not (parts[1] == "aws" or parts[1].startswith("aws-"))
                or parts[2] != "cloudformation"
                or parts[3] != region
                or not parts[4]
                or not parts[5]
            ):
                raise RuntimeError(f"Strict {label} has an invalid CloudFormation ARN")
            return parts[1], parts[3], parts[4], parts[5]

        stack_partition, _stack_region, stack_account, stack_resource = split_arn(
            stack_id,
            "stack identity",
        )
        stack_prefix = f"stack/{stack_name}/"
        if not stack_resource.startswith(stack_prefix) or not stack_resource.removeprefix(
            stack_prefix
        ):
            raise RuntimeError(
                f"Strict stack identity {stack_id} does not name expected stack {stack_name}"
            )

        change_partition, _change_region, change_account, change_resource = split_arn(
            change_set_id,
            "change-set identity",
        )
        change_prefix = f"changeSet/{change_set_name}/"
        if not change_resource.startswith(change_prefix) or not change_resource.removeprefix(
            change_prefix
        ):
            raise RuntimeError(
                f"Strict change-set identity {change_set_id} does not name {change_set_name}"
            )
        if change_partition != stack_partition or change_account != stack_account:
            raise RuntimeError(
                "Strict stack and change-set identities belong to different AWS authorities"
            )

    def _execute_prepared_change_set(
        self,
        *,
        stack_name: str,
        change_set_name: str,
        expected_stack_id: str | None,
        expected_tags: Mapping[str, str] | None,
        prepared_change_sets: Mapping[str, Mapping[str, str]],
        preparation_succeeded: bool,
        authorize_stack: StackAuthorizationCallback | None,
        on_change_set_prepared: ChangeSetPreparedCallback,
        allow_noop: bool,
        timeout: float,
    ) -> bool:
        """Validate, checkpoint, and execute only the deterministic CDK change set."""
        import boto3

        region = self._get_deploy_region(stack_name)
        if not region:
            raise RuntimeError(f"Could not resolve deploy Region for {stack_name}")
        cfn = boto3.client("cloudformation", region_name=region)
        change_set: dict[str, Any] = {}
        inspection_attempts = (
            _STRICT_CHANGE_SET_INSPECTION_ATTEMPTS
            if expected_stack_id is None and not prepared_change_sets
            else 1
        )
        for inspection_attempt in range(inspection_attempts):
            try:
                change_set = cfn.describe_change_set(
                    ChangeSetName=change_set_name,
                    StackName=stack_name,
                )
                break
            except ClientError as exc:
                fresh_create_not_visible = bool(
                    expected_stack_id is None
                    and not prepared_change_sets
                    and (self._change_set_missing(exc) or self._stack_missing(exc))
                )
                if fresh_create_not_visible and inspection_attempt + 1 < inspection_attempts:
                    if self._cdk_cancel_event.is_set():
                        raise RuntimeError(
                            "Strict change-set inspection cancelled before ownership checkpoint"
                        ) from exc
                    time.sleep(_STRICT_CHANGE_SET_INSPECTION_RETRY_SECONDS)
                    continue
                if not self._change_set_missing(exc) and not fresh_create_not_visible:
                    raise RuntimeError(
                        f"Could not inspect strict change set {change_set_name} for {stack_name}"
                    ) from exc
                if allow_noop and expected_stack_id:
                    target = self._describe_stack_target(
                        stack_name,
                        expected_stack_id=expected_stack_id,
                        require_expected_identity=True,
                    )
                    if target is not None:
                        stack = target[2]
                        status = str(stack.get("StackStatus") or "")
                        if status in _BOOTSTRAP_HEALTHY_STATUSES:
                            if authorize_stack is None:
                                raise RuntimeError(
                                    f"Strict no-op for {stack_name} lacks exact authorization"
                                ) from exc
                            authorize_stack(stack_name, region, expected_stack_id)
                            return True
                raise RuntimeError(
                    f"CDK did not create the strict change set {change_set_name} for {stack_name}"
                ) from exc

        change_set_id = str(change_set.get("ChangeSetId") or "")
        observed_change_set_name = str(change_set.get("ChangeSetName") or "")
        stack_id = str(change_set.get("StackId") or "")
        status = str(change_set.get("Status") or "")
        execution_status = str(change_set.get("ExecutionStatus") or "")
        if not change_set_id or not stack_id:
            raise RuntimeError(f"Strict change set {change_set_name} omitted immutable identities")
        if observed_change_set_name != change_set_name:
            raise RuntimeError(
                f"Strict change set identity changed from {change_set_name} "
                f"to {observed_change_set_name or 'unknown'}"
            )
        self._validate_strict_change_set_arns(
            stack_name=stack_name,
            change_set_name=change_set_name,
            stack_id=stack_id,
            change_set_id=change_set_id,
            region=region,
        )
        prepared_record = prepared_change_sets.get(change_set_id)
        if prepared_record is None:
            # DescribeChangeSet does not expose ChangeSetType. For a newly
            # prepared change set, the pre-CDK exact target state is the only
            # authoritative source: absence means CREATE; an exact stack means
            # UPDATE. Resumes use the persisted per-change-set record below.
            change_set_type = "CREATE" if expected_stack_id is None else "UPDATE"
        else:
            recorded_change_set_id = str(prepared_record.get("change_set_id") or "")
            recorded_stack_id = str(prepared_record.get("stack_id") or "")
            change_set_type = str(prepared_record.get("change_set_type") or "")
            if recorded_change_set_id != change_set_id or recorded_stack_id != stack_id:
                raise RuntimeError(
                    f"Persisted strict change-set authority changed for {stack_name}"
                )
            if change_set_type not in {"CREATE", "UPDATE"}:
                raise RuntimeError(
                    f"Persisted strict change set for {stack_name} has invalid type "
                    f"{change_set_type or 'unknown'}"
                )
        if change_set_type == "UPDATE" and expected_stack_id is None:
            raise RuntimeError(
                f"Strict change set for absent {stack_name} unexpectedly performs UPDATE"
            )
        if expected_stack_id is not None and stack_id != expected_stack_id:
            raise RuntimeError(
                f"Strict change set targets replacement {stack_id}; expected {expected_stack_id}"
            )
        observed_tags = {
            str(tag.get("Key")): str(tag.get("Value"))
            for tag in change_set.get("Tags", [])
            if tag.get("Key") is not None
        }
        for key, value in (expected_tags or {}).items():
            if observed_tags.get(str(key)) != str(value):
                raise RuntimeError(f"Strict change set {change_set_id} omitted required tag {key}")

        status_reason = " ".join(str(change_set.get("StatusReason") or "").split()).lower()
        empty_change_set = (
            "submitted information didn't contain changes" in status_reason
            or "no updates are to be performed" in status_reason
        )
        if (
            status == "FAILED"
            and empty_change_set
            and (allow_noop or prepared_record is not None)
            and expected_stack_id
        ):
            # stack_id == expected_stack_id is already guaranteed here: the
            # identity check above raises for any mismatch whenever
            # expected_stack_id is not None, and this branch requires a
            # truthy expected_stack_id.
            target = self._describe_stack_target(
                stack_name,
                expected_stack_id=expected_stack_id,
                require_expected_identity=True,
            )
            if target is None or str(target[2].get("StackStatus") or "") not in (
                _BOOTSTRAP_HEALTHY_STATUSES
            ):
                raise RuntimeError(
                    f"Empty strict change set {change_set_id} has no healthy exact stack"
                )
            if authorize_stack is None:
                raise RuntimeError(f"Strict no-op for {stack_name} lacks exact authorization")
            authorize_stack(stack_name, region, expected_stack_id)
            on_change_set_prepared(
                stack_name,
                region,
                stack_id,
                change_set_id,
                change_set_type,
            )
            return True
        if status != "CREATE_COMPLETE" or execution_status not in {
            "AVAILABLE",
            "EXECUTE_COMPLETE",
        }:
            raise RuntimeError(
                f"Strict change set {change_set_id} is {status}/{execution_status}, not usable"
            )

        if execution_status == "AVAILABLE" and (
            prepared_record is None and not preparation_succeeded
        ):
            raise RuntimeError(
                f"Strict change set {change_set_id} was not produced by this preparation"
            )
        if execution_status == "EXECUTE_COMPLETE" and (
            prepared_record is None or expected_stack_id is None
        ):
            raise RuntimeError(
                f"Executed strict change set {change_set_id} lacks prior checkpoint authority"
            )

        if expected_stack_id is not None:
            if authorize_stack is None:
                raise RuntimeError(f"Strict change set for {stack_name} lacks exact authorization")
            authorize_stack(stack_name, region, stack_id)

        if execution_status == "EXECUTE_COMPLETE":
            target = self._describe_stack_target(
                stack_name,
                expected_stack_id=stack_id,
                require_expected_identity=True,
            )
            if target is None or str(target[2].get("StackStatus") or "") not in (
                _BOOTSTRAP_HEALTHY_STATUSES
            ):
                raise RuntimeError(
                    f"Executed strict change set {change_set_id} has no healthy exact stack"
                )
        elif change_set_type == "CREATE":
            target = self._describe_stack_target(
                stack_name,
                expected_stack_id=stack_id,
                require_expected_identity=True,
            )
            if target is None or str(target[2].get("StackStatus") or "") != "REVIEW_IN_PROGRESS":
                raise RuntimeError(
                    f"Prepared CREATE change set {change_set_id} has no exact review stack"
                )

        on_change_set_prepared(
            stack_name,
            region,
            stack_id,
            change_set_id,
            change_set_type,
        )
        if execution_status == "EXECUTE_COMPLETE":
            return True
        if self._cdk_cancel_event.is_set():
            raise RuntimeError(
                f"Strict change set {change_set_id} was checkpointed but execution was cancelled"
            )

        cfn.execute_change_set(ChangeSetName=change_set_id)
        settled = self._wait_for_stack_settle(
            stack_name,
            timeout=timeout,
            stack_identifier=stack_id,
        )
        if settled not in _BOOTSTRAP_HEALTHY_STATUSES:
            logger.error(
                "Strict change set %s for %s settled as %s",
                change_set_id,
                stack_name,
                settled or "unknown",
            )
            return False
        return True

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

        # Named stacks are classified by suffix and regional stacks by the
        # ``<project>-`` prefix (#139) so a non-``gco`` deployment resolves
        # regions for its own ``<project>-*`` stacks — otherwise the image
        # mirror (which calls this to pick a regional stack's region) would
        # silently no-op. For the default ``gco`` behaviour is unchanged.
        region: str | None
        if stack_name.endswith("-global"):
            region = cdk_regions.get("global") or self.config.global_region
            return region
        if stack_name.endswith("-api-gateway"):
            region = cdk_regions.get("api_gateway") or self.config.api_gateway_region
            return region
        if stack_name.endswith("-monitoring"):
            region = cdk_regions.get("monitoring") or self.config.monitoring_region
            return region
        if stack_name.endswith("-analytics"):
            # The analytics stack shares the API gateway region so the
            # presigned-URL Lambda can hook into the existing /studio/*
            # routes on the same API Gateway.
            region = cdk_regions.get("api_gateway") or self.config.api_gateway_region
            return region

        # Regional API bridges use ``<project>-regional-api-<region>``. Resolve
        # this exact shape before generic regional stacks; otherwise the generic
        # project-prefix branch returns the malformed ``regional-api-<region>``.
        # Requiring a configured deployment region also prevents bridge-shaped
        # typos from being treated as valid AWS regions.
        bridge_prefix = f"{self.config.project_name}-regional-api-"
        if stack_name.startswith(bridge_prefix):
            region = stack_name[len(bridge_prefix) :]
            configured_regions = {str(item) for item in (cdk_regions.get("regional") or [])}
            return region if region in configured_regions else None

        # Base regional stacks: {project}-{region}. The region is whatever
        # follows the project prefix (regions contain hyphens, so we strip
        # the known prefix rather than guess a split point).
        prefix = f"{self.config.project_name}-"
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

        # Bridge stacks contain no regional Helm consumers and must not trigger
        # image mirroring. Match the exact project-scoped prefix so a project
        # name containing ``regional-api`` remains unambiguous.
        bridge_prefix = f"{self.config.project_name}-regional-api-"
        if stack_name and stack_name.startswith(bridge_prefix):
            return []

        if stack_name and stack_name.startswith(prefix) and stack_name not in named:
            region = self._get_deploy_region(stack_name)
            return [region] if region else []
        return []

    def _mirror_images_if_enabled(
        self,
        stack_name: str | None,
        all_stacks: bool,
        repository_tags: Mapping[str, str] | None = None,
        on_repository_created: EcrRepositoryCreatedCallback | None = None,
    ) -> None:
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
                    region,
                    ecr_namespace=cfg["ecr_namespace"],
                    skip_existing=True,
                    repository_tags=repository_tags,
                    on_repository_created=on_repository_created,
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
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
    ) -> tuple[bool, list[str], list[str]]:
        """
        Deploy all stacks in the correct order.

        Deploys global stacks first, then base regional stacks, regional API
        bridges, and finally monitoring. Parallelism never crosses a dependency
        level.

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
        stack_names = set(stacks)
        project_name = self.config.project_name
        ordered_stacks = get_stack_deployment_order(stacks, project_name=project_name)

        strict_deployment = (
            strict_deployment_token is not None or on_change_set_prepared is not None
        )
        if strict_deployment:
            if not strict_deployment_token or on_change_set_prepared is None:
                raise RuntimeError(
                    "Strict deployment requires both a run token and a prepared-change-set callback"
                )
            if allow_bootstrap:
                raise RuntimeError("Strict orchestrated deployment cannot auto-bootstrap")
            if authorize_stack is None:
                raise RuntimeError("Strict orchestrated deployment requires an exact authorizer")
            if expected_stack_ids is None:
                raise RuntimeError("Strict orchestrated deployment lacks target identities")
            if prepared_change_sets is None:
                raise RuntimeError("Strict orchestrated deployment lacks change-set history")
            missing = sorted(set(stacks) - set(expected_stack_ids))
            unexpected = sorted(set(expected_stack_ids) - set(stacks))
            if missing or unexpected:
                raise RuntimeError(
                    "Strict deployment target map does not match the CDK graph; "
                    f"missing={missing}, unexpected={unexpected}"
                )
            missing_history = sorted(set(stacks) - set(prepared_change_sets))
            unexpected_history = sorted(set(prepared_change_sets) - set(stacks))
            if missing_history or unexpected_history:
                raise RuntimeError(
                    "Strict change-set history does not match the CDK graph; "
                    f"missing={missing_history}, unexpected={unexpected_history}"
                )

            # Validate every toolkit and every expected stack before the first
            # repository copy, stuck-stack recovery, or CloudFormation mutation.
            validated_regions: set[str] = set()
            for target_name in ordered_stacks:
                region = self._get_deploy_region(target_name)
                if not region:
                    raise RuntimeError(f"Could not resolve deploy Region for {target_name}")
                if region not in validated_regions:
                    expected_bootstrap = (bootstrap_stacks or {}).get(region)
                    if expected_bootstrap is None:
                        raise RuntimeError(
                            f"Strict deployment lacks a checkpointed CDKToolkit identity for {region}"
                        )
                    self._validate_bootstrap_stack(region, expected_bootstrap)
                    validated_regions.add(region)

                expected_id = expected_stack_ids[target_name]
                target = self._describe_stack_target(
                    target_name,
                    expected_stack_id=expected_id,
                    require_expected_identity=True,
                )
                if expected_id is not None and target is None:
                    raise RuntimeError(
                        f"Checkpointed stack {expected_id} is absent; refusing recreation"
                    )
                if target is not None:
                    authorize_stack(target_name, region, str(target[2]["StackId"]))

        # Separate stacks into four dependency levels by suffix/marker so
        # ordering is independent of project_name (#139):
        # 1. Pre-regional global stacks (<project>-global, <project>-api-gateway)
        # 2. Base regional stacks (<project>-<region>, parallel-safe)
        # 3. Regional API bridges (<project>-regional-api-<region>, depend on base)
        # 4. Monitoring (depends on regional stacks)
        pre_regional_stacks = [s for s in ordered_stacks if s.endswith(("-global", "-api-gateway"))]
        regional_api_stacks = [
            s
            for s in ordered_stacks
            if _is_regional_api_bridge_stack(
                s,
                project_name=project_name,
                stack_names=stack_names,
            )
        ]
        regional_stacks = [
            s
            for s in ordered_stacks
            if not s.endswith(("-global", "-api-gateway", "-monitoring"))
            and not _is_regional_api_bridge_stack(
                s,
                project_name=project_name,
                stack_names=stack_names,
            )
        ]
        post_regional_stacks = [s for s in ordered_stacks if s.endswith("-monitoring")]

        successful: list[str] = []
        failed: list[str] = []
        deployment_safety: _StackOperationSafetyKwargs = {
            "allow_bootstrap": allow_bootstrap,
            "bootstrap_stacks": bootstrap_stacks,
            "expected_stack_ids": expected_stack_ids,
            "prepared_change_sets": prepared_change_sets,
            "authorize_stack": authorize_stack,
            "strict_deployment_token": strict_deployment_token,
            "on_change_set_prepared": on_change_set_prepared,
            "on_ecr_repository_created": on_ecr_repository_created,
        }

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
                **deployment_safety,
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
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
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
                        **deployment_safety,
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

        # Phase 3: Deploy regional API bridges only after every base regional
        # stack is complete. Bridges within this level remain parallel-safe.
        if regional_api_stacks:
            if parallel and len(regional_api_stacks) > 1:
                successful_api, failed_api = self._deploy_stacks_parallel(
                    stacks=regional_api_stacks,
                    require_approval=require_approval,
                    outputs_file=outputs_file,
                    parameters=parameters,
                    tags=tags,
                    progress=progress,
                    on_stack_start=on_stack_start,
                    on_stack_complete=on_stack_complete,
                    max_workers=max_workers,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
                successful.extend(successful_api)
                failed.extend(failed_api)
                if failed_api:
                    return False, successful, failed
            else:
                for stack_name in regional_api_stacks:
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
                        **deployment_safety,
                    )
                    if success:
                        successful.append(stack_name)
                    else:
                        failed.append(stack_name)
                    if on_stack_complete:
                        on_stack_complete(stack_name, success)
                    if not success:
                        return False, successful, failed

        # Phase 4: Deploy post-regional stacks (monitoring) sequentially.
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
                **deployment_safety,
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
        allow_bootstrap: bool,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None,
        expected_stack_ids: Mapping[str, str | None] | None,
        prepared_change_sets: PreparedChangeSetAuthority | None,
        authorize_stack: StackAuthorizationCallback | None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
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
            try:
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
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    expected_stack_ids=expected_stack_ids,
                    prepared_change_sets=prepared_change_sets,
                    authorize_stack=authorize_stack,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
                return stack_name, success
            finally:
                try:
                    import shutil

                    if os.path.exists(output_dir):
                        shutil.rmtree(output_dir)
                except Exception as e:
                    logger.debug("Cleanup of %s failed: %s", output_dir, e)

        self._cdk_cancel_event.clear()
        futures: dict[Any, str] = {}
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
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
        except BaseException:
            # Terminate registered process groups before waiting for executor
            # shutdown; the context-manager form waits first and can deadlock an
            # interrupted orchestration behind a still-running CDK worker.
            self.cancel_active_cdk_processes()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        finally:
            self._cdk_cancel_event.clear()

        return successful, failed

    def _resolve_strict_teardown_resources(
        self,
        *,
        stacks: Collection[str],
        regional_stacks: Collection[str],
        expected_stack_ids: Mapping[str, str | None],
        authorize_stack: StackAuthorizationCallback,
    ) -> dict[str, dict[str, str]]:
        """Authorize every live stack, then resolve helper IDs from exact stack ARNs."""
        import boto3

        live_targets: dict[str, tuple[str, Any, dict[str, Any]]] = {}
        for stack_name in stacks:
            expected_stack_id = expected_stack_ids[stack_name]
            target = self._describe_stack_target(
                stack_name,
                expected_stack_id=expected_stack_id,
                require_expected_identity=True,
            )
            if target is None:
                continue
            region, _cloudformation, stack = target
            stack_id = str(stack["StackId"])
            authorize_stack(stack_name, region, stack_id)
            live_targets[stack_name] = target

        project_name = self.config.project_name
        base_regional_stacks: list[str] = []
        for stack_name in regional_stacks:
            deploy_region = self._get_deploy_region(stack_name)
            if deploy_region and stack_name == f"{project_name}-{deploy_region}":
                base_regional_stacks.append(stack_name)

        resolved: dict[str, dict[str, str]] = {}
        for stack_name in base_regional_stacks:
            target = live_targets.get(stack_name)
            if target is None:
                continue
            region, cloudformation, stack = target
            stack_id = str(stack["StackId"])
            summaries: list[dict[str, Any]] = []
            paginator = cloudformation.get_paginator("list_stack_resources")
            for page in paginator.paginate(StackName=stack_id):
                summaries.extend(page.get("StackResourceSummaries", []))

            vpc_ids = {
                str(item["PhysicalResourceId"])
                for item in summaries
                if item.get("ResourceType") == "AWS::EC2::VPC" and item.get("PhysicalResourceId")
            }
            cluster_names = {
                str(item["PhysicalResourceId"])
                for item in summaries
                if item.get("ResourceType") == "AWS::EKS::Cluster"
                and item.get("PhysicalResourceId")
            }
            if len(vpc_ids) > 1 or len(cluster_names) > 1:
                raise RuntimeError(f"Exact stack {stack_id} returned ambiguous VPC/EKS resources")

            details = {
                "stack_name": stack_name,
                "stack_id": stack_id,
                "region": region,
            }
            vpc_id = next(iter(vpc_ids), "")
            cluster_name = next(iter(cluster_names), "")
            if vpc_id:
                details["vpc_id"] = vpc_id
            if cluster_name:
                details["cluster_name"] = cluster_name
                cluster: dict[str, Any] | None
                try:
                    cluster = boto3.client("eks", region_name=region).describe_cluster(
                        name=cluster_name
                    )["cluster"]
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        cluster = None
                    else:
                        raise
                if cluster is not None:
                    if str(cluster.get("name") or "") != cluster_name:
                        raise RuntimeError(
                            f"EKS returned a changed identity for {region}:{cluster_name}"
                        )
                    networking = cluster.get("resourcesVpcConfig") or {}
                    cluster_vpc_id = str(networking.get("vpcId") or "")
                    security_group_id = str(networking.get("clusterSecurityGroupId") or "")
                    if vpc_id and cluster_vpc_id != vpc_id:
                        raise RuntimeError(
                            f"EKS cluster {cluster_name} no longer belongs to exact VPC {vpc_id}"
                        )
                    if not security_group_id:
                        raise RuntimeError(
                            f"EKS cluster {cluster_name} omitted its security-group identity"
                        )
                    details["cluster_security_group_id"] = security_group_id
                elif vpc_id:
                    # On teardown resume the cluster may already be gone while
                    # its managed SG remains. Resolve the SG ID inside the exact
                    # stack VPC using the exact cluster physical ID.
                    ec2 = boto3.client("ec2", region_name=region)
                    groups = ec2.describe_security_groups(
                        Filters=[
                            {"Name": "vpc-id", "Values": [vpc_id]},
                            {
                                "Name": "tag:aws:eks:cluster-name",
                                "Values": [cluster_name],
                            },
                        ]
                    ).get("SecurityGroups", [])
                    group_ids = {str(group["GroupId"]) for group in groups if group.get("GroupId")}
                    if len(group_ids) > 1:
                        raise RuntimeError(
                            f"Exact VPC {vpc_id} has ambiguous EKS security groups for "
                            f"{cluster_name}"
                        )
                    if group_ids:
                        details["cluster_security_group_id"] = next(iter(group_ids))
            resolved[stack_name] = details
        return resolved

    def _destroy_phase_remaining_stacks(
        self,
        phase_name: str,
        stacks: Collection[str],
        expected_stack_ids: Mapping[str, str | None] | None = None,
    ) -> list[str]:
        """Return stacks still present after a dependency phase.

        A lookup error is treated as present: advancing when absence cannot be
        proven is less safe than stopping for an operator retry.
        """
        remaining: list[str] = []
        for stack_name in stacks:
            try:
                present = self._stack_exists_in_cloudformation(
                    stack_name,
                    expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                    require_expected_identity=expected_stack_ids is not None,
                )
            except Exception:
                logger.exception(
                    "Could not verify %s absence after %s",
                    stack_name,
                    phase_name,
                )
                present = True
            if present:
                remaining.append(stack_name)
        if remaining:
            print(
                f"  {phase_name} barrier blocked: stack absence was not confirmed for "
                + ", ".join(remaining)
            )
        return remaining

    def destroy_orchestrated(
        self,
        force: bool = False,
        on_stack_start: Callable[[str], None] | None = None,
        on_stack_complete: Callable[[str, bool], None] | None = None,
        parallel: bool = False,
        max_workers: int = 4,
        expected_stack_ids: Mapping[str, str | None] | None = None,
        prepared_change_sets: PreparedChangeSetAuthority | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        allow_bootstrap: bool = True,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None = None,
        on_cleanup_complete: CleanupOutcomeCallback | None = None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
        retain_volumes: bool = False,
    ) -> tuple[bool, list[str], list[str]]:
        """Destroy stacks in dependency order with optional exact-ARN authority.

        ``retain_volumes`` reports the destroyed clusters' orphaned CSI volumes
        instead of deleting them; see ``_cleanup_cluster_volumes``.
        """
        app_stacks = self.list_stacks()
        strict_identity = expected_stack_ids is not None
        if strict_identity:
            assert expected_stack_ids is not None
            missing = sorted(set(app_stacks) - set(expected_stack_ids))
            if missing:
                raise RuntimeError(
                    f"Strict teardown target map is incomplete before cleanup; missing={missing}"
                )
            if authorize_stack is None:
                raise RuntimeError("Strict teardown requires an exact stack authorizer")
            invalid = sorted(
                name
                for name, stack_id in expected_stack_ids.items()
                if stack_id is not None and not str(stack_id).startswith("arn:")
            )
            if invalid:
                raise RuntimeError(
                    f"Strict teardown has invalid stack identities for: {', '.join(invalid)}"
                )

        strict_prepared_deployment = (
            strict_deployment_token is not None or on_change_set_prepared is not None
        )
        if strict_prepared_deployment:
            if not strict_deployment_token or on_change_set_prepared is None:
                raise RuntimeError(
                    "Strict teardown dependency deployment requires both a run token "
                    "and a prepared-change-set callback"
                )
            if prepared_change_sets is None:
                raise RuntimeError("Strict teardown lacks prepared change-set history")
            expected_history_keys = set(expected_stack_ids or {})
            if set(prepared_change_sets) != expected_history_keys:
                raise RuntimeError(
                    "Strict teardown change-set history does not match target identities"
                )

        stacks = list(app_stacks)
        if expected_stack_ids is not None:
            for stack_name in expected_stack_ids:
                if stack_name not in stacks:
                    stacks.append(stack_name)
        project_name = self.config.project_name
        (
            post_regional_stacks,
            regional_api_stacks,
            regional_stacks,
            pre_regional_stacks,
        ) = _get_stack_destroy_phases(stacks, project_name=project_name)

        strict_resources: dict[str, dict[str, str]] = {}
        if strict_identity:
            assert expected_stack_ids is not None
            assert authorize_stack is not None
            strict_resources = self._resolve_strict_teardown_resources(
                stacks=stacks,
                regional_stacks=regional_stacks,
                expected_stack_ids=expected_stack_ids,
                authorize_stack=authorize_stack,
            )

        destroy_safety: _StackOperationSafetyKwargs = {
            "expected_stack_ids": expected_stack_ids,
            "prepared_change_sets": prepared_change_sets,
            "authorize_stack": authorize_stack,
            "allow_bootstrap": allow_bootstrap,
            "bootstrap_stacks": bootstrap_stacks,
            "strict_deployment_token": strict_deployment_token,
            "on_change_set_prepared": on_change_set_prepared,
            "on_ecr_repository_created": on_ecr_repository_created,
        }

        def record_cleanup(name: str, details: dict[str, Any]) -> None:
            if on_cleanup_complete is not None:
                on_cleanup_complete(name, details)

        if not self._image_registry_destroy_preflight(force=force):
            return False, [], list(stacks)

        bastion_targets = {
            name: details for name, details in strict_resources.items() if details.get("vpc_id")
        }
        bastions = self.cleanup_orphaned_bastions(
            stacks,
            parallel=parallel,
            resource_targets=bastion_targets if strict_identity else None,
        )
        record_cleanup("bastions", {"terminated_instances": bastions})

        # Non-strict teardowns also retire the bastion's standing IAM
        # role/profile and, below, the implicit log groups CloudFormation
        # never modeled. Strict (live-validation) teardowns skip both: the
        # harness owns fenced log-group deletion and audits IAM itself.
        if not strict_identity:
            record_cleanup("bastion-iam", self._cleanup_bastion_iam())

        global_stack_name = f"{project_name}-global"
        backup = self._cleanup_backup_vault(
            expected_stack_id=(expected_stack_ids or {}).get(global_stack_name),
            authorize_stack=authorize_stack,
            require_expected_identity=strict_identity,
        )
        record_cleanup("backup-vault", backup)
        if strict_identity and backup.get("errors"):
            raise RuntimeError(
                "Strict backup-vault cleanup failed before stack deletion: "
                + json.dumps(backup["errors"], sort_keys=True)
            )

        successful: list[str] = []
        failed: list[str] = []

        # Capture implicit log-group names while the source stacks still
        # exist; the exact derived names are deleted by ``finish`` below
        # once their stacks are gone. Strict teardowns collect nothing —
        # the live-validation harness owns fenced log-group deletion.
        implicit_log_groups: dict[str, dict[str, Any]] = {}
        if not strict_identity:
            implicit_log_groups = self._collect_implicit_log_groups(stacks)

        def finish(overall: bool) -> tuple[bool, list[str], list[str]]:
            """Funnel every exit through the teardown sweeps.

            Called at each return point so a partially failed teardown
            still cleans up the stacks that DID delete (implicit log
            groups), while the success-only sweeps (runtime traffic-dial
            parameters) run exactly when everything is gone. New exit
            paths must return through here as well.
            """
            if implicit_log_groups:
                record_cleanup(
                    "implicit-log-groups",
                    self._cleanup_implicit_log_groups(implicit_log_groups, successful),
                )
            if overall:
                # Only after a complete teardown: while any stack survives,
                # the accelerator may still be live and a manual override on
                # it is standing operator intent the purge must not erase.
                # Strict teardowns run this too — the runtime dial parameters
                # are untagged, so the harness's tagging-index audit cannot
                # see them and no one else owns their removal.
                record_cleanup(
                    "traffic-dial-parameters",
                    self._cleanup_traffic_dial_parameters(),
                )
            return overall, successful, failed

        for stack_name in post_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)
            success = self.destroy(
                stack_name=stack_name,
                force=force,
                expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                **destroy_safety,
            )
            (successful if success else failed).append(stack_name)
            if on_stack_complete:
                on_stack_complete(stack_name, success)

        phase_remaining = self._destroy_phase_remaining_stacks(
            "post-regional",
            post_regional_stacks,
            expected_stack_ids,
        )
        for stack_name in phase_remaining:
            if stack_name not in failed:
                failed.append(stack_name)
        if any(stack in failed for stack in post_regional_stacks) or phase_remaining:
            return finish(False)

        if regional_api_stacks:
            if parallel and len(regional_api_stacks) > 1:
                successful_api, failed_api = self._destroy_stacks_parallel(
                    stacks=regional_api_stacks,
                    force=force,
                    on_stack_start=on_stack_start,
                    on_stack_complete=on_stack_complete,
                    max_workers=max_workers,
                    expected_stack_ids=expected_stack_ids,
                    authorize_stack=authorize_stack,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    prepared_change_sets=prepared_change_sets,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
                successful.extend(successful_api)
                failed.extend(failed_api)
            else:
                for stack_name in regional_api_stacks:
                    if on_stack_start:
                        on_stack_start(stack_name)
                    success = self.destroy(
                        stack_name=stack_name,
                        force=force,
                        expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                        **destroy_safety,
                    )
                    (successful if success else failed).append(stack_name)
                    if on_stack_complete:
                        on_stack_complete(stack_name, success)
            phase_remaining = self._destroy_phase_remaining_stacks(
                "regional API bridge",
                regional_api_stacks,
                expected_stack_ids,
            )
            for stack_name in phase_remaining:
                if stack_name not in failed:
                    failed.append(stack_name)
            if any(stack in failed for stack in regional_api_stacks) or phase_remaining:
                return finish(False)

        watchdog_stops: dict[str, Event] = {}
        watchdog_threads: dict[str, Thread] = {}
        watchdog_targets = (
            [
                name
                for name in regional_stacks
                if strict_resources.get(name, {}).get("cluster_security_group_id")
            ]
            if strict_identity
            else list(regional_stacks)
        )
        try:
            for stack_name in watchdog_targets:
                details = strict_resources.get(stack_name, {})
                stop_event = Event()
                watchdog_stops[stack_name] = stop_event
                watchdog_threads[stack_name] = self._start_eks_sg_watchdog(
                    stack_name,
                    stop_event,
                    region=details.get("region"),
                    security_group_id=details.get("cluster_security_group_id"),
                    vpc_id=details.get("vpc_id"),
                )

            if regional_stacks:
                if parallel and len(regional_stacks) > 1:
                    successful_regional, failed_regional = self._destroy_stacks_parallel(
                        stacks=regional_stacks,
                        force=force,
                        on_stack_start=on_stack_start,
                        on_stack_complete=on_stack_complete,
                        max_workers=max_workers,
                        expected_stack_ids=expected_stack_ids,
                        authorize_stack=authorize_stack,
                        allow_bootstrap=allow_bootstrap,
                        bootstrap_stacks=bootstrap_stacks,
                        prepared_change_sets=prepared_change_sets,
                        strict_deployment_token=strict_deployment_token,
                        on_change_set_prepared=on_change_set_prepared,
                        on_ecr_repository_created=on_ecr_repository_created,
                    )
                    successful.extend(successful_regional)
                    failed.extend(failed_regional)
                else:
                    for stack_name in regional_stacks:
                        if on_stack_start:
                            on_stack_start(stack_name)
                        success = self.destroy(
                            stack_name=stack_name,
                            force=force,
                            expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                            **destroy_safety,
                        )
                        (successful if success else failed).append(stack_name)
                        if on_stack_complete:
                            on_stack_complete(stack_name, success)
        finally:
            for stop_event in watchdog_stops.values():
                stop_event.set()
            for stack_name, thread in watchdog_threads.items():
                try:
                    thread.join(timeout=5)
                except Exception as exc:
                    logger.exception("Could not join teardown watchdog for %s", stack_name)
                    record_cleanup(
                        "eks-security-group",
                        {"stack": stack_name, "errors": [f"{type(exc).__name__}: {exc}"]},
                    )
                    if strict_identity and stack_name not in failed:
                        failed.append(stack_name)
                    continue
                details = strict_resources.get(stack_name, {})
                if strict_identity and thread.is_alive():
                    outcome = {
                        "stack": stack_name,
                        "errors": ["watchdog thread did not stop"],
                    }
                else:
                    outcome = self._cleanup_eks_security_groups(
                        stack_name,
                        region=details.get("region"),
                        security_group_id=details.get("cluster_security_group_id"),
                        vpc_id=details.get("vpc_id"),
                    )
                record_cleanup("eks-security-group", outcome)
                if (
                    strict_identity
                    and (outcome.get("errors") or outcome.get("blocked_by_enis"))
                    and stack_name not in failed
                ):
                    failed.append(stack_name)

        phase_remaining = self._destroy_phase_remaining_stacks(
            "regional",
            regional_stacks,
            expected_stack_ids,
        )
        for stack_name in phase_remaining:
            if stack_name not in failed:
                failed.append(stack_name)
        if any(stack in failed for stack in regional_stacks) or phase_remaining:
            return finish(False)

        # Every regional stack is verifiably absent here, so each EKS cluster and
        # its CSI driver are gone and the volumes it provisioned can never
        # reattach. Running after the barrier (rather than inside the per-stack
        # loop above) means parallel and sequential teardowns publish the same
        # outcomes in the same order, with no concurrent access to the report.
        # Cleanup results deliberately do not feed ``failed``: these stacks are
        # already deleted, and relabeling one as failed would send the CLI's
        # retry loop back to CDK for a stack that no longer exists.
        for stack_name in regional_stacks:
            record_cleanup(
                "dynamic-pvs",
                self._cleanup_cluster_volumes(
                    stack_name,
                    region=strict_resources.get(stack_name, {}).get("region"),
                    retain=retain_volumes,
                ),
            )

        for stack_name in pre_regional_stacks:
            if on_stack_start:
                on_stack_start(stack_name)
            success = self.destroy(
                stack_name=stack_name,
                force=force,
                expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                **destroy_safety,
            )
            (successful if success else failed).append(stack_name)
            if on_stack_complete:
                on_stack_complete(stack_name, success)

            phase_remaining = self._destroy_phase_remaining_stacks(
                "pre-regional global",
                [stack_name],
                expected_stack_ids,
            )
            for remaining_stack in phase_remaining:
                if remaining_stack not in failed:
                    failed.append(remaining_stack)
            if not success or phase_remaining:
                return finish(False)

        return finish(len(failed) == 0)

    def _destroy_stacks_parallel(
        self,
        stacks: list[str],
        force: bool,
        on_stack_start: Callable[[str], None] | None,
        on_stack_complete: Callable[[str, bool], None] | None,
        max_workers: int,
        expected_stack_ids: Mapping[str, str | None] | None,
        authorize_stack: StackAuthorizationCallback | None,
        allow_bootstrap: bool,
        bootstrap_stacks: Mapping[str, Mapping[str, str]] | None,
        prepared_change_sets: PreparedChangeSetAuthority | None,
        strict_deployment_token: str | None = None,
        on_change_set_prepared: ChangeSetPreparedCallback | None = None,
        on_ecr_repository_created: EcrRepositoryCreatedCallback | None = None,
    ) -> tuple[list[str], list[str]]:
        """Destroy multiple stacks in parallel using separate CDK output directories."""
        import tempfile

        successful: list[str] = []
        failed: list[str] = []
        lock = Lock()

        def destroy_single(stack_name: str) -> tuple[str, bool]:
            # Use a unique output directory in /tmp for each parallel destruction
            output_dir = tempfile.mkdtemp(prefix=f"cdk-{stack_name}-")
            try:
                if on_stack_start:
                    with lock:
                        on_stack_start(stack_name)

                success = self.destroy(
                    stack_name=stack_name,
                    force=force,
                    output_dir=output_dir,
                    expected_stack_id=(expected_stack_ids or {}).get(stack_name),
                    expected_stack_ids=expected_stack_ids,
                    authorize_stack=authorize_stack,
                    allow_bootstrap=allow_bootstrap,
                    bootstrap_stacks=bootstrap_stacks,
                    prepared_change_sets=prepared_change_sets,
                    strict_deployment_token=strict_deployment_token,
                    on_change_set_prepared=on_change_set_prepared,
                    on_ecr_repository_created=on_ecr_repository_created,
                )
                return stack_name, success
            finally:
                try:
                    import shutil

                    if os.path.exists(output_dir):
                        shutil.rmtree(output_dir)
                except Exception as e:
                    logger.debug("Cleanup of %s failed: %s", output_dir, e)

        self._cdk_cancel_event.clear()
        futures: dict[Any, str] = {}
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
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
        except BaseException:
            self.cancel_active_cdk_processes()
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True)
        finally:
            self._cdk_cancel_event.clear()

        return successful, failed

    def _cleanup_backup_vault(
        self,
        *,
        expected_stack_id: str | None = None,
        authorize_stack: StackAuthorizationCallback | None = None,
        require_expected_identity: bool = False,
    ) -> dict[str, Any]:
        """Delete points only from the exact stack resource's physical vault."""
        import boto3

        global_region = self.config.global_region
        global_stack_name = f"{self.config.project_name}-global"
        result: dict[str, Any] = {
            "stack_name": global_stack_name,
            "stack_id": expected_stack_id,
            "status": "not-needed",
            "deleted_recovery_points": 0,
            "errors": [],
        }

        try:
            target = self._describe_stack_target(
                global_stack_name,
                expected_stack_id=expected_stack_id,
                require_expected_identity=require_expected_identity,
            )
            if target is None:
                result["status"] = "stack-absent"
                return result
            region, cloudformation, stack = target
            stack_id = str(stack["StackId"])
            result["stack_id"] = stack_id
            if region != global_region:
                raise RuntimeError(f"Global stack resolved to {region}, expected {global_region}")
            if authorize_stack is not None:
                authorize_stack(global_stack_name, region, stack_id)

            resources: list[dict[str, Any]] = []
            for page in cloudformation.get_paginator("list_stack_resources").paginate(
                StackName=stack_id
            ):
                resources.extend(
                    resource
                    for resource in page.get("StackResourceSummaries", [])
                    if resource.get("ResourceType") == "AWS::Backup::BackupVault"
                    and resource.get("PhysicalResourceId")
                )
            if not resources:
                result["status"] = "vault-resource-absent"
                return result
            if len(resources) != 1:
                raise RuntimeError(
                    f"Expected one AWS::Backup::BackupVault in {stack_id}; found {len(resources)}"
                )

            resource = resources[0]
            physical_id = str(resource["PhysicalResourceId"])
            if physical_id.startswith("arn:"):
                parts = physical_id.split(":", 5)
                if len(parts) != 6 or not parts[5].startswith("backup-vault:"):
                    raise RuntimeError(f"Invalid backup vault physical ARN: {physical_id}")
                vault_name = parts[5].removeprefix("backup-vault:")
            else:
                vault_name = physical_id
            if not vault_name:
                raise RuntimeError("CloudFormation returned an empty backup vault physical ID")

            backup_client = boto3.client("backup", region_name=global_region)
            described_vault = backup_client.describe_backup_vault(BackupVaultName=vault_name)
            vault_arn = str(described_vault.get("BackupVaultArn") or "")
            arn_parts = vault_arn.split(":", 5)
            if (
                len(arn_parts) != 6
                or arn_parts[2] != "backup"
                or arn_parts[3] != global_region
                or arn_parts[5] != f"backup-vault:{vault_name}"
            ):
                raise RuntimeError(
                    "AWS Backup identity does not match the CloudFormation physical resource"
                )
            if physical_id.startswith("arn:") and physical_id != vault_arn:
                raise RuntimeError("Backup vault ARN changed after CloudFormation resolution")

            result.update(
                {
                    "status": "inspected",
                    "logical_id": str(resource.get("LogicalResourceId") or ""),
                    "physical_id": physical_id,
                    "vault_name": vault_name,
                    "vault_arn": vault_arn,
                }
            )
            paginator = backup_client.get_paginator("list_recovery_points_by_backup_vault")
            for page in paginator.paginate(BackupVaultName=vault_name):
                for recovery_point in page.get("RecoveryPoints", []):
                    recovery_point_arn = recovery_point.get("RecoveryPointArn")
                    if not recovery_point_arn:
                        continue
                    try:
                        backup_client.delete_recovery_point(
                            BackupVaultName=vault_name,
                            RecoveryPointArn=recovery_point_arn,
                        )
                        result["deleted_recovery_points"] += 1
                    except Exception as exc:
                        result["errors"].append(
                            {
                                "recovery_point_arn": str(recovery_point_arn),
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
            if result["deleted_recovery_points"]:
                print(
                    f"  Cleaned up {result['deleted_recovery_points']} backup recovery "
                    f"points from {vault_name}"
                )
            result["status"] = "completed" if not result["errors"] else "partial"
        except Exception as exc:
            result["status"] = "failed"
            result["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
            print(f"  Warning: Backup vault cleanup failed (non-fatal): {exc}")
        return result

    def cleanup_orphaned_bastions(
        self,
        stacks: list[str] | None = None,
        *,
        parallel: bool = True,
        resource_targets: Mapping[str, Mapping[str, str]] | None = None,
    ) -> int:
        """Terminate CLI bastions, using exact stack VPC IDs in strict mode."""
        if stacks is None:
            stacks = self.list_stacks()
        if resource_targets is not None:
            regional_stacks = [name for name in stacks if name in resource_targets]
        else:
            regional_stacks = [
                stack
                for stack in stacks
                if not stack.endswith(("-global", "-api-gateway", "-monitoring", "-analytics"))
            ]

        def cleanup_one(stack_name: str) -> int:
            details = (resource_targets or {}).get(stack_name, {})
            return self._cleanup_orphaned_bastions(
                stack_name,
                region=details.get("region"),
                vpc_id=details.get("vpc_id"),
                fail_closed=resource_targets is not None,
            )

        if not regional_stacks:
            return 0
        if len(regional_stacks) == 1 or not parallel:
            terminated = sum(cleanup_one(stack_name) for stack_name in regional_stacks)
        else:
            # Each region has independent EC2 waiters. Run them concurrently so
            # one slow termination does not add its full timeout to every other
            # region before CloudFormation can start deleting stacks.
            with ThreadPoolExecutor(max_workers=min(4, len(regional_stacks))) as executor:
                terminated = sum(executor.map(cleanup_one, regional_stacks))
        if terminated:
            print(
                f"  Requested termination for {terminated} orphaned ephemeral "
                "SSM bastion(s) before stack deletion."
            )
        return terminated

    def _cleanup_orphaned_bastions(
        self,
        stack_name: str,
        *,
        region: str | None = None,
        vpc_id: str | None = None,
        fail_closed: bool = False,
    ) -> int:
        """Terminate tagged bastions only inside a resolved stack VPC."""
        import boto3

        from .ephemeral_bastion import (
            BASTION_PURPOSE,
            TAG_EPHEMERAL_KEY,
            TAG_PROJECT_KEY,
            TAG_PURPOSE_KEY,
            bastion_instance_name,
        )

        region = region or self._get_deploy_region(stack_name)
        if not region:
            if fail_closed:
                raise RuntimeError(f"Strict bastion cleanup lacks a Region for {stack_name}")
            return 0

        project_name = str(self.config.project_name)
        expected_name = bastion_instance_name(project_name)
        try:
            ec2 = boto3.client("ec2", region_name=region)
            if vpc_id:
                vpcs = [{"VpcId": vpc_id}]
            else:
                vpcs = ec2.describe_vpcs(
                    Filters=[
                        {
                            "Name": "tag:aws:cloudformation:stack-name",
                            "Values": [stack_name],
                        }
                    ]
                ).get("Vpcs", [])
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(
                    f"Strict bastion cleanup could not inspect {stack_name}"
                ) from exc
            print(f"  Warning: Bastion cleanup could not inspect {stack_name}: {exc}")
            return 0

        instance_ids: list[str] = []
        eni_ids: list[str] = []
        for vpc in vpcs:
            candidate_vpc_id = str(vpc.get("VpcId") or "")
            if not candidate_vpc_id:
                if fail_closed:
                    raise RuntimeError(f"Strict bastion cleanup has no VPC ID for {stack_name}")
                continue
            try:
                reservations = ec2.describe_instances(
                    Filters=[
                        {"Name": "vpc-id", "Values": [candidate_vpc_id]},
                        {"Name": f"tag:{TAG_EPHEMERAL_KEY}", "Values": ["true"]},
                        {"Name": f"tag:{TAG_PURPOSE_KEY}", "Values": [BASTION_PURPOSE]},
                        {
                            "Name": "instance-state-name",
                            "Values": [
                                "pending",
                                "running",
                                "stopping",
                                "stopped",
                                "shutting-down",
                            ],
                        },
                    ]
                ).get("Reservations", [])
            except Exception as exc:
                if fail_closed:
                    raise RuntimeError(
                        f"Strict bastion lookup failed in {stack_name} ({candidate_vpc_id})"
                    ) from exc
                logger.warning(
                    "Bastion lookup failed in %s (%s): %s",
                    stack_name,
                    candidate_vpc_id,
                    exc,
                )
                continue

            for reservation in reservations:
                for instance in reservation.get("Instances", []):
                    tags = {
                        str(tag.get("Key")): str(tag.get("Value"))
                        for tag in instance.get("Tags", [])
                        if tag.get("Key") is not None
                    }
                    tagged_project = tags.get(TAG_PROJECT_KEY)
                    if tagged_project != project_name and not (
                        tagged_project is None and tags.get("Name") == expected_name
                    ):
                        continue
                    instance_id = instance.get("InstanceId")
                    if instance_id:
                        instance_ids.append(str(instance_id))
                    for interface in instance.get("NetworkInterfaces", []):
                        attachment = interface.get("Attachment") or {}
                        if not (
                            attachment.get("DeviceIndex") == 0
                            and attachment.get("DeleteOnTermination") is True
                        ):
                            continue
                        eni_id = interface.get("NetworkInterfaceId")
                        if eni_id:
                            eni_ids.append(str(eni_id))

        instance_ids = list(dict.fromkeys(instance_ids))
        eni_ids = list(dict.fromkeys(eni_ids))
        if not instance_ids:
            return 0

        try:
            ec2.terminate_instances(InstanceIds=instance_ids)
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(f"Strict bastion termination failed in {stack_name}") from exc
            print(f"  Warning: Failed to terminate ephemeral bastion(s) in {stack_name}: {exc}")
            return 0

        print(
            f"  Terminating {len(instance_ids)} ephemeral SSM bastion(s) in "
            f"{stack_name}: {', '.join(instance_ids)}"
        )
        try:
            ec2.get_waiter("instance_terminated").wait(
                InstanceIds=instance_ids,
                WaiterConfig={"Delay": 5, "MaxAttempts": 60},
            )
        except Exception as exc:
            if fail_closed:
                raise RuntimeError(
                    f"Strict bastion termination did not converge in {stack_name}"
                ) from exc
            logger.warning("Timed out waiting for bastion termination in %s: %s", stack_name, exc)

        remaining_enis = self._wait_for_bastion_network_interfaces(ec2, eni_ids)
        if remaining_enis:
            message = (
                f"{len(remaining_enis)} bastion network interface(s) in {stack_name} "
                f"have not released: {', '.join(sorted(remaining_enis))}"
            )
            if fail_closed:
                raise RuntimeError(message)
            print(f"  Warning: {message}. The destroy retry will check again.")
        return len(instance_ids)

    @staticmethod
    def _wait_for_bastion_network_interfaces(
        ec2: Any,
        eni_ids: list[str],
        *,
        timeout_seconds: float = 120.0,
        poll_interval_seconds: float = 2.0,
    ) -> set[str]:
        """Wait for terminated bastion ENIs, deleting detached leftovers.

        EC2 normally deletes a primary ENI with its instance. If it becomes
        detached instead, it is safe to delete here because its owning instance
        was selected by the project/VPC bastion filters and termination has
        already been requested.
        """
        import time as _time

        remaining = set(eni_ids)
        deadline = _time.monotonic() + timeout_seconds
        while remaining:
            for eni_id in tuple(remaining):
                try:
                    response = ec2.describe_network_interfaces(NetworkInterfaceIds=[eni_id])
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code")
                    if code == "InvalidNetworkInterfaceID.NotFound":
                        remaining.discard(eni_id)
                        continue
                    logger.warning("Could not inspect bastion ENI %s: %s", eni_id, exc)
                    return remaining
                except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
                    logger.warning("Could not inspect bastion ENI %s: %s", eni_id, exc)
                    return remaining

                interfaces = response.get("NetworkInterfaces", [])
                if not interfaces:
                    remaining.discard(eni_id)
                    continue
                if interfaces[0].get("Status") == "available":
                    try:
                        ec2.delete_network_interface(NetworkInterfaceId=eni_id)
                        remaining.discard(eni_id)
                    except ClientError as exc:
                        code = exc.response.get("Error", {}).get("Code")
                        if code == "InvalidNetworkInterfaceID.NotFound":
                            remaining.discard(eni_id)
                        else:
                            logger.debug("Delete of bastion ENI %s failed: %s", eni_id, exc)
                    except Exception as exc:  # noqa: BLE001 - retry until timeout
                        logger.debug("Delete of bastion ENI %s failed: %s", eni_id, exc)

            if not remaining or _time.monotonic() >= deadline:
                break
            _time.sleep(poll_interval_seconds)
        return remaining

    # ------------------------------------------------------------------
    # Implicit log-group + bastion IAM cleanup (non-strict destroy only)
    # ------------------------------------------------------------------
    #
    # CloudFormation only deletes the log groups it modeled. Lambda default
    # groups (``/aws/lambda/<function>``), the EKS control-plane group
    # (``/aws/eks/<cluster>/cluster``), and the Container Insights groups
    # (``/aws/containerinsights/<cluster>/…``) are created out-of-band by
    # the services themselves, so ``destroy-all`` used to report success
    # while leaving them behind — a real teardown orphaned 22 of them plus
    # the ephemeral-bastion IAM role/profile, which then failed the live
    # release validation's clean-account baseline gate.
    #
    # The cleanup below deletes ONLY exact names derived from the project's
    # own stack resources, captured while the stacks still exist, and only
    # for stacks whose deletion actually succeeded. It never runs in strict
    # (live-validation) teardowns: the harness checkpoints, tags, and
    # fences its own log-group generations and must remain the single
    # owner of that deletion authority.

    # The service-side patterns implicit log groups follow. An explicit
    # ``AWS::Logs::LogGroup`` resource is deliberately absent here —
    # CloudFormation owns those directly.
    _EKS_CONTAINER_INSIGHTS_SUFFIXES = ("application", "dataplane", "host", "performance")

    @staticmethod
    def _implicit_log_group_names(resource_type: str, physical_id: str) -> tuple[str, ...]:
        """Exact implicit log-group names a stack resource creates out-of-band."""
        if resource_type == "AWS::Lambda::Function":
            return (f"/aws/lambda/{physical_id}",)
        if resource_type == "AWS::EKS::Cluster":
            return (
                f"/aws/eks/{physical_id}/cluster",
                *(
                    f"/aws/containerinsights/{physical_id}/{suffix}"
                    for suffix in StackManager._EKS_CONTAINER_INSIGHTS_SUFFIXES
                ),
            )
        return ()

    def _collect_implicit_log_groups(self, stacks: Collection[str]) -> dict[str, dict[str, Any]]:
        """Derive per-stack implicit log-group names while the stacks are live.

        Best-effort: a stack that cannot be described or listed is skipped
        with a warning — collection must never block the destroy itself.
        """
        collected: dict[str, dict[str, Any]] = {}
        for stack_name in stacks:
            try:
                target = self._describe_stack_target(stack_name)
                if target is None:
                    continue
                region, cloudformation, stack = target
                names: list[str] = []
                paginator = cloudformation.get_paginator("list_stack_resources")
                for page in paginator.paginate(StackName=str(stack["StackId"])):
                    for item in page.get("StackResourceSummaries", []):
                        resource_type = str(item.get("ResourceType") or "")
                        physical_id = str(item.get("PhysicalResourceId") or "")
                        if not physical_id:
                            continue
                        names.extend(self._implicit_log_group_names(resource_type, physical_id))
                if names:
                    collected[stack_name] = {"region": region, "log_groups": sorted(set(names))}
            except Exception as exc:  # noqa: BLE001 - best-effort collection
                logger.warning("Could not derive implicit log groups for %s: %s", stack_name, exc)
        return collected

    def _cleanup_implicit_log_groups(
        self,
        collected: Mapping[str, Mapping[str, Any]],
        successful_stacks: Collection[str],
    ) -> dict[str, Any]:
        """Delete the exact derived log groups of successfully destroyed stacks.

        A missing group is normal (a Lambda that never logged, or a custom
        ``LoggingConfig`` pointing elsewhere) and is recorded, not retried.
        Every error is recorded and swallowed: cleanup never converts a
        successful destroy into a failure.
        """
        import boto3

        outcome: dict[str, Any] = {"deleted": [], "missing": [], "errors": []}
        clients: dict[str, Any] = {}
        for stack_name in sorted(successful_stacks):
            details = collected.get(stack_name)
            if not details:
                continue
            region = str(details.get("region") or "")
            for name in details.get("log_groups", []):
                try:
                    client = clients.get(region)
                    if client is None:
                        client = boto3.client("logs", region_name=region)
                        clients[region] = client
                    client.delete_log_group(logGroupName=name)
                    outcome["deleted"].append(f"{region}:{name}")
                except ClientError as exc:
                    code = str(exc.response.get("Error", {}).get("Code") or "")
                    if code == "ResourceNotFoundException":
                        outcome["missing"].append(f"{region}:{name}")
                    else:
                        outcome["errors"].append(f"{region}:{name}: {code}")
                except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                    outcome["errors"].append(f"{region}:{name}: {type(exc).__name__}: {exc}")
        if outcome["deleted"]:
            print(
                f"  Deleted {len(outcome['deleted'])} implicit CloudWatch log group(s) "
                "left behind by Lambda/EKS/Container Insights."
            )
        for failure in outcome["errors"]:
            logger.warning("Implicit log-group cleanup failed for %s", failure)
        return outcome

    def _cleanup_traffic_dial_parameters(self) -> dict[str, Any]:
        """Best-effort purge of the runtime traffic-dial SSM parameter tree.

        The traffic-dial controller Lambda writes ``/{project}/traffic-dial/
        state`` and ``gco capacity traffic-dial set`` writes ``override-*``
        siblings at runtime; CloudFormation never owns them, so stack
        deletion leaves them behind. A surviving override is the real
        hazard: the scheduled controller honors overrides indefinitely, so
        the next deployment in this account would silently pin that region's
        dial until someone noticed. Runs only after a *fully* successful
        teardown — while any stack remains, the accelerator may still be
        live and an override on it is standing operator intent.
        """
        from .capacity.traffic_dial import TrafficDialManager

        outcome: dict[str, Any] = {"deleted": [], "errors": []}
        try:
            outcome["deleted"] = TrafficDialManager(self.config).purge_runtime_parameters()
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            outcome["errors"].append(f"{type(exc).__name__}: {exc}")
        if outcome["deleted"]:
            print(
                f"  Deleted {len(outcome['deleted'])} runtime traffic-dial SSM "
                "parameter(s) (controller state / manual overrides)."
            )
        for failure in outcome["errors"]:
            logger.warning("Traffic-dial parameter cleanup failed: %s", failure)
        return outcome

    def _cleanup_bastion_iam(self) -> dict[str, Any]:
        """Best-effort teardown of the ephemeral-bastion IAM role + profile.

        ``destroy_ephemeral_bastion`` already attempts this when a tunnel
        closes normally, but a killed process leaves the pair behind (they
        cost nothing, yet fail any clean-account audit). Deletion is by the
        exact project-scoped names from the bastion naming contract; a
        ``NoSuchEntity`` response simply means there was nothing to clean.
        """
        from .ephemeral_bastion import (
            _run_aws,
            bastion_profile_name,
            bastion_role_name,
            build_iam_teardown_commands,
        )

        outcome: dict[str, Any] = {
            "completed_steps": 0,
            "absent_steps": 0,
            "errors": [],
        }
        try:
            role_name = bastion_role_name(self.config.project_name)
            profile_name = bastion_profile_name(self.config.project_name)
            outcome["role"] = role_name
            outcome["profile"] = profile_name
            steps = build_iam_teardown_commands(
                role_name,
                profile_name,
                self.config.global_region,
            )
        except Exception as exc:  # noqa: BLE001 - best-effort cleanup
            outcome["errors"].append(f"{type(exc).__name__}: {exc}")
            return outcome
        for step in steps:
            try:
                _run_aws(step)
                outcome["completed_steps"] += 1
            except RuntimeError as exc:
                if "NoSuchEntity" in str(exc):
                    outcome["absent_steps"] += 1
                    continue
                outcome["errors"].append(f"{' '.join(step[:3])}: {exc}")
        if outcome["completed_steps"] and not outcome["errors"]:
            print("  Removed the ephemeral-bastion IAM role and instance profile.")
        for failure in outcome["errors"]:
            logger.warning("Bastion IAM teardown step failed: %s", failure)
        return outcome

    def cleanup_eks_security_groups(self) -> None:
        """Clean up EKS-managed security groups across all regional stacks.

        Called between destroy retries to remove orphaned security groups
        that block VPC deletion.
        """
        stacks = self.list_stacks()
        # Regional stacks are everything that isn't a named global stack;
        # classify by suffix so this works for any project_name (#139).
        regional_stacks = [
            s for s in stacks if not s.endswith(("-global", "-api-gateway", "-monitoring"))
        ]
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
        regional_stacks = [
            s for s in stacks if not s.endswith(("-global", "-api-gateway", "-monitoring"))
        ]
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

    def _cleanup_eks_security_groups(
        self,
        stack_name: str,
        *,
        region: str | None = None,
        security_group_id: str | None = None,
        vpc_id: str | None = None,
    ) -> dict[str, Any]:
        """Delete empty EKS SGs, optionally by one exact preauthorized ID."""
        import boto3

        project_name = self.config.project_name
        region = region or stack_name.replace(f"{project_name}-", "", 1)
        cluster_name = stack_name
        outcome: dict[str, Any] = {
            "stack": stack_name,
            "region": region,
            "security_group_id": security_group_id,
            "inspected": 0,
            "deleted": [],
            "blocked_by_enis": [],
            "errors": [],
        }

        try:
            ec2 = boto3.client("ec2", region_name=region)
            try:
                if security_group_id:
                    response = ec2.describe_security_groups(GroupIds=[security_group_id])
                else:
                    response = ec2.describe_security_groups(
                        Filters=[
                            {
                                "Name": "group-name",
                                "Values": [f"eks-cluster-sg-{cluster_name}-*"],
                            }
                        ]
                    )
            except ClientError as exc:
                if (
                    security_group_id
                    and exc.response.get("Error", {}).get("Code") == "InvalidGroup.NotFound"
                ):
                    outcome["absent"] = True
                    return outcome
                raise

            for security_group in response.get("SecurityGroups", []):
                outcome["inspected"] += 1
                group_id = str(security_group["GroupId"])
                group_name = str(security_group.get("GroupName", ""))
                if security_group_id and group_id != security_group_id:
                    raise RuntimeError(
                        f"EC2 returned changed security-group identity for {security_group_id}"
                    )
                if vpc_id and str(security_group.get("VpcId") or "") != vpc_id:
                    raise RuntimeError(
                        f"Security group {group_id} no longer belongs to exact VPC {vpc_id}"
                    )
                interfaces = ec2.describe_network_interfaces(
                    Filters=[{"Name": "group-id", "Values": [group_id]}]
                ).get("NetworkInterfaces", [])
                if interfaces:
                    outcome["blocked_by_enis"].append(
                        {
                            "group_id": group_id,
                            "group_name": group_name,
                            "network_interface_ids": sorted(
                                str(interface.get("NetworkInterfaceId") or "")
                                for interface in interfaces
                                if interface.get("NetworkInterfaceId")
                            ),
                        }
                    )
                    logger.debug(
                        "Waiting for AWS to release %d EKS-managed ENI(s) from %s",
                        len(interfaces),
                        group_name,
                    )
                    continue
                try:
                    ec2.delete_security_group(GroupId=group_id)
                    outcome["deleted"].append({"group_id": group_id, "group_name": group_name})
                    print(f"  Cleaned up empty EKS security group: {group_name} ({group_id})")
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "InvalidGroup.NotFound":
                        outcome["absent"] = True
                        continue
                    outcome["errors"].append(
                        {"group_id": group_id, "error": f"{type(exc).__name__}: {exc}"}
                    )
                except Exception as exc:
                    outcome["errors"].append(
                        {"group_id": group_id, "error": f"{type(exc).__name__}: {exc}"}
                    )
        except Exception as exc:
            outcome["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
            logger.debug("EKS security group cleanup for %s failed: %s", stack_name, exc)
        return outcome

    def cleanup_cluster_volumes(
        self,
        stack_name: str,
        *,
        region: str | None = None,
        retain: bool = False,
    ) -> dict[str, Any]:
        """Sweep one regional stack's orphaned CSI volumes; no-op for global stacks.

        Entry point for the single-stack ``gco stacks destroy`` path, which has no
        orchestrated cleanup barrier of its own. Global stacks host no cluster, so
        they resolve to no work rather than a derived pseudo-Region.
        """
        if stack_name.endswith(("-global", "-api-gateway", "-monitoring")):
            return {"stack": stack_name, "skipped": "not-a-regional-stack"}
        return self._cleanup_cluster_volumes(stack_name, region=region, retain=retain)

    def _cleanup_cluster_volumes(
        self,
        stack_name: str,
        *,
        region: str | None = None,
        retain: bool = False,
    ) -> dict[str, Any]:
        """Delete the EBS volumes a destroyed cluster's CSI driver left behind.

        Deleting an EKS cluster does not delete the PersistentVolumes its EBS CSI
        driver provisioned, so every deploy/destroy cycle strands ``available``
        volumes tagged ``kubernetes.io/cluster/<cluster>`` for a cluster that no
        longer exists. Nothing can reattach them and they bill indefinitely (#268).
        They carry the CSI driver's tags rather than the CDK ``Project`` tag, so no
        project-scoped sweep or cost query can see them.

        Deletion is the default because it honors intent already declared
        elsewhere: the ``gco-observability-gp3`` StorageClass sets
        ``reclaimPolicy: Delete``, and these volumes survive only because the
        cluster is torn down before its PVCs are, so the CSI driver never receives
        the delete event. ``retain=True`` reports them instead, and either way
        every volume is named in the outcome — a silent leak is the actual bug.

        Fail-closed on ordering: the sweep first proves the cluster is absent, so a
        still-reconciling CSI driver is never raced. Ownership, ``available``
        state, and zero attachments are then rechecked immediately before each
        delete rather than trusted from the discovery snapshot. One volume's
        failure never stops the others, and nothing raises into the destroy flow.
        """
        import boto3

        project_name = self.config.project_name
        region = region or stack_name.replace(f"{project_name}-", "", 1)
        cluster_name = stack_name
        cluster_tag = f"kubernetes.io/cluster/{cluster_name}"
        outcome: dict[str, Any] = {
            "stack": stack_name,
            "region": region,
            "cluster": cluster_name,
            "retained": retain,
            "inspected": 0,
            "deleted": [],
            "surviving": [],
            "errors": [],
        }

        try:
            # Ordering gate: a live cluster means its CSI driver may still be
            # reconciling, so a detached volume can simply be between pod
            # restarts. Only a proven-absent cluster makes these volumes garbage.
            try:
                boto3.client("eks", region_name=region).describe_cluster(name=cluster_name)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                    raise
            else:
                outcome["cluster_present"] = True
                logger.debug(
                    "Skipping volume cleanup for %s: cluster is still present",
                    cluster_name,
                )
                return outcome

            ec2 = boto3.client("ec2", region_name=region)
            volumes: list[dict[str, Any]] = []
            for page in ec2.get_paginator("describe_volumes").paginate(
                Filters=[
                    {"Name": "tag-key", "Values": [cluster_tag]},
                    {"Name": "status", "Values": ["available"]},
                ]
            ):
                volumes.extend(page.get("Volumes", []))

            for volume in volumes:
                outcome["inspected"] += 1
                volume_id = str(volume["VolumeId"])
                record = {
                    "volume_id": volume_id,
                    "size_gib": volume.get("Size"),
                    "volume_type": volume.get("VolumeType"),
                    "availability_zone": volume.get("AvailabilityZone"),
                    "pvc": _volume_pvc_name(volume),
                }
                if retain:
                    outcome["surviving"].append({**record, "reason": "retained-by-request"})
                    continue
                blocked = self._volume_delete_blocked(ec2, volume_id, cluster_tag=cluster_tag)
                if blocked is not None:
                    outcome["surviving"].append({**record, "reason": blocked})
                    logger.debug("Leaving volume %s in place: %s", volume_id, blocked)
                    continue
                try:
                    ec2.delete_volume(VolumeId=volume_id)
                    outcome["deleted"].append(record)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
                        outcome["absent"] = True
                        continue
                    outcome["errors"].append(
                        {"volume_id": volume_id, "error": f"{type(exc).__name__}: {exc}"}
                    )
                except Exception as exc:
                    outcome["errors"].append(
                        {"volume_id": volume_id, "error": f"{type(exc).__name__}: {exc}"}
                    )
        except Exception as exc:
            outcome["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
            logger.debug("Cluster volume cleanup for %s failed: %s", stack_name, exc)
        self._price_surviving_volumes(outcome)
        _print_cluster_volume_outcome(outcome)
        return outcome

    @staticmethod
    def _volume_storage_price_per_gib_month(region: str, volume_type: str) -> float | None:
        """Return the current on-demand $/GiB-month for a volume type in a Region.

        Priced at teardown time against the target Region rather than from a
        constant in this file: EBS rates differ per Region and change over time, so
        a checked-in number would quietly drift into misinforming the operator.

        Returns ``None`` whenever the real rate cannot be established — no
        credentials for ``pricing:GetProducts``, an unroutable endpoint, an
        emulator that does not implement the Price List API, or an unrecognized
        response shape. Callers must say the cost is unknown rather than
        substitute a guess. Timeouts are short and retries few because this is a
        cosmetic annotation on the teardown path and must never hold it up.
        """
        try:
            import boto3
            from botocore.config import Config

            # The Price List API is only offered in a few Regions; us-east-1 is
            # the canonical endpoint and is what cli/capacity/checker.py uses.
            # The Region being priced is a filter, not the endpoint.
            pricing = boto3.client(
                "pricing",
                region_name="us-east-1",
                config=Config(
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"max_attempts": 2},
                ),
            )
            response = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "productFamily", "Value": "Storage"},
                    {"Type": "TERM_MATCH", "Field": "volumeApiName", "Value": volume_type},
                    {"Type": "TERM_MATCH", "Field": "regionCode", "Value": region},
                ],
                MaxResults=1,
            )
            for entry in response.get("PriceList") or []:
                product = json.loads(entry)
                for term in (product.get("terms") or {}).get("OnDemand", {}).values():
                    for dimension in (term.get("priceDimensions") or {}).values():
                        usd = (dimension.get("pricePerUnit") or {}).get("USD")
                        if usd is not None:
                            return float(usd)
        except Exception as exc:
            logger.debug(
                "Could not price %s storage in %s: %s",
                volume_type,
                region,
                exc,
            )
        return None

    def _price_surviving_volumes(self, outcome: dict[str, Any]) -> None:
        """Annotate an outcome with the monthly cost of the volumes left behind.

        Sets ``monthly_cost_usd`` when every surviving volume's type could be
        priced, and ``monthly_cost_unavailable`` with the reason otherwise. Only
        runs when volumes actually survived, so a teardown that cleaned up
        completely makes no pricing call at all.
        """
        surviving = list(outcome.get("surviving") or [])
        if not surviving:
            return
        region = str(outcome.get("region") or "")
        rates: dict[str, float | None] = {}
        for record in surviving:
            volume_type = str(record.get("volume_type") or "")
            if volume_type and volume_type not in rates:
                rates[volume_type] = self._volume_storage_price_per_gib_month(region, volume_type)

        unpriced = sorted({name for name, rate in rates.items() if rate is None})
        if not rates or unpriced:
            outcome["monthly_cost_unavailable"] = (
                "could not retrieve current EBS pricing for "
                + (", ".join(unpriced) if unpriced else "these volumes")
                + f" in {region}"
            )
            return
        outcome["monthly_cost_usd"] = round(
            sum(
                int(record.get("size_gib") or 0)
                * (rates.get(str(record.get("volume_type"))) or 0.0)
                for record in surviving
            ),
            2,
        )

    @staticmethod
    def _volume_delete_blocked(
        ec2: Any,
        volume_id: str,
        *,
        cluster_tag: str,
    ) -> str | None:
        """Return why ``volume_id`` must not be deleted, or None when it may be.

        Re-reads the volume immediately before deletion so a volume that was
        reattached, or whose ownership tag changed, since discovery is left alone.
        An unreadable volume is reported as blocked: acting without confirmation
        is worse than leaving one volume behind for the operator to see.
        """
        try:
            described = ec2.describe_volumes(VolumeIds=[volume_id]).get("Volumes", [])
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "InvalidVolume.NotFound":
                return "already-absent"
            return f"recheck-failed: {exc.response.get('Error', {}).get('Code') or 'ClientError'}"
        except Exception as exc:
            return f"recheck-failed: {type(exc).__name__}"
        if len(described) != 1:
            return "recheck-returned-ambiguous-identity"
        current = described[0]
        if str(current.get("VolumeId") or "") != volume_id:
            return "recheck-returned-changed-identity"
        if str(current.get("State") or "") != "available":
            return f"state-is-{current.get('State') or 'unknown'}"
        if current.get("Attachments"):
            return "volume-has-attachments"
        if not any(str(tag.get("Key") or "") == cluster_tag for tag in current.get("Tags") or []):
            return "cluster-ownership-tag-absent"
        return None

    def _start_eks_sg_watchdog(
        self,
        stack_name: str,
        stop_event: Event,
        *,
        region: str | None = None,
        security_group_id: str | None = None,
        vpc_id: str | None = None,
    ) -> Thread:
        """Start a background thread that polls for orphaned EKS security groups.

        EKS creates an ``eks-cluster-sg-<cluster-name>-*`` security group that
        is owned by the EKS service (not CloudFormation). The watchdog observes
        it throughout regional teardown and removes it only after AWS has
        released every attached ENI. Service-managed interfaces are never
        detached or deleted by the CLI.

        The thread exits when ``stop_event`` is set by the orchestrator at
        the end of the regional phase.
        """

        def _watchdog() -> None:
            while not stop_event.is_set():
                try:
                    self._cleanup_eks_security_groups(
                        stack_name,
                        region=region,
                        security_group_id=security_group_id,
                        vpc_id=vpc_id,
                    )
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


def _volume_pvc_name(volume: Mapping[str, Any]) -> str | None:
    """Return the PVC name the CSI driver recorded on a volume, when present."""
    for tag in volume.get("Tags") or []:
        if str(tag.get("Key") or "") == "kubernetes.io/created-for/pvc/name":
            return str(tag.get("Value") or "") or None
    return None


def _describe_volume_record(record: Mapping[str, Any]) -> str:
    """Render one volume as ``vol-x (50 GiB gp3, us-west-2a, pvc=prometheus-db)``."""
    size = f"{record['size_gib']} GiB" if record.get("size_gib") else "unknown size"
    if record.get("volume_type"):
        size = f"{size} {record['volume_type']}"
    parts = [size]
    if record.get("availability_zone"):
        parts.append(str(record["availability_zone"]))
    if record.get("pvc"):
        parts.append(f"pvc={record['pvc']}")
    return f"{record['volume_id']} ({', '.join(parts)})"


def _print_cluster_volume_outcome(outcome: Mapping[str, Any]) -> None:
    """Report what a cluster-volume sweep deleted, left behind, or could not do.

    Silent retention is the defect this feature exists to fix (#268), so every
    surviving volume is named on stdout — not only the deleted ones.
    """
    deleted = list(outcome.get("deleted") or [])
    surviving = list(outcome.get("surviving") or [])
    errors = list(outcome.get("errors") or [])
    cluster = outcome.get("cluster")

    if deleted:
        total_gib = sum(int(record.get("size_gib") or 0) for record in deleted)
        print(
            f"  Cleaned up {len(deleted)} orphaned EBS volume(s) "
            f"({total_gib} GiB) left by cluster {cluster}:"
        )
        for record in deleted:
            print(f"    - {_describe_volume_record(record)}")

    if surviving:
        total_gib = sum(int(record.get("size_gib") or 0) for record in surviving)
        cost = outcome.get("monthly_cost_usd")
        if cost is not None:
            billing = f", ${cost:.2f}/month at current {outcome.get('region')} rates"
        else:
            billing = ""
        print(
            f"  {len(surviving)} EBS volume(s) ({total_gib} GiB{billing}) from "
            f"cluster {cluster} were left in place:"
        )
        for record in surviving:
            print(f"    - {_describe_volume_record(record)} [{record.get('reason')}]")
        if cost is None:
            print(f"    Ongoing cost: {outcome.get('monthly_cost_unavailable')}.")
        print(
            "    Nothing can reattach these once the cluster is gone. Delete them "
            "with: aws ec2 delete-volume --region "
            f"{outcome.get('region')} --volume-id <vol-id>"
        )

    for failure in errors:
        target = failure.get("volume_id") or cluster
        logger.warning("Could not dispose of EBS volume %s: %s", target, failure.get("error"))
        print(f"  Could not dispose of EBS volume {target}: {failure.get('error')}")


def _is_regional_api_bridge_stack(
    stack: str,
    *,
    project_name: str,
    stack_names: Collection[str],
) -> bool:
    """Return whether ``stack`` is a configured per-Region API bridge.

    A bare ``"-regional-api-"`` substring is ambiguous because it is valid
    inside ``project_name``. Match the exact project-scoped bridge prefix and
    require the corresponding configured ``<project>-<region>`` base stack.
    """
    bridge_prefix = f"{project_name}-regional-api-"
    if not stack.startswith(bridge_prefix):
        return False
    region = stack.removeprefix(bridge_prefix)
    return bool(region) and f"{project_name}-{region}" in stack_names


def _get_stack_destroy_phases(
    stacks: list[str],
    *,
    project_name: str,
) -> tuple[list[str], list[str], list[str], list[str]]:
    """Classify and order the exact phases used by orchestrated destroy.

    Returns monitoring, regional API bridge, base regional, and pre-regional
    global phases. The public preview helper and the execution path both flatten
    this result, so custom project names and bridge dependencies cannot drift.
    """
    stack_names = set(stacks)
    monitoring_stacks = sorted(
        (stack for stack in stacks if stack.endswith("-monitoring")),
        reverse=True,
    )
    regional_api_stacks = sorted(
        (
            stack
            for stack in stacks
            if _is_regional_api_bridge_stack(
                stack,
                project_name=project_name,
                stack_names=stack_names,
            )
        ),
        reverse=True,
    )
    regional_stacks = sorted(
        (
            stack
            for stack in stacks
            if not stack.endswith(("-global", "-api-gateway", "-monitoring"))
            and not _is_regional_api_bridge_stack(
                stack,
                project_name=project_name,
                stack_names=stack_names,
            )
        ),
        reverse=True,
    )
    pre_regional_stacks = sorted(
        (stack for stack in stacks if stack.endswith(("-global", "-api-gateway"))),
        key=lambda stack: (
            1 if stack.endswith("-api-gateway") else (2 if stack.endswith("-global") else 0)
        ),
    )
    return (
        monitoring_stacks,
        regional_api_stacks,
        regional_stacks,
        pre_regional_stacks,
    )


def get_stack_deployment_order(
    stacks: list[str],
    *,
    project_name: str = "gco",
) -> list[str]:
    """
    Get the correct deployment order for stacks.

    Order: global stacks first, then regional stacks.
    Global stacks: <project>-global, <project>-api-gateway,
    <project>-analytics, <project>-monitoring
    Regional stacks: <project>-{region} (e.g., gco-us-east-1)

    Named stacks are classified by suffix so ordering is independent of
    ``project_name`` (#139): a non-``gco`` deployment (``acme-global`` …)
    orders identically. Regional stacks are ``<project>-<region>`` and match
    no named suffix, so they fall through to the regional bucket.
    """
    stack_names = set(stacks)
    global_stacks = []
    regional_stacks = []
    regional_api_stacks = []

    # Named (non-regional) stack priority by suffix (lower = deploy first).
    suffix_priority = {
        "-global": 1,
        "-api-gateway": 2,
        "-analytics": 2.5,
        "-monitoring": 3,
    }

    def _named_priority(stack: str) -> float | None:
        for suffix, prio in suffix_priority.items():
            if stack.endswith(suffix):
                return prio
        return None

    for stack in stacks:
        priority = _named_priority(stack)
        if priority is not None:
            global_stacks.append((priority, stack))
        elif _is_regional_api_bridge_stack(
            stack,
            project_name=project_name,
            stack_names=stack_names,
        ):
            regional_api_stacks.append(stack)
        else:
            regional_stacks.append(stack)

    # Keep bridge dependencies after every base regional stack. The
    # orchestrated lifecycle further separates monitoring into its own phase.
    global_stacks.sort(key=lambda x: x[0])
    regional_stacks.sort()
    regional_api_stacks.sort()

    return [s[1] for s in global_stacks] + regional_stacks + regional_api_stacks


def get_stack_destroy_order(
    stacks: list[str],
    *,
    project_name: str = "gco",
) -> list[str]:
    """Return the exact project-aware order used by orchestrated destroy."""
    phases = _get_stack_destroy_phases(stacks, project_name=project_name)
    return [stack for phase in phases for stack in phase]


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

    with _config_mutation_lock(cdk_json_path):
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

        serialized = json.dumps(cdk_config, indent=2).encode("utf-8")
        _atomic_write_bytes(
            cdk_json_path,
            serialized,
            mode=stat.S_IMODE(cdk_json_path.stat().st_mode),
        )


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


# =============================================================================
# Cluster observability configuration
# =============================================================================

# Mirrors the on-by-default cdk.json cluster_observability block. Unlike the
# other feature toggles this one defaults to enabled=True: a stock deploy
# installs kube-prometheus-stack on every regional cluster and operators opt
# out. The CDK side reads/validates the same block via
# ConfigLoader.get_cluster_observability_config.
_CLUSTER_OBSERVABILITY_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "grafana": {
        "persistence_size": "10Gi",
        "admin_user": "admin",
        "admin_password_rotation_schedule": "0 4 1 * *",
    },
    "prometheus": {"persistence_size": "50Gi", "retention": "15d"},
    "alertmanager": {"enabled": True, "persistence_size": "5Gi"},
}


def get_cluster_observability_config() -> dict[str, Any]:
    """Get the cluster observability configuration from cdk.json.

    Observability is per-region (installed on every regional cluster) but the
    toggle itself is global, so this takes no region argument. Returns the
    defaults merged with any operator overrides from the
    ``context.cluster_observability`` block.
    """
    return _get_feature_config("cluster_observability", _CLUSTER_OBSERVABILITY_DEFAULTS)


def update_cluster_observability_config(settings: dict[str, Any]) -> None:
    """Update the cluster observability toggle in cdk.json.

    ``gco monitoring enable`` / ``disable`` pass ``{"enabled": True/False}``;
    the grafana/prometheus/alertmanager sub-blocks are left untouched so an
    operator's sizing/retention/rotation overrides survive a disable/enable
    cycle.
    """
    _update_feature_config("cluster_observability", settings, _CLUSTER_OBSERVABILITY_DEFAULTS)
