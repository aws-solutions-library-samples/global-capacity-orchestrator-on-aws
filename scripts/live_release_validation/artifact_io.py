"""Owner-only artifact directory and atomic text I/O for live validation."""

from __future__ import annotations

import os
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TextIO

_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
REPORT_FILENAMES = frozenset(
    {
        "live-release-validation.json",
        "live-release-validation.md",
        "example-job-validation.json",
        "example-job-validation.md",
        "upgrade-validation.json",
        "upgrade-validation.md",
        "kubeconfig",
    }
)


def _validate_private_regular_metadata(metadata: os.stat_result, path: Path) -> None:
    """Reject links, special files, foreign owners, and non-private POSIX modes."""
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Live-validation output must be a regular file, not {path}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError(f"Live-validation output is not owned by this user: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != _PRIVATE_FILE_MODE:
        raise PermissionError(f"Live-validation output must have mode 0600: {path}")


def _validate_private_regular_file(path: Path) -> None:
    _validate_private_regular_metadata(path.lstat(), path)


def _validate_private_directory_metadata(metadata: os.stat_result, directory: Path) -> None:
    """Require a real, current-user-owned, owner-only run directory."""
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Live-validation output directory must be real: {directory}")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise PermissionError(
            f"Live-validation output directory is not owned by this user: {directory}"
        )
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != _PRIVATE_DIRECTORY_MODE:
        raise PermissionError(
            f"Live-validation output directory must already have mode 0700: {directory}"
        )


def ensure_private_directory(directory: Path) -> None:
    """Create a private directory or validate it without changing permissions."""
    directory = Path(directory)
    with suppress(FileExistsError):
        directory.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE, exist_ok=False)
    _validate_private_directory_metadata(directory.lstat(), directory)


def _assert_directory_binding(directory: Path, descriptor: int) -> None:
    """Fail if the verified pathname no longer names the pinned directory."""
    try:
        current = directory.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Live-validation output directory was rebound while open: {directory}"
        ) from exc
    if not os.path.samestat(current, os.fstat(descriptor)):
        raise RuntimeError(f"Live-validation output directory was rebound while open: {directory}")


@contextmanager
def _open_private_directory(directory: Path) -> Iterator[int | None]:
    """Pin a validated directory so artifact I/O cannot follow a rebound pathname."""
    directory = Path(directory)
    ensure_private_directory(directory)
    if os.name == "nt":
        yield None
        return

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if not no_follow or not directory_only:
        raise RuntimeError("Secure directory-descriptor operations are unavailable")

    before = directory.lstat()
    descriptor = os.open(
        directory,
        os.O_RDONLY | no_follow | directory_only | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not os.path.samestat(before, opened):
            raise RuntimeError(
                f"Live-validation output directory changed while opening: {directory}"
            )
        _validate_private_directory_metadata(opened, directory)
        yield descriptor
        _assert_directory_binding(directory, descriptor)
    finally:
        os.close(descriptor)


def ensure_private_run_directory(directory: Path, checkpoint_path: Path) -> None:
    """Validate that an existing private directory is dedicated to one harness run."""
    directory = Path(directory)
    allowed_names = {*REPORT_FILENAMES, checkpoint_path.name}
    with _open_private_directory(directory) as descriptor:
        if descriptor is None:
            entries = [(entry.name, entry.lstat()) for entry in directory.iterdir()]
        else:
            entries = [
                (name, os.stat(name, dir_fd=descriptor, follow_symlinks=False))
                for name in os.listdir(descriptor)
            ]
        for name, metadata in entries:
            is_temporary = any(
                name.startswith(f".{allowed_name}.") and name.endswith(".tmp")
                for allowed_name in allowed_names
            )
            entry = directory / name
            if name not in allowed_names and not is_temporary:
                raise ValueError(
                    "Live-validation output directory contains an unrelated entry and is not "
                    f"dedicated to this run: {entry}"
                )
            _validate_private_regular_metadata(metadata, entry)


def read_private_text(path: Path) -> str:
    """Read one owner-only regular file relative to a pinned directory."""
    with _open_private_directory(path.parent) as descriptor:
        if descriptor is None:
            _validate_private_regular_file(path)
            return path.read_text(encoding="utf-8")

        opened_descriptor = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=descriptor,
        )
        descriptor_to_close: int | None = opened_descriptor
        try:
            _validate_private_regular_metadata(os.fstat(opened_descriptor), path)
            text_handle: TextIO = os.fdopen(opened_descriptor, mode="r", encoding="utf-8")
            descriptor_to_close = None
            with text_handle:
                return text_handle.read()
        finally:
            if descriptor_to_close is not None:
                os.close(descriptor_to_close)


def atomic_write_text(path: Path, content: str) -> None:
    """Atomically persist owner-only text relative to a pinned private directory."""
    with _open_private_directory(path.parent) as descriptor:
        if descriptor is None:
            temporary_path_to_unlink: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=path.parent,
                    prefix=f".{path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as named_handle:
                    temporary_path = Path(named_handle.name)
                    temporary_path_to_unlink = temporary_path
                    named_handle.write(content)
                    named_handle.flush()
                    os.fsync(named_handle.fileno())
                os.replace(temporary_path, path)
                temporary_path_to_unlink = None
            finally:
                if temporary_path_to_unlink is not None:
                    temporary_path_to_unlink.unlink(missing_ok=True)
            return

        temporary_name = f".{path.name}.{secrets.token_hex(16)}.tmp"
        temporary_name_to_unlink: str | None = temporary_name
        descriptor_to_close: int | None = None
        try:
            opened_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
                _PRIVATE_FILE_MODE,
                dir_fd=descriptor,
            )
            descriptor_to_close = opened_descriptor
            os.fchmod(opened_descriptor, _PRIVATE_FILE_MODE)
            text_handle: TextIO = os.fdopen(opened_descriptor, mode="w", encoding="utf-8")
            descriptor_to_close = None
            with text_handle:
                text_handle.write(content)
                text_handle.flush()
                os.fsync(text_handle.fileno())

            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
            temporary_name_to_unlink = None
        finally:
            if descriptor_to_close is not None:
                os.close(descriptor_to_close)
            if temporary_name_to_unlink is not None:
                with suppress(FileNotFoundError):
                    os.unlink(temporary_name_to_unlink, dir_fd=descriptor)
