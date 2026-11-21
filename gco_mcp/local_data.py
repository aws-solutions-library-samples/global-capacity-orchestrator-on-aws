"""Shared confinement and secure staging for MCP tools that access host data."""

from __future__ import annotations

import errno
import os
import re
import secrets
import shutil
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-11-19T00:26:15Z
# Generated from Git commit: b6581899649d40769798e294f2ea42237f493c03
# Flowchart(s) generated from this file:
#   * ``resolve_local_path`` -> ``diagrams/code_diagrams/gco_mcp/local_data.resolve_local_path.html``
#     (PNG: ``diagrams/code_diagrams/gco_mcp/local_data.resolve_local_path.png``)
# Regenerate with ``SOURCE_DATE_EPOCH=<unix-seconds> GCO_DIAGRAM_SOURCE_COMMIT=<40-char-sha> python diagrams/generate.py --code-only``.
# <pyflowchart-code-diagram> END


_LOCAL_ROOT_ENV = "GCO_STORAGE_LOCAL_ROOT"
_UPLOAD_STAGE_PREFIX = ".gco-mcp-upload-"
_STAGE_NAME_RE = re.compile(r"\.gco-mcp-upload-(?:(?P<pid>[1-9][0-9]{0,9})-)?[0-9a-f]{32}")
"""Snapshot directory names: ``<prefix><pid>-<hex>``, or ``<prefix><hex>`` as
created before the owning process ID was part of the name."""
_LEGACY_STAGE_MAX_AGE_SECONDS = 2 * 60 * 60
"""Age after which a snapshot without a process ID in its name is stale. The
servers that created such names stopped an upload after two minutes."""


@dataclass(frozen=True)
class LocalPathContract:
    """A root-confined path and the filesystem identities checked at use time."""

    local_argument: str
    resolved_path: Path
    root: Path
    device: int
    inode: int
    source_device: int | None
    source_inode: int | None
    source_mode: int | None


@dataclass(frozen=True)
class StagedUpload:
    """Upload argument for the CLI, valid for the context lifetime.

    ``argument`` is ``/dev/fd/<directory_fd>/<name>`` where the host can walk
    an open directory through ``/dev/fd`` (Linux), else the snapshot's path.
    """

    argument: str
    directory_fd: int


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    """Return the device, inode, and file-type bits for one artifact."""
    return metadata.st_dev, metadata.st_ino, stat.S_IFMT(metadata.st_mode)


def resolve_local_path(
    local_path: str,
    *,
    require_exists: bool,
    purpose: str = "Local data",
) -> LocalPathContract:
    """Resolve ``local_path`` beneath the configured local-data root.

    Relative paths (including short forms such as ``weights`` and
    ``./weights``) resolve beneath ``GCO_STORAGE_LOCAL_ROOT`` rather than the
    server process's working directory. Lexical traversal and realpath symlink
    escapes are rejected. Existing source identity is captured so short upload
    tools can verify it again while building a private no-follow snapshot.
    """
    configured_root = os.environ.get(_LOCAL_ROOT_ENV, "").strip()
    if not configured_root:
        raise ValueError(f"{_LOCAL_ROOT_ENV} must be set before enabling local data access")
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise ValueError(
            "Local data access requires descriptor-relative no-follow filesystem support"
        )

    try:
        root = Path(configured_root).expanduser().resolve(strict=True)
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise ValueError(f"{_LOCAL_ROOT_ENV} could not be resolved safely") from exc
    except RuntimeError as exc:
        raise ValueError(f"{_LOCAL_ROOT_ENV} could not be resolved safely") from exc
    root_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    root_fd = os.open(root, root_flags)
    try:
        root_stat = os.fstat(root_fd)
    finally:
        os.close(root_fd)

    supplied = Path(local_path).expanduser()
    candidate = supplied if supplied.is_absolute() else root / supplied
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"{purpose} path must stay within {_LOCAL_ROOT_ENV}: {local_path}"
        ) from exc

    try:
        resolved = lexical.resolve(strict=require_exists)
    except FileNotFoundError as exc:
        raise ValueError(f"{purpose} source does not exist: {local_path}") from exc
    except OSError as exc:
        if exc.errno != errno.ELOOP:
            raise
        raise ValueError(f"{purpose} path could not be resolved safely: {local_path}") from exc
    except RuntimeError as exc:
        raise ValueError(f"{purpose} path could not be resolved safely: {local_path}") from exc
    if not resolved.is_relative_to(root):
        raise ValueError(f"{purpose} path must stay within {_LOCAL_ROOT_ENV}: {local_path}")

    source_stat: os.stat_result | None
    try:
        source_stat = os.stat(resolved, follow_symlinks=False)
    except FileNotFoundError:
        if require_exists:
            raise ValueError(f"{purpose} source does not exist: {local_path}") from None
        source_stat = None
    if source_stat is not None and not (
        stat.S_ISREG(source_stat.st_mode) or stat.S_ISDIR(source_stat.st_mode)
    ):
        raise ValueError(f"{purpose} source must be a regular file or directory: {local_path}")

    return LocalPathContract(
        local_argument=str(relative) if relative.parts else ".",
        resolved_path=resolved,
        root=root,
        device=root_stat.st_dev,
        inode=root_stat.st_ino,
        source_device=source_stat.st_dev if source_stat is not None else None,
        source_inode=source_stat.st_ino if source_stat is not None else None,
        source_mode=source_stat.st_mode if source_stat is not None else None,
    )


