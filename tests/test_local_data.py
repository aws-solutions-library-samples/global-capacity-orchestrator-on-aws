"""POSIX security and lifecycle tests for :mod:`gco_mcp.local_data`."""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from gco_mcp import local_data

pytestmark = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"),
    reason="local data requires descriptor-relative POSIX filesystem support",
)


@pytest.fixture
def local_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure a fresh local-data root on the test filesystem."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv(local_data._LOCAL_ROOT_ENV, str(root))
    return root


@contextmanager
def _directory_fd(path: Path) -> Iterator[int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _require_dev_fd_directory_access(root: Path) -> None:
    """Skip only when this POSIX host cannot traverse an open directory via /dev/fd."""
    if not Path("/dev/fd").is_dir():
        pytest.skip("secure upload staging requires /dev/fd")

    probe = root / ".dev-fd-probe"
    probe.write_bytes(b"probe")
    try:
        with _directory_fd(root) as descriptor:
            try:
                metadata = os.stat(
                    f"/dev/fd/{descriptor}/{probe.name}",
                    follow_symlinks=False,
                )
            except OSError as exc:
                pytest.skip(f"/dev/fd directory traversal is unavailable: {exc}")
            if not stat.S_ISREG(metadata.st_mode):
                pytest.skip("/dev/fd directory traversal did not reach the probe file")
    finally:
        probe.unlink(missing_ok=True)


def _stage_directories(root: Path) -> list[Path]:
    return sorted(
        (
            entry
            for entry in root.iterdir()
            if entry.name.startswith(local_data._UPLOAD_STAGE_PREFIX)
        ),
        key=lambda entry: entry.name,
    )


def _assert_no_stage_directories(root: Path) -> None:
    assert _stage_directories(root) == []


def _clone_stat(metadata: os.stat_result | SimpleNamespace, **changes: int) -> SimpleNamespace:
    values = {
        "st_dev": metadata.st_dev,
        "st_ino": metadata.st_ino,
        "st_mode": metadata.st_mode,
        "st_nlink": metadata.st_nlink,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _synthetic_contract(
    *,
    root_device: int = 41,
    source_device: int | None = 41,
    source_inode: int | None = 73,
    source_mode: int | None = stat.S_IFREG | 0o600,
) -> local_data.LocalPathContract:
    root = Path("/configured-root")
    return local_data.LocalPathContract(
        local_argument="payload",
        resolved_path=root / "payload",
        root=root,
        device=root_device,
        inode=19,
        source_device=source_device,
        source_inode=source_inode,
        source_mode=source_mode,
    )


def _synthetic_stat(
    *,
    device: int = 41,
    inode: int = 73,
    mode: int = stat.S_IFREG | 0o600,
    links: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(st_dev=device, st_ino=inode, st_mode=mode, st_nlink=links)


@pytest.mark.parametrize("configured_root", [None, "", "  \t  "])
def test_resolve_local_path_requires_configured_root(
    monkeypatch: pytest.MonkeyPatch,
    configured_root: str | None,
) -> None:
    if configured_root is None:
        monkeypatch.delenv(local_data._LOCAL_ROOT_ENV, raising=False)
    else:
        monkeypatch.setenv(local_data._LOCAL_ROOT_ENV, configured_root)

    with pytest.raises(ValueError, match="must be set before enabling local data access"):
        local_data.resolve_local_path("payload", require_exists=False)


@pytest.mark.parametrize("argument_kind", ["relative", "absolute"])
def test_resolve_local_path_accepts_confined_relative_and_absolute_paths(
    local_root: Path,
    argument_kind: str,
) -> None:
    source = local_root / "nested" / "payload.bin"
    source.parent.mkdir()
    source.write_bytes(b"payload")
    argument = "nested/payload.bin" if argument_kind == "relative" else str(source)

    contract = local_data.resolve_local_path(argument, require_exists=True)
    source_stat = os.stat(source, follow_symlinks=False)
    root_stat = os.stat(local_root, follow_symlinks=False)

    assert contract.local_argument == "nested/payload.bin"
    assert contract.resolved_path == source.resolve()
    assert contract.root == local_root.resolve()
    assert (contract.device, contract.inode) == (root_stat.st_dev, root_stat.st_ino)
    assert (contract.source_device, contract.source_inode, contract.source_mode) == (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_mode,
    )


def test_resolve_local_path_represents_the_root_as_dot(local_root: Path) -> None:
    contract = local_data.resolve_local_path(str(local_root), require_exists=True)

    assert contract.local_argument == "."
    assert contract.resolved_path == local_root.resolve()
    assert stat.S_ISDIR(contract.source_mode or 0)


@pytest.mark.parametrize("argument_kind", ["absolute", "lexical-traversal"])
def test_resolve_local_path_rejects_paths_outside_root(
    local_root: Path,
    tmp_path: Path,
    argument_kind: str,
) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    argument = str(outside) if argument_kind == "absolute" else "../outside.bin"

    with pytest.raises(ValueError, match="Model upload path must stay within"):
        local_data.resolve_local_path(
            argument,
            require_exists=True,
            purpose="Model upload",
        )


def test_resolve_local_path_rejects_symlink_escape(local_root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (local_root / "escape").symlink_to(outside)

    with pytest.raises(ValueError, match="path must stay within"):
        local_data.resolve_local_path("escape", require_exists=True)


def test_resolve_local_path_canonicalizes_a_confined_symlink(local_root: Path) -> None:
    actual = local_root / "actual"
    actual.mkdir()
    source = actual / "payload.bin"
    source.write_bytes(b"payload")
    (local_root / "alias").symlink_to(actual, target_is_directory=True)

    contract = local_data.resolve_local_path("alias/payload.bin", require_exists=True)

    assert contract.local_argument == "alias/payload.bin"
    assert contract.resolved_path == source.resolve()
    assert contract.resolved_path.is_relative_to(contract.root)


def test_resolve_local_path_records_existing_and_missing_source_identity(
    local_root: Path,
) -> None:
    source = local_root / "present.bin"
    source.write_bytes(b"present")
    source_stat = os.stat(source, follow_symlinks=False)

    existing = local_data.resolve_local_path("present.bin", require_exists=True)
    missing = local_data.resolve_local_path("missing.bin", require_exists=False)

    assert (existing.source_device, existing.source_inode, existing.source_mode) == (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_mode,
    )
    assert missing.resolved_path == (local_root / "missing.bin").resolve(strict=False)
    assert (missing.source_device, missing.source_inode, missing.source_mode) == (None, None, None)
    with pytest.raises(ValueError, match="source does not exist"):
        local_data.resolve_local_path("missing.bin", require_exists=True)


@pytest.mark.parametrize("require_exists", [False, True])
def test_resolve_local_path_handles_source_disappearing_after_resolution(
    local_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_exists: bool,
) -> None:
    source = local_root / "racy.bin"
    source.write_bytes(b"payload")
    real_stat = local_data.os.stat

    def disappearing_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if (
            isinstance(path, (str, os.PathLike))
            and Path(path) == source
            and kwargs.get("follow_symlinks") is False
        ):
            raise FileNotFoundError(path)
        return real_stat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(local_data.os, "stat", disappearing_stat)
        if require_exists:
            with pytest.raises(ValueError, match="source does not exist"):
                local_data.resolve_local_path("racy.bin", require_exists=True)
        else:
            contract = local_data.resolve_local_path("racy.bin", require_exists=False)
            assert (contract.source_device, contract.source_inode, contract.source_mode) == (
                None,
                None,
                None,
            )


def test_resolve_local_path_rejects_non_regular_source(local_root: Path) -> None:
    fifo = local_root / "input.pipe"
    os.mkfifo(fifo)

    with pytest.raises(ValueError, match="must be a regular file or directory"):
        local_data.resolve_local_path("input.pipe", require_exists=True)


def test_resolve_local_path_wraps_root_symlink_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.symlink_to(second)
    second.symlink_to(first)
    monkeypatch.setenv(local_data._LOCAL_ROOT_ENV, str(first))

    with pytest.raises(ValueError, match="could not be resolved safely"):
        local_data.resolve_local_path("payload", require_exists=False)


def test_resolve_local_path_wraps_source_symlink_cycle(local_root: Path) -> None:
    first = local_root / "first"
    second = local_root / "second"
    first.symlink_to(second.name)
    second.symlink_to(first.name)

    with pytest.raises(ValueError, match="path could not be resolved safely"):
        local_data.resolve_local_path("first", require_exists=True)


def test_verified_root_fd_rejects_replaced_root_and_closes_descriptor(
    local_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = local_root / "payload.bin"
    source.write_bytes(b"payload")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    retained_root = tmp_path / "retained-root"
    local_root.rename(retained_root)
    local_root.mkdir()

    real_open = local_data.os.open
    opened: list[int] = []

    def recording_open(
        path: str | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.append(descriptor)
        return descriptor

    with monkeypatch.context() as scoped:
        scoped.setattr(local_data.os, "open", recording_open)
        with pytest.raises(ValueError, match="changed after validation"):
            local_data._verified_root_fd(contract)

    assert len(opened) == 1
    with pytest.raises(OSError) as closed:
        os.fstat(opened[0])
    assert closed.value.errno == errno.EBADF
    assert (retained_root / "payload.bin").read_bytes() == b"payload"


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing", "must exist before secure staging"),
        ("replacement", "changed after validation"),
        ("cross-device", "crosses a filesystem boundary"),
        ("special", "must be a regular file or directory"),
        ("hard-link", "must not be hard-linked"),
    ],
)
def test_verify_source_identity_rejects_unsafe_identity(
    case: str,
    message: str,
) -> None:
    regular_mode = stat.S_IFREG | 0o600
    contract = _synthetic_contract(source_mode=regular_mode)
    metadata = _synthetic_stat(mode=regular_mode)

    if case == "missing":
        contract = replace(
            contract,
            source_device=None,
            source_inode=None,
            source_mode=None,
        )
    elif case == "replacement":
        metadata = _synthetic_stat(inode=contract.source_inode + 1, mode=regular_mode)  # type: ignore[operator]
    elif case == "cross-device":
        contract = replace(contract, device=99)
    elif case == "special":
        special_mode = stat.S_IFIFO | 0o600
        contract = replace(contract, source_mode=special_mode)
        metadata = _synthetic_stat(mode=special_mode)
    elif case == "hard-link":
        metadata = _synthetic_stat(mode=regular_mode, links=2)

    with pytest.raises(ValueError, match=message):
        local_data._verify_source_identity(contract, metadata)


@pytest.mark.parametrize(
    ("mode", "links"),
    [(stat.S_IFREG | 0o600, 1), (stat.S_IFDIR | 0o700, 2)],
)
def test_verify_source_identity_accepts_same_filesystem_file_or_directory(
    mode: int,
    links: int,
) -> None:
    contract = _synthetic_contract(source_mode=mode)
    metadata = _synthetic_stat(mode=mode, links=links)

    local_data._verify_source_identity(contract, metadata)


def test_open_source_opens_nested_components_relative_to_verified_root(local_root: Path) -> None:
    source = local_root / "one" / "two" / "payload.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"nested payload")
    contract = local_data.resolve_local_path("one/two/payload.bin", require_exists=True)
    root_fd = local_data._verified_root_fd(contract)
    source_fd: int | None = None
    parent_fd: int | None = None
    try:
        source_fd, parent_fd, source_name = local_data._open_source(root_fd, contract)
        assert source_name == "payload.bin"
        assert parent_fd is not None
        assert os.read(source_fd, 64) == b"nested payload"
        parent_entry = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
        opened = os.fstat(source_fd)
        assert local_data._file_identity(parent_entry) == local_data._file_identity(opened)
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def test_open_source_duplicates_root_for_dot_contract(local_root: Path) -> None:
    contract = local_data.resolve_local_path(".", require_exists=True)
    root_fd = local_data._verified_root_fd(contract)
    source_fd: int | None = None
    try:
        source_fd, parent_fd, source_name = local_data._open_source(root_fd, contract)
        assert source_fd != root_fd
        assert parent_fd is None
        assert source_name is None
        assert local_data._file_identity(os.fstat(source_fd)) == local_data._file_identity(
            os.fstat(root_fd)
        )
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(root_fd)


def test_create_stage_directory_retries_a_name_collision(
    local_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = local_root / f"{local_data._UPLOAD_STAGE_PREFIX}collision"
    collision.mkdir()
    tokens = iter(("collision", "fresh"))
    monkeypatch.setattr(local_data.secrets, "token_hex", lambda _size: next(tokens))

    stage_fd: int | None = None
    stage_path: Path | None = None
    with _directory_fd(local_root) as root_fd:
        try:
            stage_name, stage_fd = local_data._create_stage_directory(root_fd)
            stage_path = local_root / stage_name
            assert stage_name == f"{local_data._UPLOAD_STAGE_PREFIX}fresh"
            assert stat.S_ISDIR(os.fstat(stage_fd).st_mode)
            assert stat.S_IMODE(stage_path.stat().st_mode) == 0o700
        finally:
            if stage_fd is not None:
                os.close(stage_fd)
            if stage_path is not None:
                stage_path.rmdir()

    assert collision.is_dir()


def test_create_stage_directory_exhausts_colliding_names(
    local_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = local_root / f"{local_data._UPLOAD_STAGE_PREFIX}taken"
    collision.mkdir()
    attempts = 0

    def fixed_token(_size: int) -> str:
        nonlocal attempts
        attempts += 1
        return "taken"

    monkeypatch.setattr(local_data.secrets, "token_hex", fixed_token)
    with _directory_fd(local_root) as root_fd, pytest.raises(OSError, match="Unable to allocate"):
        local_data._create_stage_directory(root_fd)

    assert attempts == 10
    assert _stage_directories(local_root) == [collision]


def test_stage_upload_path_rejects_missing_dev_fd_support(
    local_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = local_root / "payload.bin"
    source.write_bytes(b"payload")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    real_is_dir = Path.is_dir

    def unavailable_dev_fd(path: Path) -> bool:
        if path == Path("/dev/fd"):
            return False
        return real_is_dir(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "is_dir", unavailable_dev_fd)
        with (
            pytest.raises(ValueError, match="requires /dev/fd support"),
            local_data.stage_upload_path(contract),
        ):
            pytest.fail("staging must not yield without /dev/fd support")

    _assert_no_stage_directories(local_root)
    assert os.stat(source, follow_symlinks=False).st_nlink == 1


def test_stage_upload_path_regular_file_keeps_descriptor_valid_and_cleans_up(
    local_root: Path,
) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "payload.bin"
    source.write_bytes(b"immutable payload")
    source_before = os.stat(source, follow_symlinks=False)
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    stage_fd = -1
    stage_argument = ""
    stage_root: Path | None = None

    with local_data.stage_upload_path(contract) as staged:
        stage_fd = staged.directory_fd
        stage_argument = staged.argument
        staged_path = Path(stage_argument)
        staged_stat = os.stat(stage_argument, follow_symlinks=False)
        stage_roots = _stage_directories(local_root)
        assert len(stage_roots) == 1
        stage_root = stage_roots[0]

        assert staged_path.read_bytes() == b"immutable payload"
        assert stat.S_ISREG(staged_stat.st_mode)
        assert (staged_stat.st_dev, staged_stat.st_ino) == (
            source_before.st_dev,
            source_before.st_ino,
        )
        assert staged_stat.st_nlink == 2
        assert os.stat(source, follow_symlinks=False).st_nlink == 2
        assert stat.S_ISDIR(os.fstat(stage_fd).st_mode)
        assert os.get_inheritable(stage_fd) is False
        assert stat.S_IMODE(stage_root.stat().st_mode) == 0o700

    with pytest.raises(OSError) as closed:
        os.fstat(stage_fd)
    assert closed.value.errno == errno.EBADF
    with pytest.raises(OSError):
        os.stat(stage_argument, follow_symlinks=False)
    assert stage_root is not None
    assert not stage_root.exists()
    _assert_no_stage_directories(local_root)
    assert source.read_bytes() == b"immutable payload"
    assert os.stat(source, follow_symlinks=False).st_nlink == 1


def test_stage_upload_path_recursively_snapshots_directory(local_root: Path) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "dataset"
    nested = source / "nested"
    empty = nested / "empty"
    empty.mkdir(parents=True)
    top_file = source / "top.txt"
    nested_file = nested / "payload.bin"
    top_file.write_text("top", encoding="utf-8")
    nested_file.write_bytes(b"nested")
    contract = local_data.resolve_local_path("dataset", require_exists=True)
    stage_fd = -1

    with local_data.stage_upload_path(contract) as staged:
        stage_fd = staged.directory_fd
        snapshot = Path(staged.argument)
        assert snapshot.is_dir()
        assert (snapshot / "top.txt").read_text(encoding="utf-8") == "top"
        assert (snapshot / "nested" / "payload.bin").read_bytes() == b"nested"
        assert (snapshot / "nested" / "empty").is_dir()
        assert sorted(path.name for path in snapshot.iterdir()) == ["nested", "top.txt"]
        assert (
            os.stat(snapshot, follow_symlinks=False).st_ino
            != os.stat(
                source,
                follow_symlinks=False,
            ).st_ino
        )
        for original, copied in (
            (top_file, snapshot / "top.txt"),
            (nested_file, snapshot / "nested" / "payload.bin"),
        ):
            original_stat = os.stat(original, follow_symlinks=False)
            copied_stat = os.stat(copied, follow_symlinks=False)
            assert (copied_stat.st_dev, copied_stat.st_ino) == (
                original_stat.st_dev,
                original_stat.st_ino,
            )
            assert original_stat.st_nlink == copied_stat.st_nlink == 2

    with pytest.raises(OSError) as closed:
        os.fstat(stage_fd)
    assert closed.value.errno == errno.EBADF
    _assert_no_stage_directories(local_root)
    assert os.stat(top_file, follow_symlinks=False).st_nlink == 1
    assert os.stat(nested_file, follow_symlinks=False).st_nlink == 1


def test_staging_root_skips_only_top_level_internal_stage_names(local_root: Path) -> None:
    _require_dev_fd_directory_access(local_root)
    preexisting_stage = local_root / f"{local_data._UPLOAD_STAGE_PREFIX}preexisting"
    preexisting_stage.mkdir()
    (preexisting_stage / "secret.txt").write_text("do not copy", encoding="utf-8")
    normal = local_root / "normal.txt"
    normal.write_text("copy me", encoding="utf-8")
    ordinary = local_root / "ordinary"
    ordinary.mkdir()
    nested_prefixed = ordinary / f"{local_data._UPLOAD_STAGE_PREFIX}ordinary-file"
    nested_prefixed.write_text("nested prefix is data", encoding="utf-8")
    contract = local_data.resolve_local_path(".", require_exists=True)

    with local_data.stage_upload_path(contract) as staged:
        snapshot = Path(staged.argument)
        assert (snapshot / "normal.txt").read_text(encoding="utf-8") == "copy me"
        assert (snapshot / "ordinary" / nested_prefixed.name).read_text(
            encoding="utf-8"
        ) == "nested prefix is data"
        assert not (snapshot / preexisting_stage.name).exists()
        assert all(
            not entry.name.startswith(local_data._UPLOAD_STAGE_PREFIX)
            for entry in snapshot.iterdir()
        )
        stages_during_context = _stage_directories(local_root)
        assert preexisting_stage in stages_during_context
        assert len(stages_during_context) == 2

    assert _stage_directories(local_root) == [preexisting_stage]
    assert (preexisting_stage / "secret.txt").read_text(encoding="utf-8") == "do not copy"
    assert os.stat(normal, follow_symlinks=False).st_nlink == 1
    assert os.stat(nested_prefixed, follow_symlinks=False).st_nlink == 1


def test_stage_upload_path_rejects_source_replacement_and_leaves_no_stage(
    local_root: Path,
) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "payload.bin"
    source.write_bytes(b"validated")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    retained = local_root / "validated-original.bin"
    source.rename(retained)
    source.write_bytes(b"replacement")
    assert os.stat(source, follow_symlinks=False).st_ino != contract.source_inode

    with (
        pytest.raises(ValueError, match="changed after validation"),
        local_data.stage_upload_path(contract),
    ):
        pytest.fail("a replaced source must never be yielded")

    _assert_no_stage_directories(local_root)
    assert retained.read_bytes() == b"validated"
    assert source.read_bytes() == b"replacement"
    assert os.stat(retained, follow_symlinks=False).st_nlink == 1
    assert os.stat(source, follow_symlinks=False).st_nlink == 1


def test_stage_upload_path_rejects_hard_linked_source(local_root: Path) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "payload.bin"
    source.write_bytes(b"payload")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    alias = local_root / "payload-alias.bin"
    os.link(source, alias)

    with (
        pytest.raises(ValueError, match="must not be hard-linked"),
        local_data.stage_upload_path(contract),
    ):
        pytest.fail("a hard-linked source must never be yielded")

    _assert_no_stage_directories(local_root)
    assert os.stat(source, follow_symlinks=False).st_nlink == 2
    assert os.stat(alias, follow_symlinks=False).st_nlink == 2


def test_stage_upload_path_cleans_up_when_context_body_raises(local_root: Path) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "payload.bin"
    source.write_bytes(b"payload")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    stage_fd = -1
    stage_argument = ""

    with (
        pytest.raises(RuntimeError, match="consumer failed"),
        local_data.stage_upload_path(contract) as staged,
    ):
        stage_fd = staged.directory_fd
        stage_argument = staged.argument
        assert Path(stage_argument).read_bytes() == b"payload"
        raise RuntimeError("consumer failed")

    with pytest.raises(OSError) as closed:
        os.fstat(stage_fd)
    assert closed.value.errno == errno.EBADF
    with pytest.raises(OSError):
        os.stat(stage_argument, follow_symlinks=False)
    _assert_no_stage_directories(local_root)
    assert os.stat(source, follow_symlinks=False).st_nlink == 1


def test_stage_directory_rejects_symlink_entry_and_cleans_partial_stage(
    local_root: Path,
) -> None:
    _require_dev_fd_directory_access(local_root)
    target = local_root / "target.bin"
    target.write_bytes(b"target")
    source = local_root / "dataset"
    source.mkdir()
    link = source / "link.bin"
    link.symlink_to(target)
    contract = local_data.resolve_local_path("dataset", require_exists=True)

    with (
        pytest.raises(ValueError, match="must not be a symbolic link"),
        local_data.stage_upload_path(contract),
    ):
        pytest.fail("a directory containing a symlink must never be yielded")

    _assert_no_stage_directories(local_root)
    assert link.is_symlink()
    assert target.read_bytes() == b"target"
    assert os.stat(target, follow_symlinks=False).st_nlink == 1


def test_stage_directory_rejects_special_entry_and_cleans_partial_stage(
    local_root: Path,
) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "dataset"
    source.mkdir()
    fifo = source / "input.pipe"
    os.mkfifo(fifo)
    contract = local_data.resolve_local_path("dataset", require_exists=True)

    with (
        pytest.raises(ValueError, match="must be a regular file or directory"),
        local_data.stage_upload_path(contract),
    ):
        pytest.fail("a directory containing a special file must never be yielded")

    _assert_no_stage_directories(local_root)
    assert stat.S_ISFIFO(os.stat(fifo, follow_symlinks=False).st_mode)


def test_stage_directory_rejects_hard_linked_entry_and_cleans_partial_stage(
    local_root: Path,
) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "dataset"
    source.mkdir()
    first = source / "first.bin"
    second = source / "second.bin"
    first.write_bytes(b"shared")
    os.link(first, second)
    contract = local_data.resolve_local_path("dataset", require_exists=True)

    with (
        pytest.raises(ValueError, match="not a private regular file"),
        local_data.stage_upload_path(contract),
    ):
        pytest.fail("a directory containing hard links must never be yielded")

    _assert_no_stage_directories(local_root)
    assert os.stat(first, follow_symlinks=False).st_nlink == 2
    assert os.stat(second, follow_symlinks=False).st_nlink == 2


def test_stage_directory_rejects_cross_device_entry_with_scoped_metadata_mock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "foreign.bin").write_bytes(b"foreign")

    with _directory_fd(source) as source_fd, _directory_fd(destination) as destination_fd:
        root_device = os.fstat(source_fd).st_dev
        real_stat = local_data.os.stat

        def foreign_device_stat(path: object, *args: object, **kwargs: object) -> object:
            metadata = real_stat(path, *args, **kwargs)
            if path == "foreign.bin" and kwargs.get("dir_fd") == source_fd:
                return _clone_stat(metadata, st_dev=root_device + 1)
            return metadata

        with monkeypatch.context() as scoped:
            scoped.setattr(local_data.os, "stat", foreign_device_stat)
            with pytest.raises(ValueError, match="crosses a filesystem boundary"):
                local_data._stage_directory(
                    source_fd,
                    destination_fd,
                    root_device=root_device,
                    visited={(root_device, os.fstat(source_fd).st_ino)},
                    skip_internal_stages=False,
                )

    assert list(destination.iterdir()) == []


def test_stage_directory_rejects_revisited_directory_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    child = source / "child"
    child.mkdir(parents=True)
    destination.mkdir()
    child_stat = os.stat(child, follow_symlinks=False)

    with (
        _directory_fd(source) as source_fd,
        _directory_fd(destination) as destination_fd,
        pytest.raises(ValueError, match="cycle detected"),
    ):
        local_data._stage_directory(
            source_fd,
            destination_fd,
            root_device=child_stat.st_dev,
            visited={(child_stat.st_dev, child_stat.st_ino)},
            skip_internal_stages=False,
        )

    assert list(destination.iterdir()) == []


@pytest.mark.parametrize(
    ("entry_kind", "message"),
    [("regular", "entry changed while opening"), ("directory", "directory changed while opening")],
)
def test_stage_directory_rejects_entry_replaced_while_opening_and_closes_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
    message: str,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    entry = source / "entry"
    if entry_kind == "regular":
        entry.write_bytes(b"payload")
    else:
        entry.mkdir()

    opened_entries: list[int] = []
    with _directory_fd(source) as source_fd, _directory_fd(destination) as destination_fd:
        root_device = os.fstat(source_fd).st_dev
        real_open = local_data.os.open
        real_fstat = local_data.os.fstat

        def recording_open(
            path: str | os.PathLike[str],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "entry" and dir_fd == source_fd:
                opened_entries.append(descriptor)
            return descriptor

        def changed_fstat(descriptor: int) -> object:
            metadata = real_fstat(descriptor)
            if descriptor in opened_entries:
                return _clone_stat(metadata, st_ino=metadata.st_ino + 1)
            return metadata

        with monkeypatch.context() as scoped:
            scoped.setattr(local_data.os, "open", recording_open)
            scoped.setattr(local_data.os, "fstat", changed_fstat)
            with pytest.raises(ValueError, match=message):
                local_data._stage_directory(
                    source_fd,
                    destination_fd,
                    root_device=root_device,
                    visited={(root_device, real_fstat(source_fd).st_ino)},
                    skip_internal_stages=False,
                )

        assert len(opened_entries) == 1
        with pytest.raises(OSError) as closed:
            os.fstat(opened_entries[0])
        assert closed.value.errno == errno.EBADF

    assert list(destination.iterdir()) == []


def test_link_verified_regular_removes_link_when_name_changes_during_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_directory = tmp_path / "source"
    destination_directory = tmp_path / "destination"
    source_directory.mkdir()
    destination_directory.mkdir()
    source = source_directory / "payload.bin"
    destination = destination_directory / "payload.bin"
    source.write_bytes(b"payload")

    with (
        _directory_fd(source_directory) as source_fd,
        _directory_fd(destination_directory) as destination_fd,
    ):
        source_stat = os.stat(source, follow_symlinks=False)
        real_stat = local_data.os.stat

        def changed_source_stat(path: object, *args: object, **kwargs: object) -> object:
            metadata = real_stat(path, *args, **kwargs)
            if path == source.name and kwargs.get("dir_fd") == source_fd:
                return _clone_stat(metadata, st_ino=metadata.st_ino + 1)
            return metadata

        with monkeypatch.context() as scoped:
            scoped.setattr(local_data.os, "stat", changed_source_stat)
            with pytest.raises(ValueError, match="changed while staging"):
                local_data._link_verified_regular(
                    source_fd,
                    source.name,
                    source_stat,
                    destination_fd,
                    destination.name,
                )

    assert not destination.exists()
    assert source.read_bytes() == b"payload"
    assert os.stat(source, follow_symlinks=False).st_nlink == 1


def test_stage_upload_path_rejects_invalid_staged_result_and_cleans_up(
    local_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _require_dev_fd_directory_access(local_root)
    source = local_root / "payload.bin"
    source.write_bytes(b"payload")
    contract = local_data.resolve_local_path("payload.bin", require_exists=True)
    real_stat = local_data.os.stat

    def invalid_result_stat(path: object, *args: object, **kwargs: object) -> object:
        metadata = real_stat(path, *args, **kwargs)
        if (
            isinstance(path, str)
            and path.startswith("/dev/fd/")
            and kwargs.get("follow_symlinks") is False
        ):
            return _clone_stat(metadata, st_mode=stat.S_IFIFO | 0o600)
        return metadata

    with monkeypatch.context() as scoped:
        scoped.setattr(local_data.os, "stat", invalid_result_stat)
        with (
            pytest.raises(ValueError, match="produced an invalid source"),
            local_data.stage_upload_path(contract),
        ):
            pytest.fail("an invalid staged artifact must never be yielded")

    _assert_no_stage_directories(local_root)
    assert source.read_bytes() == b"payload"
    assert os.stat(source, follow_symlinks=False).st_nlink == 1
