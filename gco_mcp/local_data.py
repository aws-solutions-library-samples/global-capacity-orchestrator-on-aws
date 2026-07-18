"""Shared confinement and secure staging for MCP tools that access host data."""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

_LOCAL_ROOT_ENV = "GCO_STORAGE_LOCAL_ROOT"
_UPLOAD_STAGE_PREFIX = ".gco-mcp-upload-"


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
    """Descriptor-backed upload argument valid for the context lifetime."""

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
    if not stat.S_ISDIR(root_stat.st_mode):
        raise ValueError(f"{_LOCAL_ROOT_ENV} is not a directory: {root}")

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


def _create_stage_directory(root_fd: int) -> tuple[str, int]:
    """Create one unpredictable private staging directory under the root."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    for _ in range(10):
        name = f"{_UPLOAD_STAGE_PREFIX}{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        return name, os.open(name, flags, dir_fd=root_fd)
    raise OSError("Unable to allocate a private upload staging directory")


@contextmanager
def stage_upload_path(contract: LocalPathContract) -> Iterator[StagedUpload]:
    """Yield a private no-follow snapshot suitable for a short CLI upload.

    The snapshot contains only directories and hard links to regular,
    single-link files discovered through descriptor-relative no-follow opens.
    The CLI receives ``/dev/fd/<dirfd>/<name>`` and that directory descriptor is
    explicitly inherited by its subprocess, closing validation/use path races.
    """
    if not Path("/dev/fd").is_dir():
        raise ValueError("Secure upload staging requires /dev/fd support")

    root_fd = _verified_root_fd(contract)
    stage_name: str | None = None
    stage_fd: int | None = None
    source_fd: int | None = None
    source_parent_fd: int | None = None
    try:
        source_fd, source_parent_fd, source_name = _open_source(root_fd, contract)
        source_stat = os.fstat(source_fd)
        _verify_source_identity(contract, source_stat)

        stage_name, stage_fd = _create_stage_directory(root_fd)
        target_name = contract.resolved_path.name or "upload"
        if stat.S_ISREG(source_stat.st_mode):
            if source_parent_fd is None or source_name is None:
                raise ValueError("Regular upload source has no parent directory")
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

        argument = f"/dev/fd/{stage_fd}/{target_name}"
        staged_stat = os.stat(argument, follow_symlinks=False)
        if not (stat.S_ISREG(staged_stat.st_mode) or stat.S_ISDIR(staged_stat.st_mode)):
            raise ValueError("Secure upload staging produced an invalid source")
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