def _verified_root_fd(contract: LocalPathContract) -> int:
    """Open the configured root without following a swapped final symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(contract.root, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISDIR(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (
            contract.device,
            contract.inode,
        ):
            raise ValueError(f"{_LOCAL_ROOT_ENV} changed after validation")
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_source(
    root_fd: int,
    contract: LocalPathContract,
) -> tuple[int, int | None, str | None]:
    """Open the captured source by canonical root-relative components."""
    relative = contract.resolved_path.relative_to(contract.root)
    components = relative.parts
    current_fd = os.dup(root_fd)
    try:
        if not components:
            return current_fd, None, None
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        for component in components[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        source_flags = (
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        source_fd = os.open(components[-1], source_flags, dir_fd=current_fd)
        return source_fd, current_fd, components[-1]
    except Exception:
        os.close(current_fd)
        raise


def _verify_source_identity(contract: LocalPathContract, metadata: os.stat_result) -> None:
    """Reject source replacement, mount crossings, links, and special files."""
    expected = (contract.source_device, contract.source_inode)
    if None in expected or contract.source_mode is None:
        raise ValueError("Upload source must exist before secure staging")
    if _file_identity(metadata) != (
        contract.source_device,
        contract.source_inode,
        stat.S_IFMT(contract.source_mode),
    ):
        raise ValueError("Upload source changed after validation")
    if metadata.st_dev != contract.device:
        raise ValueError("Upload source crosses a filesystem boundary")
    if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
        raise ValueError("Upload source must be a regular file or directory")
    if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
        raise ValueError("Upload source must not be hard-linked")


def _link_verified_regular(
    source_dir_fd: int,
    source_name: str,
    source_stat: os.stat_result,
    destination_dir_fd: int,
    destination_name: str,
) -> None:
    """Hard-link one opened regular file and verify the name did not race."""
    if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
        raise ValueError(f"Upload entry is not a private regular file: {source_name}")
    os.link(
        source_name,
        destination_name,
        src_dir_fd=source_dir_fd,
        dst_dir_fd=destination_dir_fd,
        follow_symlinks=False,
    )
    try:
        source_after = os.stat(source_name, dir_fd=source_dir_fd, follow_symlinks=False)
        destination = os.stat(
            destination_name,
            dir_fd=destination_dir_fd,
            follow_symlinks=False,
        )
        expected = _file_identity(source_stat)
        if (
            _file_identity(source_after) != expected
            or _file_identity(destination) != expected
            or source_after.st_nlink != 2
            or destination.st_nlink != 2
        ):
            raise ValueError(f"Upload entry changed while staging: {source_name}")
    except Exception:
        with suppress(OSError):
            os.unlink(destination_name, dir_fd=destination_dir_fd)
        raise


def _stage_directory(
    source_fd: int,
    destination_fd: int,
    *,
    root_device: int,
    visited: set[tuple[int, int]],
    skip_internal_stages: bool,
) -> None:
    """Recursively snapshot regular files without following any link."""
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    regular_flags = (
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    for name in sorted(os.listdir(source_fd)):
        if skip_internal_stages and name.startswith(_UPLOAD_STAGE_PREFIX):
            continue
        before = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        if before.st_dev != root_device:
            raise ValueError(f"Upload entry crosses a filesystem boundary: {name}")
        if stat.S_ISLNK(before.st_mode):
            raise ValueError(f"Upload entry must not be a symbolic link: {name}")
        if stat.S_ISREG(before.st_mode):
            opened_fd = os.open(name, regular_flags, dir_fd=source_fd)
            try:
                opened = os.fstat(opened_fd)
                if _file_identity(opened) != _file_identity(before):
                    raise ValueError(f"Upload entry changed while opening: {name}")
                _link_verified_regular(source_fd, name, opened, destination_fd, name)
            finally:
                os.close(opened_fd)
            continue
        if stat.S_ISDIR(before.st_mode):
            identity = (before.st_dev, before.st_ino)
            if identity in visited:
                raise ValueError(f"Upload directory cycle detected: {name}")
            child_fd = os.open(name, directory_flags, dir_fd=source_fd)
            try:
                opened = os.fstat(child_fd)
                if _file_identity(opened) != _file_identity(before):
                    raise ValueError(f"Upload directory changed while opening: {name}")
                os.mkdir(name, mode=0o700, dir_fd=destination_fd)
                child_destination_fd = os.open(name, directory_flags, dir_fd=destination_fd)
                try:
                    visited.add(identity)
                    _stage_directory(
                        child_fd,
                        child_destination_fd,
                        root_device=root_device,
                        visited=visited,
                        skip_internal_stages=False,
                    )
                finally:
                    visited.remove(identity)
                    os.close(child_destination_fd)
            finally:
                os.close(child_fd)
            continue
        raise ValueError(f"Upload entry must be a regular file or directory: {name}")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError, OverflowError:
        return False
    except PermissionError:
        return True
    return True


def _remove_stale_stages(root_fd: int) -> None:
    """Delete snapshot directories that an interrupted upload left behind.

    A server killed mid-upload never runs the cleanup in
    :func:`stage_upload_path`, and the hard links in its snapshot gave every
    source file a second link, so each later upload of those files was
    refused as hard-linked. A snapshot is stale when the process named in it
    no longer exists (never this process's own, which another upload may be
    using), or, for a name without a process ID, when it is older than
    :data:`_LEGACY_STAGE_MAX_AGE_SECONDS`. Only directories owned by this
    user with exactly the snapshot name format are removed.
    """
    euid = os.geteuid()
    for name in os.listdir(root_fd):
        match = _STAGE_NAME_RE.fullmatch(name)
        if match is None:
            continue
        try:
            metadata = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != euid:
            continue
        pid = match.group("pid")
        if pid is None:
            stale = time.time() - metadata.st_mtime > _LEGACY_STAGE_MAX_AGE_SECONDS
        else:
            stale = int(pid) != os.getpid() and not _process_exists(int(pid))
        if stale:
            with suppress(OSError):
                shutil.rmtree(name, dir_fd=root_fd)


def _create_stage_directory(root_fd: int) -> tuple[str, int]:
    """Create one unpredictable private staging directory under the root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(10):
        name = f"{_UPLOAD_STAGE_PREFIX}{os.getpid()}-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        return name, os.open(name, flags, dir_fd=root_fd)
    raise OSError("Unable to allocate a private upload staging directory")


