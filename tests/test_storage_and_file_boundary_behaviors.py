"""Boundary and race-behavior tests for storage and filesystem operations."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

from cli import files, storage
from cli.aws_client import RegionalStack
from cli.files import FileSystemInfo
from cli.storage import (
    BUCKET_DESCRIPTORS,
    BucketDescriptor,
    StorageManager,
    _ConfinementContract,
    _PinnedRoot,
    _SyncObject,
    _UploadObject,
)


def _contract(root: Path) -> _ConfinementContract:
    root = root.resolve()
    identity = root.stat()
    return _ConfinementContract(root=root, device=identity.st_dev, inode=identity.st_ino)


def _storage_manager() -> StorageManager:
    manager = object.__new__(StorageManager)
    manager.config = SimpleNamespace(
        project_name="gco",
        global_region="us-east-1",
        api_gateway_region="us-east-1",
    )
    manager._stack_resource_cache = {}
    return manager


def _file_client() -> files.FileSystemClient:
    client = object.__new__(files.FileSystemClient)
    client.config = SimpleNamespace(project_name="gco")
    client._session = MagicMock()
    client._aws_client = MagicMock()
    return client


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_pinned_root_exit_is_idempotent(tmp_path: Path) -> None:
    pinned = _PinnedRoot(_contract(tmp_path))

    pinned.__exit__(None, None, None)
    pinned.__exit__(None, None, None)

    assert pinned._fd == -1


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_pinned_root_lstat_handles_root_and_missing_parent(tmp_path: Path) -> None:
    with _PinnedRoot(_contract(tmp_path)) as pinned:
        assert stat.S_ISDIR(pinned.lstat(()).st_mode)
        assert pinned.lstat(("missing", "file")) is None


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_pinned_root_reports_directory_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _PinnedRoot(_contract(tmp_path)) as pinned:
        monkeypatch.setattr(
            storage.os,
            "open",
            Mock(side_effect=[FileNotFoundError("missing"), OSError("replaced")]),
        )
        monkeypatch.setattr(storage.os, "mkdir", Mock())

        with pytest.raises(ValueError, match="changed while creating"):
            pinned.open_directory(("child",), create=True)


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_pinned_root_opens_real_child_directory_and_rejects_linked_file(tmp_path: Path) -> None:
    (tmp_path / "child").mkdir()
    target = tmp_path / "target"
    target.write_text("data", encoding="utf-8")
    (tmp_path / "linked").symlink_to(target)

    with _PinnedRoot(_contract(tmp_path)) as pinned:
        child_fd = pinned.open_child_directory(pinned._fd, "child", tmp_path / "child")
        os.close(child_fd)
        with pytest.raises(ValueError, match="changed or is linked"):
            pinned.open_child_regular_file(pinned._fd, "linked", tmp_path / "linked")


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_confined_download_target_can_be_current(tmp_path: Path) -> None:
    target = tmp_path / "file.bin"
    target.write_bytes(b"data")
    modified = datetime.now(UTC) - timedelta(seconds=10)

    with _PinnedRoot(_contract(tmp_path)) as pinned:
        assert pinned.download_target_is_current(
            ("file.bin",),
            4,
            modified,
            evaluate_current=True,
        )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_confined_download_without_timestamp_installs_exact_bytes(tmp_path: Path) -> None:
    s3 = MagicMock()
    s3.download_fileobj.side_effect = lambda _bucket, _key, stream: stream.write(b"data")
    obj = _SyncObject(
        key="prefix/file.bin",
        destination=tmp_path / "nested" / "file.bin",
        destination_parts=("nested", "file.bin"),
        size=4,
        last_modified=None,
        current=False,
    )

    with _PinnedRoot(_contract(tmp_path)) as pinned:
        pinned.download_object(s3, "bucket", obj)

    assert obj.destination.read_bytes() == b"data"


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_confined_download_rejects_size_change_and_removes_temporary_file(tmp_path: Path) -> None:
    s3 = MagicMock()
    s3.download_fileobj.side_effect = lambda _bucket, _key, stream: stream.write(b"short")
    obj = _SyncObject(
        key="file.bin",
        destination=tmp_path / "file.bin",
        destination_parts=("file.bin",),
        size=100,
        last_modified=None,
        current=False,
    )

    with (
        _PinnedRoot(_contract(tmp_path)) as pinned,
        pytest.raises(RuntimeError, match="Downloaded size changed"),
    ):
        pinned.download_object(s3, "bucket", obj)

    assert list(tmp_path.glob(".gco-sync-*.tmp")) == []


def test_inventory_record_reports_resolution_failure() -> None:
    manager = _storage_manager()
    manager._resolve_primary_bucket = Mock(side_effect=RuntimeError("ssm denied"))
    descriptor = next(item for item in BUCKET_DESCRIPTORS if item.role == "primary")

    record = manager._s3_inventory_record(
        descriptor,
        region="us-east-1",
        stack_name="gco-global",
        account="123456789012",
        log_buckets={},
        scope_key="global",
    )

    assert record["status"] == "not-deployed"
    assert record["detail"] == "could not resolve: ssm denied"


def test_primary_bucket_resolution_handles_missing_account_and_unknown_descriptor() -> None:
    manager = _storage_manager()
    cost = next(item for item in BUCKET_DESCRIPTORS if item.id == "cost-reports")
    unknown = BucketDescriptor(
        id="unknown",
        role="primary",
        scope="global",
        purpose="test",
        pod_access="none",
        discovery="none",
        removal_policy="destroy",
        logical_id_prefix="Unknown",
    )

    assert manager._resolve_primary_bucket(cost, "us-east-1", None) == (None, None)
    assert manager._resolve_primary_bucket(unknown, "us-east-1", "123456789012") == (
        None,
        None,
    )


def test_stack_bucket_resource_sweep_skips_missing_physical_and_unknown_logical_ids() -> None:
    manager = _storage_manager()
    cfn = MagicMock()
    cfn.list_stack_resources.return_value = {
        "StackResourceSummaries": [
            {
                "ResourceType": "AWS::S3::Bucket",
                "LogicalResourceId": "MissingPhysical",
                "PhysicalResourceId": None,
            },
            {
                "ResourceType": "AWS::S3::Bucket",
                "LogicalResourceId": "UnrelatedBucketABC",
                "PhysicalResourceId": "physical-bucket",
            },
        ]
    }

    with patch("cli.storage.boto3.client", return_value=cfn):
        assert manager._stack_bucket_resources("gco-global", "us-east-1") == {}


def test_storage_account_and_partition_resolution_degrade_on_sdk_failure() -> None:
    manager = _storage_manager()
    with patch("cli.storage.boto3.client", side_effect=RuntimeError("no credentials")):
        assert manager._account_id() is None
    with patch("cli.storage.boto3.Session", side_effect=RuntimeError("metadata missing")):
        assert manager._partition_for("us-test-1") == "aws"


def test_nonconfined_download_without_timestamp_skips_utime(tmp_path: Path) -> None:
    manager = _storage_manager()
    destination = tmp_path / "downloads"
    obj = _SyncObject(
        key="prefix/file.bin",
        destination=destination / "file.bin",
        destination_parts=None,
        size=4,
        last_modified=None,
        current=False,
    )
    manager._build_sync_plan = Mock(return_value=([obj], 0))
    s3 = MagicMock()
    s3.download_file.side_effect = lambda _bucket, _key, path: Path(path).write_bytes(b"data")

    with patch("cli.storage.os.utime") as utime:
        result = manager._sync_download(
            {"alias": "cluster-shared", "bucket": "bucket", "region": "us-east-1"},
            s3,
            destination,
            "prefix/",
            dry_run=False,
            force=False,
        )

    assert result["files_downloaded"] == 1
    utime.assert_not_called()


def test_confined_skipped_download_is_revalidated(tmp_path: Path) -> None:
    manager = _storage_manager()
    obj = _SyncObject(
        key="file.bin",
        destination=tmp_path / "file.bin",
        destination_parts=("file.bin",),
        size=4,
        last_modified=None,
        current=True,
    )
    manager._build_sync_plan = Mock(return_value=([obj], 0))
    confinement = MagicMock()
    confinement.download_target_is_current.return_value = True

    result = manager._sync_download(
        {"alias": "cluster-shared", "bucket": "bucket", "region": "us-east-1"},
        MagicMock(),
        tmp_path,
        "",
        dry_run=False,
        force=False,
        confinement=confinement,
    )

    assert result["files_skipped"] == 1
    confinement.download_target_is_current.assert_called_once_with(
        ("file.bin",),
        4,
        None,
        evaluate_current=True,
    )


def test_upload_detects_source_change_between_plan_and_open(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"data")
    signature = (1, 2, 4, 5)
    obj = _UploadObject(
        source=source,
        source_parts=None,
        key="source.bin",
        size=4,
        sha256="0" * 64,
        signature=signature,
        current=False,
    )
    manager = _storage_manager()
    manager._build_upload_plan = Mock(return_value=([obj], 0))
    manager._open_upload_source = Mock(side_effect=lambda *_args: os.open(source, os.O_RDONLY))
    manager._stat_signature = Mock(return_value=(9, 9, 9, 9))

    with pytest.raises(RuntimeError, match="changed after planning"):
        manager._sync_upload(
            {"alias": "cluster-shared", "bucket": "bucket", "region": "us-east-1"},
            MagicMock(),
            source,
            "",
            dry_run=False,
            force=False,
        )


def test_upload_file_collection_propagates_walk_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def walk(_source: Path, *, onerror, **_kwargs):
        onerror(PermissionError("denied"))
        return iter(())

    monkeypatch.setattr(storage.os, "walk", walk)

    with pytest.raises(PermissionError, match="denied"):
        StorageManager._collect_upload_files(tmp_path)


def test_upload_file_collection_rejects_descendant_directory_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "linked").symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="contains a symbolic link"):
        StorageManager._collect_upload_files(tmp_path)


@pytest.mark.parametrize(
    ("stat_effect", "message"),
    [
        (FileNotFoundError("gone"), "changed during enumeration"),
        (SimpleNamespace(st_mode=stat.S_IFLNK), "contains a symbolic link"),
        (SimpleNamespace(st_mode=stat.S_IFIFO), "contains a non-regular file"),
    ],
)
def test_confined_upload_walk_rejects_raced_or_unsafe_children(
    monkeypatch: pytest.MonkeyPatch,
    stat_effect: object,
    message: str,
) -> None:
    confinement = MagicMock()
    confinement.display_path.return_value = Path("/root/child")
    monkeypatch.setattr(storage.os, "listdir", lambda _fd: ["child"])
    if isinstance(stat_effect, Exception):
        monkeypatch.setattr(storage.os, "stat", Mock(side_effect=stat_effect))
    else:
        monkeypatch.setattr(storage.os, "stat", Mock(return_value=stat_effect))

    with pytest.raises((RuntimeError, ValueError), match=message):
        StorageManager._walk_confined_upload_directory(
            confinement,
            7,
            (),
            (),
            [],
        )


def test_open_upload_source_rejects_nonregular_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj = _UploadObject(
        source=Path("/source"),
        source_parts=None,
        key="source",
        size=0,
        sha256="0" * 64,
        signature=(1, 2, 3, 4),
        current=False,
    )
    monkeypatch.setattr(storage.os, "open", lambda *_args, **_kwargs: 9)
    monkeypatch.setattr(storage.os, "fstat", lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    close = Mock()
    monkeypatch.setattr(storage.os, "close", close)

    with pytest.raises(ValueError, match="not a regular file"):
        StorageManager._open_upload_source(obj, None)
    close.assert_called_once_with(9)


def test_verify_upload_source_signature_rejects_replaced_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    obj = _UploadObject(
        source=Path("/source"),
        source_parts=None,
        key="source",
        size=4,
        sha256="0" * 64,
        signature=(1, 2, 3, 4),
        current=False,
    )
    monkeypatch.setattr(StorageManager, "_open_upload_source", lambda *_args: 9)
    monkeypatch.setattr(StorageManager, "_stat_signature", lambda _stat: (9, 9, 9, 9))
    monkeypatch.setattr(storage.os, "fstat", lambda _fd: object())
    monkeypatch.setattr(storage.os, "close", Mock())

    with pytest.raises(RuntimeError, match="changed after planning"):
        StorageManager._verify_upload_source_signature(obj, None, skipped=False)


@pytest.mark.parametrize(
    ("value", "field", "allow_subdomains", "message"),
    [
        ("Bad_Name", "namespace", False, "contains characters"),
        ("", "path", False, "non-empty path"),
        ("bad\npath", "path", False, "control characters"),
    ],
)
def test_file_validators_reject_invalid_names_and_paths(
    value: str,
    field: str,
    allow_subdomains: bool,
    message: str,
) -> None:
    if field == "namespace":
        with pytest.raises(ValueError, match=message):
            files._validated_kubernetes_name(value, field, allow_subdomains=allow_subdomains)
    else:
        with pytest.raises(ValueError, match=message):
            files._validated_copy_path(value, field)


def test_file_system_discovery_skips_absent_ids_and_unresolved_details() -> None:
    client = _file_client()
    client._aws_client.discover_regional_stacks.return_value = {
        "us-east-1": RegionalStack(
            region="us-east-1",
            stack_name="gco-us-east-1",
            cluster_name="gco-us-east-1",
            status="CREATE_COMPLETE",
        ),
        "us-west-2": RegionalStack(
            region="us-west-2",
            stack_name="gco-us-west-2",
            cluster_name="gco-us-west-2",
            status="CREATE_COMPLETE",
            efs_file_system_id="fs-efs",
            fsx_file_system_id="fs-fsx",
        ),
    }
    client._get_efs_info = Mock(return_value=None)
    client._get_fsx_info = Mock(return_value=None)

    assert client.get_file_systems() == []


def test_file_system_lookup_continues_past_wrong_type() -> None:
    client = _file_client()
    client.get_file_systems = Mock(
        return_value=[
            FileSystemInfo(
                file_system_id="fs-fsx",
                file_system_type="fsx",
                region="us-east-1",
                dns_name="fsx.example",
            )
        ]
    )

    assert client.get_file_system_by_region("us-east-1", "efs") is None


def test_datasync_lookup_continues_past_wrong_file_system_id() -> None:
    client = _file_client()
    client.get_file_systems = Mock(
        return_value=[
            FileSystemInfo(
                file_system_id="fs-other",
                file_system_type="efs",
                region="us-east-1",
                dns_name="efs.example",
            )
        ]
    )

    with pytest.raises(ValueError, match="fs-target not found"):
        client.create_datasync_download_task(
            "fs-target",
            "us-east-1",
            "/data",
            "destination",
        )


def test_access_point_client_error_returns_empty_list() -> None:
    client = _file_client()
    efs = MagicMock()
    efs.describe_access_points.side_effect = _client_error("AccessDeniedException")
    client._session.client.return_value = efs

    assert client.get_access_point_info("fs-123", "us-east-1") == []


def _client_error(code: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


@pytest.mark.parametrize("method", ["list_storage_contents", "download_from_storage"])
def test_storage_helper_methods_reject_invalid_type(method: str) -> None:
    client = _file_client()
    kwargs = {"region": "us-east-1", "storage_type": "invalid"}
    if method == "download_from_storage":
        kwargs.update(remote_path="data", local_path="output")

    with pytest.raises(ValueError, match="storage_type"):
        getattr(client, method)(**kwargs)


def test_storage_listing_explicit_pvc_skips_default_and_ignores_short_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_client()
    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object):
        commands.append(command)
        if command[:3] == ["kubectl", "get", "pod"]:
            return SimpleNamespace(returncode=0, stdout="Running", stderr="")
        if command[:2] == ["kubectl", "exec"]:
            return SimpleNamespace(
                returncode=0,
                stdout="short\n-rw-r--r-- 1 root root 4 Jan 1 00:00 file.txt\n",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    result = client.list_storage_contents(
        "us-east-1",
        remote_path="data",
        pvc_name="custom-pvc",
    )

    assert result["status"] == "success"
    assert [entry["name"] for entry in result["contents"]] == ["file.txt"]
    apply = next(command for command in commands if command[:2] == ["kubectl", "apply"])
    assert apply


def test_storage_listing_times_out_when_helper_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_client()
    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)
    monkeypatch.setattr(
        files.time if hasattr(files, "time") else __import__("time"), "sleep", lambda _seconds: None
    )

    def run(command: list[str], **_kwargs: object):
        if command[:3] == ["kubectl", "get", "pod"]:
            return SimpleNamespace(returncode=0, stdout="Pending", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="did not become ready"):
        client.list_storage_contents("us-east-1")


def test_storage_listing_translates_missing_kubectl(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _file_client()
    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError("kubectl")))

    with pytest.raises(RuntimeError, match="kubectl not found"):
        client.list_storage_contents("us-east-1")


def test_storage_download_translates_missing_kubectl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_client()
    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError("kubectl")))

    with pytest.raises(RuntimeError, match="kubectl not found"):
        client.download_from_storage(
            "us-east-1",
            "data",
            str(tmp_path / "output"),
        )


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement is POSIX-only")
def test_confined_download_retries_one_temporary_name_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collision = tmp_path / f".gco-sync-{os.getpid()}-collision.tmp"
    collision.write_bytes(b"occupied")
    tokens = iter(("collision", "available"))
    monkeypatch.setattr(storage.secrets, "token_hex", lambda _size: next(tokens))
    s3 = MagicMock()
    s3.download_fileobj.side_effect = lambda _bucket, _key, stream: stream.write(b"data")
    obj = _SyncObject(
        key="file.bin",
        destination=tmp_path / "file.bin",
        destination_parts=("file.bin",),
        size=4,
        last_modified=None,
        current=False,
    )

    with _PinnedRoot(_contract(tmp_path)) as pinned:
        pinned.download_object(s3, "bucket", obj)

    assert obj.destination.read_bytes() == b"data"
    assert collision.read_bytes() == b"occupied"


def test_storage_account_resolution_returns_string_identity() -> None:
    manager = _storage_manager()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": 123456789012}
    with patch("cli.storage.boto3.client", return_value=sts):
        assert manager._account_id() == "123456789012"


def test_confined_upload_walk_recurses_into_child_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    confinement = MagicMock()
    confinement.display_path.return_value = Path("/root/child")
    confinement.open_child_directory.return_value = 8
    listings = iter((["child"], []))
    monkeypatch.setattr(storage.os, "listdir", lambda _fd: next(listings))
    monkeypatch.setattr(
        storage.os,
        "stat",
        lambda *_args, **_kwargs: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    close = Mock()
    monkeypatch.setattr(storage.os, "close", close)

    prepared: list[object] = []
    StorageManager._walk_confined_upload_directory(
        confinement,
        7,
        (),
        (),
        prepared,
    )

    assert prepared == []
    confinement.open_child_directory.assert_called_once()
    close.assert_called_once_with(8)


def test_kubernetes_name_validator_rejects_empty_value() -> None:
    with pytest.raises(ValueError, match="non-empty Kubernetes DNS name"):
        files._validated_kubernetes_name("", "namespace", allow_subdomains=False)


def test_storage_download_directory_sums_nested_file_sizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _file_client()
    destination = tmp_path / "download"
    monkeypatch.setattr(files, "update_kubeconfig", lambda *_args: None)

    def run(command: list[str], **_kwargs: object):
        stdout = "Running" if command[:3] == ["kubectl", "get", "pod"] else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    real_isdir = os.path.isdir
    monkeypatch.setattr(os.path, "isfile", lambda _path: False)
    monkeypatch.setattr(
        os.path,
        "isdir",
        lambda path: str(path) == str(destination) or real_isdir(path),
    )
    monkeypatch.setattr(os, "walk", lambda _path: [(str(destination), [], ["a", "b"])])
    monkeypatch.setattr(os.path, "getsize", lambda path: 2 if str(path).endswith("a") else 3)

    result = client.download_from_storage(
        "us-east-1",
        "data",
        str(destination),
    )

    assert result["status"] == "success"
    assert result["size_bytes"] == 5