def _descriptor_argument(stage_fd: int, name: str, expected: os.stat_result) -> str | None:
    """Return ``/dev/fd/<stage_fd>/<name>`` when it reaches the staged entry.

    Linux resolves ``/dev/fd/<n>`` to the open directory, so a CLI that
    inherits the descriptor reads the snapshot even if its path is renamed.
    macOS has no such traversal (``ENOENT``); the caller then passes a path.
    """
    argument = f"/dev/fd/{stage_fd}/{name}"
    try:
        metadata = os.stat(argument, follow_symlinks=False)
    except OSError:
        return None
    if _file_identity(metadata) != _file_identity(expected):
        raise ValueError("Secure upload staging produced an invalid source")
    return argument


def _require_unmodifiable_by_others(root: Path) -> None:
    """Refuse when another user could rename an entry on the path to ``root``.

    With a path argument the CLI resolves the snapshot by name, so the root
    and every directory above it must be owned by this user or the superuser
    and not be group- or world-writable (unless sticky, like ``/tmp``).
    Otherwise another user could swap the snapshot between staging and use.
    """
    owners = {0, os.geteuid()}
    for directory in (root, *root.parents):
        metadata = os.stat(directory)
        shared = metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        if metadata.st_uid not in owners or (shared and not metadata.st_mode & stat.S_ISVTX):
            raise ValueError(
                "Upload staging on this host hands the CLI a path, so other users must not "
                f"be able to modify {directory} (it must be owned by you or root and not be "
                "group- or world-writable unless sticky)"
            )


def _path_argument(
    contract: LocalPathContract,
    stage_name: str,
    stage_fd: int,
    name: str,
    expected: os.stat_result,
) -> str:
    """Return the snapshot entry's path after checking it reaches the snapshot."""
    _require_unmodifiable_by_others(contract.root)
    stage_path = contract.root / stage_name
    argument = str(stage_path / name)
    if _file_identity(os.stat(stage_path, follow_symlinks=False)) != _file_identity(
        os.fstat(stage_fd)
    ) or _file_identity(os.stat(argument, follow_symlinks=False)) != _file_identity(expected):
        raise ValueError("Upload staging directory changed while staging")
    return argument


@contextmanager
def stage_upload_path(contract: LocalPathContract) -> Iterator[StagedUpload]:
    """Yield a private no-follow snapshot suitable for a short CLI upload.

    The snapshot contains only directories and hard links to regular,
    single-link files discovered through descriptor-relative no-follow opens,
    in a ``0700`` directory under the root. Where the host can walk an open
    directory through ``/dev/fd`` (Linux), the CLI receives
    ``/dev/fd/<dirfd>/<name>`` and inherits that descriptor, closing
    validation/use path races. Elsewhere (macOS) it receives the snapshot's
    path, which is only accepted when other users cannot modify the root or
    the directories above it. Snapshots left behind by an interrupted upload
    are removed first, so their hard links do not block this one.
    """
    root_fd = _verified_root_fd(contract)
    stage_name: str | None = None
    stage_fd: int | None = None
    source_fd: int | None = None
    source_parent_fd: int | None = None
    try:
        _remove_stale_stages(root_fd)
        source_fd, source_parent_fd, source_name = _open_source(root_fd, contract)
        source_stat = os.fstat(source_fd)
        _verify_source_identity(contract, source_stat)

        stage_name, stage_fd = _create_stage_directory(root_fd)
        target_name = contract.resolved_path.name or "upload"
        if stat.S_ISREG(source_stat.st_mode):
            # ``_open_source`` returns a null parent/name pair only for the root
            # directory itself, and the root is opened with ``O_DIRECTORY``, so a
            # regular source always carries both values. Stating that invariant
            # here narrows the types and fails loudly if it is ever broken,
            # rather than carrying an unreachable user-error path.
            assert source_parent_fd is not None and source_name is not None
            _link_verified_regular(
                source_parent_fd,
                source_name,
                source_stat,
                stage_fd,
                target_name,
            )
        else:
            os.mkdir(target_name, mode=0o700, dir_fd=stage_fd)
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
            destination_fd = os.open(target_name, flags, dir_fd=stage_fd)
            try:
                source_identity = (source_stat.st_dev, source_stat.st_ino)
                _stage_directory(
                    source_fd,
                    destination_fd,
                    root_device=contract.device,
                    visited={source_identity},
                    skip_internal_stages=source_identity == (contract.device, contract.inode),
                )
            finally:
                os.close(destination_fd)

        staged_stat = os.stat(target_name, dir_fd=stage_fd, follow_symlinks=False)
        if not (stat.S_ISREG(staged_stat.st_mode) or stat.S_ISDIR(staged_stat.st_mode)):
            raise ValueError("Secure upload staging produced an invalid source")
        argument = _descriptor_argument(stage_fd, target_name, staged_stat)
        if argument is None:
            argument = _path_argument(contract, stage_name, stage_fd, target_name, staged_stat)
        yield StagedUpload(argument=argument, directory_fd=stage_fd)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if source_parent_fd is not None:
            os.close(source_parent_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        if stage_name is not None:
            with suppress(OSError):
                shutil.rmtree(stage_name, dir_fd=root_fd)
        os.close(root_fd)
