"""Behavioral tests for GCO S3 bucket discovery and safe storage sync."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from cli.config import GCOConfig
from cli.storage import (
    StorageBucketNotFoundError,
    StorageManager,
    _ConfinementContract,
    _PinnedRoot,
    _SyncObject,
    get_storage_manager,
)

NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
BUCKET = {
    "alias": "cluster-shared",
    "bucket": "physical-bucket",
    "region": "us-east-2",
    "scope": "global",
    "purpose": "shared",
    "s3_uri": "s3://physical-bucket/",
}


def _client_error(code: str, status: int = 400, message: str = "failure") -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "HeadObject",
    )


@dataclass
class _FakeObject:
    data: bytes
    modified: datetime = NOW
    metadata: dict[str, str] = field(default_factory=dict)


class _FakePaginator:
    def __init__(self, client: FakeS3) -> None:
        self.client = client

    def paginate(self, *, Bucket: str, Prefix: str):
        assert Bucket == BUCKET["bucket"]
        self.client.list_calls.append((Bucket, Prefix))
        entries = [
            {
                "Key": key,
                "Size": len(obj.data),
                "LastModified": obj.modified,
            }
            for key, obj in sorted(self.client.objects.items())
            if key.startswith(Prefix)
        ]
        if not entries:
            yield {}
            return
        for offset in range(0, len(entries), 2):
            yield {"Contents": entries[offset : offset + 2]}


class FakeS3:
    """Small in-memory S3 implementation that records the APIs sync uses."""

    def __init__(self) -> None:
        self.objects: dict[str, _FakeObject] = {}
        self.list_calls: list[tuple[str, str]] = []
        self.head_calls: list[tuple[str, str]] = []
        self.download_calls: list[str] = []
        self.upload_calls: list[tuple[str, dict[str, Any]]] = []
        self.head_errors: dict[str, ClientError] = {}
        self.on_upload: Any = None

    def put(
        self,
        key: str,
        data: bytes,
        *,
        modified: datetime = NOW,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.objects[key] = _FakeObject(data, modified, metadata or {})

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_objects_v2"
        return _FakePaginator(self)

    def download_file(self, bucket: str, key: str, destination: str) -> None:
        assert bucket == BUCKET["bucket"]
        self.download_calls.append(key)
        Path(destination).write_bytes(self.objects[key].data)

    def download_fileobj(self, bucket: str, key: str, destination: Any) -> None:
        assert bucket == BUCKET["bucket"]
        self.download_calls.append(key)
        destination.write(self.objects[key].data)

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET["bucket"]
        self.head_calls.append((Bucket, Key))
        if Key in self.head_errors:
            raise self.head_errors[Key]
        if Key not in self.objects:
            raise _client_error("404", 404, "Not Found")
        obj = self.objects[Key]
        return {"ContentLength": len(obj.data), "Metadata": dict(obj.metadata)}

    def upload_fileobj(
        self,
        source: Any,
        bucket: str,
        key: str,
        *,
        ExtraArgs: dict[str, Any],
    ) -> None:
        assert bucket == BUCKET["bucket"]
        data = source.read()
        self.upload_calls.append((key, ExtraArgs))
        self.objects[key] = _FakeObject(data, NOW, dict(ExtraArgs["Metadata"]))
        if self.on_upload is not None:
            self.on_upload(key)


@pytest.fixture
def config() -> GCOConfig:
    return GCOConfig(
        project_name="demo",
        default_region="us-east-1",
        global_region="us-east-2",
        api_gateway_region="us-west-2",
    )


@pytest.fixture
def manager(config: GCOConfig) -> StorageManager:
    value = StorageManager(config)
    value.resolve_bucket = MagicMock(return_value=dict(BUCKET))  # type: ignore[method-assign]
    return value


def _sync_with(manager: StorageManager, s3: FakeS3, *args: Any, **kwargs: Any) -> dict[str, Any]:
    with patch("cli.storage.boto3.client", return_value=s3):
        return manager.sync("cluster-shared", *args, **kwargs)


class TestBucketDiscovery:
    def test_get_storage_manager_uses_supplied_config(self, config: GCOConfig) -> None:
        assert get_storage_manager(config).config is config

    def test_resolve_ssm_backed_aliases(self, config: GCOConfig) -> None:
        manager = StorageManager(config)

        def parameter(name: str, *, region: str) -> str | None:
            values = {
                ("/demo/cluster-shared-bucket/name", "us-east-2"): "cluster-bucket",
                ("/demo/cluster-shared-bucket/region", "us-east-2"): "eu-west-1",
                ("/demo/model-bucket-name", "us-east-2"): "models-bucket",
                ("/demo/regional-shared-bucket/name", "ap-south-1"): "regional-bucket",
                ("/demo/regional-shared-bucket/region", "ap-south-1"): "ap-south-1",
            }
            return values.get((name, region))

        with patch("gco.services.aws_ssm.get_ssm_parameter_optional", side_effect=parameter):
            cluster = manager.resolve_bucket("  CLUSTER-SHARED ")
            models = manager.resolve_bucket("model-weights")
            regional = manager.resolve_bucket("regional-shared:ap-south-1")

        assert cluster == {
            "alias": "cluster-shared",
            "bucket": "cluster-bucket",
            "region": "eu-west-1",
            "scope": "global",
            "purpose": "Cross-region cluster job artifacts and shared data",
            "s3_uri": "s3://cluster-bucket/",
        }
        assert models["bucket"] == "models-bucket"
        assert models["region"] == "us-east-2"
        assert regional["alias"] == "regional-shared:ap-south-1"
        assert regional["scope"] == "regional"

    @pytest.mark.parametrize(
        ("alias", "message"),
        [
            ("cluster-shared", "Deploy the global stack"),
            ("model-weights", "Deploy the global stack"),
            ("regional-shared:us-east-1", "Deploy that region's stack"),
        ],
    )
    def test_missing_ssm_bucket_is_reported(
        self,
        config: GCOConfig,
        alias: str,
        message: str,
    ) -> None:
        manager = StorageManager(config)
        with (
            patch("gco.services.aws_ssm.get_ssm_parameter_optional", return_value=None),
            pytest.raises(StorageBucketNotFoundError, match=message),
        ):
            manager.resolve_bucket(alias)

    def test_regional_alias_validation_and_inference(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        manager._resolve_regional_shared = MagicMock(return_value={"bucket": "one"})  # type: ignore[method-assign]
        manager._configured_regional_regions = MagicMock(return_value=["eu-north-1"])  # type: ignore[method-assign]

        assert manager.resolve_bucket("regional-shared") == {"bucket": "one"}
        manager._resolve_regional_shared.assert_called_once_with("eu-north-1")

        with pytest.raises(ValueError, match="must include a region"):
            manager.resolve_bucket("regional-shared:")
        with pytest.raises(ValueError, match="conflicts"):
            manager.resolve_bucket("regional-shared:us-east-1", region="us-west-2")
        with pytest.raises(ValueError, match="only valid"):
            manager.resolve_bucket("cluster-shared", region="us-east-1")
        with pytest.raises(ValueError, match="Unknown bucket alias"):
            manager.resolve_bucket("access-logs")

        manager._configured_regional_regions.return_value = ["us-east-1", "us-west-2"]
        with pytest.raises(ValueError, match="ambiguous"):
            manager.resolve_bucket("regional-shared")

    def test_configured_regions_deduplicate_and_fallback(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        with patch(
            "cli.config._load_cdk_json",
            return_value={"regional": ["us-west-2", "us-west-2", 7, "", "eu-west-1"]},
        ):
            assert manager._configured_regional_regions() == ["us-west-2", "eu-west-1"]
        with patch("cli.config._load_cdk_json", return_value={"regional": "invalid"}):
            assert manager._configured_regional_regions() == ["us-east-1"]

    def test_analytics_discovery_paginates(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        cfn = MagicMock()
        cfn.list_stack_resources.side_effect = [
            {
                "StackResourceSummaries": [
                    {"LogicalResourceId": "Other", "ResourceType": "AWS::S3::Bucket"}
                ],
                "NextToken": "next",
            },
            {
                "StackResourceSummaries": [
                    {
                        "LogicalResourceId": "StudioOnlyBucketABC",
                        "ResourceType": "AWS::S3::Bucket",
                        "PhysicalResourceId": "studio-bucket",
                    }
                ]
            },
        ]
        with patch("cli.storage.boto3.client", return_value=cfn):
            result = manager.resolve_bucket("analytics-studio")
        assert result["bucket"] == "studio-bucket"
        assert result["region"] == "us-west-2"
        assert cfn.list_stack_resources.call_args_list[1].kwargs["NextToken"] == "next"

    def test_analytics_missing_stack_and_missing_resource(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        cfn = MagicMock()
        cfn.list_stack_resources.side_effect = ClientError(
            {"Error": {"Code": "ValidationError", "Message": "Stack does not exist"}},
            "ListStackResources",
        )
        with (
            patch("cli.storage.boto3.client", return_value=cfn),
            pytest.raises(StorageBucketNotFoundError, match="Deploy the analytics stack"),
        ):
            manager.resolve_bucket("analytics-studio")

        cfn.list_stack_resources.side_effect = None
        cfn.list_stack_resources.return_value = {
            "StackResourceSummaries": [
                {
                    "LogicalResourceId": "StudioOnlyBucket",
                    "ResourceType": "AWS::S3::Bucket",
                    "PhysicalResourceId": None,
                }
            ],
            "NextToken": 123,
        }
        with (
            patch("cli.storage.boto3.client", return_value=cfn),
            pytest.raises(StorageBucketNotFoundError, match="deployed analytics stack"),
        ):
            manager.resolve_bucket("analytics-studio")

    def test_analytics_propagates_non_missing_error(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        cfn = MagicMock()
        error = _client_error("AccessDenied", 403)
        cfn.list_stack_resources.side_effect = error
        with patch("cli.storage.boto3.client", return_value=cfn), pytest.raises(ClientError):
            manager.resolve_bucket("analytics-studio")

    def test_list_buckets_omits_only_not_deployed(self, config: GCOConfig) -> None:
        manager = StorageManager(config)
        manager._configured_regional_regions = MagicMock(return_value=["us-east-1", "us-west-2"])  # type: ignore[method-assign]

        def resolve(alias: str, region: str | None = None) -> dict[str, str]:
            if alias == "model-weights" or region == "us-west-2":
                raise StorageBucketNotFoundError("not deployed")
            return {"alias": f"{alias}:{region}" if region else alias}

        manager.resolve_bucket = MagicMock(side_effect=resolve)  # type: ignore[method-assign]
        assert manager.list_buckets() == [
            {"alias": "cluster-shared"},
            {"alias": "regional-shared:us-east-1"},
            {"alias": "analytics-studio"},
        ]
        manager.resolve_bucket.reset_mock()
        manager.list_buckets(region="eu-west-1")
        manager.resolve_bucket.assert_any_call("regional-shared", region="eu-west-1")


class TestDownloadSync:
    def test_download_pagination_incrementality_force_and_no_delete(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        s3 = FakeS3()
        s3.put("datasets/", b"")
        s3.put("datasets/a.txt", b"alpha")
        s3.put("datasets/nested/b.bin", b"bravo")
        s3.put("other/ignored", b"ignored")
        destination = tmp_path / "download"
        destination.mkdir()
        local_only = destination / "local-only.txt"
        local_only.write_text("keep", encoding="utf-8")

        result = _sync_with(manager, s3, str(destination), prefix="/datasets")

        assert result["direction"] == "download"
        assert result["objects_scanned"] == 3
        assert result["directory_markers"] == 1
        assert result["files_downloaded"] == 2
        assert result["bytes_downloaded"] == 10
        assert (destination / "a.txt").read_bytes() == b"alpha"
        assert (destination / "nested/b.bin").read_bytes() == b"bravo"
        assert int((destination / "a.txt").stat().st_mtime) == int(NOW.timestamp())
        assert local_only.read_text(encoding="utf-8") == "keep"
        assert s3.list_calls == [(BUCKET["bucket"], "datasets/")]

        s3.download_calls.clear()
        repeated = _sync_with(manager, s3, str(destination), prefix="datasets/")
        assert repeated["files_downloaded"] == 0
        assert repeated["files_skipped"] == 2
        assert s3.download_calls == []

        forced = _sync_with(manager, s3, str(destination), prefix="datasets", force=True)
        assert forced["files_downloaded"] == 2
        assert forced["files_skipped"] == 0

        absent = tmp_path / "dry-run"
        dry_run = _sync_with(manager, s3, str(absent), prefix="datasets", dry_run=True)
        assert dry_run["files_planned"] == 2
        assert dry_run["files_downloaded"] == 0
        assert not absent.exists()

    def test_download_revalidates_skipped_file(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        s3 = FakeS3()
        s3.put("stable.txt", b"same")
        destination = tmp_path / "download"
        destination.mkdir()
        target = destination / "stable.txt"
        target.write_bytes(b"same")
        future = NOW + timedelta(hours=1)
        os.utime(target, (future.timestamp(), future.timestamp()))

        original = manager._build_sync_plan

        def mutate_after_plan(*args: Any, **kwargs: Any):
            result = original(*args, **kwargs)
            target.write_bytes(b"changed")
            return result

        manager._build_sync_plan = mutate_after_plan  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="skipped local file changed"):
            _sync_with(manager, s3, str(destination))

    def test_download_wraps_transfer_failure(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        s3 = FakeS3()
        s3.put("file.txt", b"data")
        s3.download_file = MagicMock(side_effect=OSError("disk full"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="failed to download"):
            _sync_with(manager, s3, str(tmp_path / "out"))

    def test_download_destination_must_be_directory(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        target = tmp_path / "file"
        target.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError, match="not a directory"):
            _sync_with(manager, FakeS3(), str(target))

    @pytest.mark.parametrize(
        ("key", "data", "message"),
        [
            ("../escape", b"x", "Unsafe S3 object key"),
            ("bad\\name", b"x", "Unsafe S3 object key"),
            ("folder/", b"not-empty", "cannot be represented"),
            ("", b"x", "empty key"),
        ],
    )
    def test_download_rejects_unsafe_keys(
        self,
        manager: StorageManager,
        tmp_path: Path,
        key: str,
        data: bytes,
        message: str,
    ) -> None:
        s3 = FakeS3()
        s3.put(key, data)
        with pytest.raises(ValueError, match=message):
            _sync_with(manager, s3, str(tmp_path / "out"), dry_run=True)

    def test_download_rejects_collisions(self, tmp_path: Path) -> None:
        first = _SyncObject("a", tmp_path / "a", None, 1, NOW, False)
        child = _SyncObject("a/b", tmp_path / "a/b", None, 1, NOW, False)
        with pytest.raises(ValueError, match="file/directory collision"):
            StorageManager._validate_sync_plan([first, child])

        duplicate = _SyncObject("other", tmp_path / "a", None, 1, NOW, False)
        with pytest.raises(ValueError, match="same local path"):
            StorageManager._validate_sync_plan([first, duplicate])

    def test_safe_local_path_rejects_escape_and_filesystem_conflicts(self, tmp_path: Path) -> None:
        destination = tmp_path / "destination"
        destination.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (destination / "link").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ValueError, match="escapes"):
            StorageManager._safe_local_path(destination.resolve(), "link/file", "link/file")

        parent_file = destination / "parent"
        parent_file.write_text("x", encoding="utf-8")
        with pytest.raises(NotADirectoryError, match="parent"):
            StorageManager._safe_local_path(destination.resolve(), "parent/file", "parent/file")

        existing_dir = destination / "folder"
        existing_dir.mkdir()
        with pytest.raises(IsADirectoryError, match="existing directory"):
            StorageManager._safe_local_path(destination.resolve(), "folder", "folder")

    def test_download_name_validation_for_windows(self) -> None:
        with patch("cli.storage.sys.platform", "win32"):
            for key in ("bad. ", "a<b", "CON", "com1.txt", "LPT¹.log"):
                with pytest.raises(ValueError, match="Windows|Reserved"):
                    StorageManager._download_relative_parts(key, key)
            assert StorageManager._download_relative_parts("safe/name.txt", "safe/name.txt") == (
                "safe",
                "name.txt",
            )

    def test_collision_normalization_platforms(self) -> None:
        with patch("cli.storage.sys.platform", "win32"):
            assert StorageManager._local_collision_parts(Path("Dir/File")) == ("dir", "file")
        with patch("cli.storage.sys.platform", "darwin"):
            assert StorageManager._local_collision_parts(Path("É")) == ("é",)
        with patch("cli.storage.sys.platform", "linux"):
            assert StorageManager._local_collision_parts(Path("Case")) == ("Case",)


class TestUploadSync:
    def test_upload_directory_dry_run_incrementality_force_and_checksums(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        (source / "nested").mkdir(parents=True)
        (source / "a.txt").write_bytes(b"alpha")
        (source / "nested/b.bin").write_bytes(b"bravo")
        s3 = FakeS3()
        s3.put("uploads/remote-only.txt", b"preserve")

        dry_run = _sync_with(
            manager,
            s3,
            str(source),
            direction="UPLOAD",
            prefix="/uploads",
            dry_run=True,
        )
        assert dry_run["files_planned"] == 2
        assert dry_run["files_uploaded"] == 0
        assert dry_run["objects_probed"] == 2
        assert s3.upload_calls == []

        uploaded = _sync_with(manager, s3, str(source), direction="upload", prefix="uploads")
        assert uploaded["files_uploaded"] == 2
        assert uploaded["bytes_uploaded"] == 10
        assert s3.objects["uploads/a.txt"].data == b"alpha"
        assert s3.objects["uploads/nested/b.bin"].data == b"bravo"
        assert s3.objects["uploads/remote-only.txt"].data == b"preserve"
        for key, extra_args in s3.upload_calls:
            digest = hashlib.sha256(s3.objects[key].data).hexdigest()
            assert extra_args["Metadata"] == {"gco-sync-sha256": digest}
            assert extra_args["ChecksumSHA256"] == base64.b64encode(bytes.fromhex(digest)).decode(
                "ascii"
            )

        s3.upload_calls.clear()
        repeated = _sync_with(manager, s3, str(source), direction="upload", prefix="uploads")
        assert repeated["files_uploaded"] == 0
        assert repeated["files_skipped"] == 2
        assert repeated["objects_probed"] == 4
        assert s3.upload_calls == []

        head_count = len(s3.head_calls)
        forced = _sync_with(
            manager,
            s3,
            str(source),
            direction="upload",
            prefix="uploads",
            force=True,
        )
        assert forced["files_uploaded"] == 2
        assert forced["objects_probed"] == 0
        assert len(s3.head_calls) == head_count

    def test_upload_single_file_mapping_and_missing_metadata(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "weights.bin"
        source.write_bytes(b"weights")
        s3 = FakeS3()
        s3.put("models/weights.bin", b"weights", metadata={})

        result = _sync_with(manager, s3, str(source), direction="upload", prefix="models")
        assert result["files_scanned"] == 1
        assert result["files_uploaded"] == 1
        assert result["destination"] == f"s3://{BUCKET['bucket']}/models/"

        digest = hashlib.sha256(b"weights").hexdigest()
        s3.objects["models/weights.bin"].metadata = {"GCO-SYNC-SHA256": digest.upper()}
        repeated = _sync_with(manager, s3, str(source), direction="upload", prefix="models")
        assert repeated["files_skipped"] == 1

    def test_remote_digest_comparison_variants(self, manager: StorageManager) -> None:
        s3 = FakeS3()
        digest = hashlib.sha256(b"abc").hexdigest()
        s3.put("key", b"abc", metadata={"gco-sync-sha256": digest})
        assert manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 3, digest)
        assert not manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 4, digest)

        s3.objects["key"].metadata = {}  # type: ignore[assignment]
        assert not manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 3, digest)
        s3.head_object = MagicMock(return_value={"ContentLength": 3, "Metadata": "bad"})  # type: ignore[method-assign]
        assert not manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 3, digest)

        for code, status in (("AccessDenied", 403), ("NoSuchKey", 404), ("NotFound", 404)):
            s3.head_object = MagicMock(side_effect=_client_error(code, status))  # type: ignore[method-assign]
            assert not manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 3, digest)

        s3.head_object = MagicMock(side_effect=_client_error("SlowDown", 503))  # type: ignore[method-assign]
        with pytest.raises(ClientError):
            manager._remote_digest_matches(s3, BUCKET["bucket"], "key", 3, digest)

    def test_upload_detects_source_mutation(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source.txt"
        source.write_bytes(b"before")
        s3 = FakeS3()
        s3.on_upload = lambda _key: source.write_bytes(b"after!")
        with pytest.raises(RuntimeError, match="changed during upload"):
            _sync_with(manager, s3, str(source), direction="upload")

    def test_upload_detects_path_replacement_after_transfer(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source.txt"
        source.write_bytes(b"original")
        moved = tmp_path / "moved.txt"
        s3 = FakeS3()

        def replace(_key: str) -> None:
            source.rename(moved)
            source.write_bytes(b"replaced")

        s3.on_upload = replace
        with pytest.raises(RuntimeError, match="changed during upload"):
            _sync_with(manager, s3, str(source), direction="upload")

    def test_upload_wraps_transfer_error(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source.txt"
        source.write_bytes(b"data")
        s3 = FakeS3()
        s3.upload_fileobj = MagicMock(side_effect=OSError("network"))  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="failed to upload"):
            _sync_with(manager, s3, str(source), direction="upload")

    def test_upload_revalidates_skipped_remote_and_local(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source.txt"
        source.write_bytes(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        s3 = FakeS3()
        s3.put("source.txt", b"same", metadata={"gco-sync-sha256": digest})
        original = manager._build_upload_plan

        def change_remote_after_plan(*args: Any, **kwargs: Any):
            result = original(*args, **kwargs)
            s3.objects["source.txt"].metadata = {"gco-sync-sha256": "changed"}
            return result

        manager._build_upload_plan = change_remote_after_plan  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="skipped S3 object changed"):
            _sync_with(manager, s3, str(source), direction="upload")

    def test_upload_source_validation(self, manager: StorageManager, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            _sync_with(manager, FakeS3(), str(tmp_path / "missing"), direction="upload")

        real = tmp_path / "real"
        real.write_text("data", encoding="utf-8")
        linked = tmp_path / "linked"
        linked.symlink_to(real)
        with pytest.raises(ValueError, match="symbolic link"):
            _sync_with(manager, FakeS3(), str(linked), direction="upload")

        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular file or directory"):
            _sync_with(manager, FakeS3(), str(fifo), direction="upload")

        directory = tmp_path / "directory"
        directory.mkdir()
        (directory / "child-link").symlink_to(real)
        with pytest.raises(ValueError, match="contains a symbolic link"):
            _sync_with(manager, FakeS3(), str(directory), direction="upload")

    def test_upload_rejects_unrepresentable_relative_name(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        source.mkdir()
        (source / "bad\\name").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="represented safely"):
            _sync_with(manager, FakeS3(), str(source), direction="upload")

    def test_empty_directory_upload(self, manager: StorageManager, tmp_path: Path) -> None:
        source = tmp_path / "empty"
        source.mkdir()
        result = _sync_with(manager, FakeS3(), str(source), direction="upload")
        assert result["files_scanned"] == 0
        assert result["files_uploaded"] == 0

    def test_invalid_direction_is_rejected(self, manager: StorageManager, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="either 'download' or 'upload'"):
            _sync_with(manager, FakeS3(), str(tmp_path), direction="both")


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative confinement requires POSIX")
class TestConfinedSync:
    @staticmethod
    def _identity(root: Path) -> tuple[int, int]:
        value = root.stat()
        return value.st_dev, value.st_ino

    def test_confined_download_and_upload_round_trip(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)
        s3 = FakeS3()
        s3.put("prefix/nested/file.txt", b"payload")

        downloaded = _sync_with(
            manager,
            s3,
            "downloads",
            prefix="prefix",
            confinement_root=str(root),
            confinement_device=device,
            confinement_inode=inode,
        )
        assert downloaded["files_downloaded"] == 1
        assert (root / "downloads/nested/file.txt").read_bytes() == b"payload"

        source = root / "uploads"
        source.mkdir()
        (source / "new.txt").write_bytes(b"new")
        uploaded = _sync_with(
            manager,
            s3,
            "uploads",
            direction="upload",
            prefix="out",
            confinement_root=str(root),
            confinement_device=device,
            confinement_inode=inode,
        )
        assert uploaded["files_uploaded"] == 1
        assert s3.objects["out/new.txt"].data == b"new"

        repeated = _sync_with(
            manager,
            s3,
            "uploads",
            direction="upload",
            prefix="out",
            confinement_root=str(root),
            confinement_device=device,
            confinement_inode=inode,
        )
        assert repeated["files_skipped"] == 1

    def test_confinement_contract_validation(self, manager: StorageManager, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)

        assert manager._make_confinement_contract(None, None, None) is None
        for values in ((str(root), None, inode), (None, device, inode), (str(root), device, None)):
            with pytest.raises(ValueError, match="incomplete"):
                manager._make_confinement_contract(*values)
        with pytest.raises(ValueError, match="invalid"):
            manager._make_confinement_contract(str(root), -1, inode)
        with pytest.raises(ValueError, match="normalized and absolute"):
            manager._make_confinement_contract("relative", device, inode)
        with pytest.raises(ValueError, match="normalized and absolute"):
            manager._make_confinement_contract(str(root / ".." / "root"), device, inode)

    def test_pinned_root_identity_and_path_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)
        with pytest.raises(RuntimeError, match="changed after"):
            _PinnedRoot(_ConfinementContract(root, device, inode + 1))

        with _PinnedRoot(_ConfinementContract(root, device, inode)) as pinned:
            assert pinned.relative_parts(".") == ()
            assert pinned.relative_parts("child/file") == ("child", "file")
            assert pinned.display_path(("child",)) == root / "child"
            with pytest.raises(ValueError, match="stay within"):
                pinned.relative_parts(str(tmp_path / "outside"))
            assert pinned.open_directory(("missing",), allow_missing=True) is None
            assert not pinned.inspect_directory(("missing",), create=False)
            assert pinned.lstat(("missing",)) is None
            with pytest.raises(FileNotFoundError, match="does not exist"):
                pinned.open_directory(("missing",))
            with pytest.raises(IsADirectoryError):
                pinned.open_regular_file(())

    def test_confined_symlink_and_nonregular_rejection(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)
        outside = tmp_path / "outside"
        outside.mkdir()
        (root / "link").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="symbolic link|non-directory"):
            _sync_with(
                manager,
                FakeS3(),
                "link/source",
                direction="upload",
                confinement_root=str(root),
                confinement_device=device,
                confinement_inode=inode,
            )

        fifo = root / "fifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="regular file or directory"):
            _sync_with(
                manager,
                FakeS3(),
                "fifo",
                direction="upload",
                confinement_root=str(root),
                confinement_device=device,
                confinement_inode=inode,
            )

    def test_confined_download_rejects_existing_link_and_directory(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        destination = root / "downloads"
        destination.mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.write_text("outside", encoding="utf-8")
        device, inode = self._identity(root)
        s3 = FakeS3()
        s3.put("linked", b"x")
        (destination / "linked").symlink_to(outside)

        with pytest.raises(ValueError, match="must not be a symbolic link"):
            _sync_with(
                manager,
                s3,
                "downloads",
                dry_run=True,
                confinement_root=str(root),
                confinement_device=device,
                confinement_inode=inode,
            )

        (destination / "linked").unlink()
        (destination / "linked").mkdir()
        with pytest.raises(IsADirectoryError, match="existing directory"):
            _sync_with(
                manager,
                s3,
                "downloads",
                dry_run=True,
                confinement_root=str(root),
                confinement_device=device,
                confinement_inode=inode,
            )

    def test_confined_download_detects_wrong_size_and_cleans_temp(
        self,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)
        s3 = FakeS3()
        s3.put("file", b"short")
        obj = _SyncObject("file", root / "file", ("file",), 99, NOW, False)

        with (
            _PinnedRoot(_ConfinementContract(root, device, inode)) as pinned,
            pytest.raises(RuntimeError, match="Downloaded size changed"),
        ):
            pinned.download_object(s3, BUCKET["bucket"], obj)
        assert list(root.iterdir()) == []

    def test_pinned_root_requires_supported_absolute_directory(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        device, inode = self._identity(root)
        contract = _ConfinementContract(root, device, inode)
        with patch("cli.storage.os.name", "nt"), pytest.raises(RuntimeError, match="requires"):
            _PinnedRoot(contract)
        with pytest.raises(ValueError, match="must be absolute"):
            _PinnedRoot(_ConfinementContract(Path("relative"), device, inode))

        regular = tmp_path / "file"
        regular.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot open"):
            _PinnedRoot(_ConfinementContract(regular, *self._identity(regular)))


@pytest.mark.skipif(os.name != "posix", reason="descriptor-relative safety checks require POSIX")
class TestStorageSafetyBranches:
    def test_helper_edge_cases(self, tmp_path: Path) -> None:
        assert StorageManager._normalize_prefix("") == ""
        assert StorageManager._normalize_prefix("///prefix") == "prefix/"
        for relative in ("", "bad\x00name", "bad\\name", "./name", "name//child"):
            with pytest.raises(ValueError, match="represented safely"):
                StorageManager._validate_upload_relative_path(relative, tmp_path / "source")

        missing = tmp_path / "missing"
        assert not StorageManager._is_current(missing, 0, NOW)
        current = tmp_path / "current"
        current.write_bytes(b"abc")
        assert not StorageManager._is_current(current, 3, None)
        assert not StorageManager._is_current(current, 4, NOW)
        future = NOW + timedelta(hours=1)
        os.utime(current, (future.timestamp(), future.timestamp()))
        assert StorageManager._is_current(current, 3, NOW)

    def test_hash_rejects_nonregular_and_detects_planning_change(self, tmp_path: Path) -> None:
        fifo = tmp_path / "fifo"
        os.mkfifo(fifo)
        fifo_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        with pytest.raises(ValueError, match="not a regular file"):
            StorageManager._hash_upload_fd(fifo_fd, fifo)

        source = tmp_path / "source"
        source.write_bytes(b"data")
        first = (1, 2, 4, 5, 6)
        second = (1, 2, 4, 7, 8)
        with (
            patch.object(StorageManager, "_stat_signature", side_effect=[first, second]),
            pytest.raises(RuntimeError, match="changed while planning"),
        ):
            StorageManager._hash_upload_file(source)

    def test_collect_upload_rejects_descendant_nonregular_file(self, tmp_path: Path) -> None:
        source = tmp_path / "source"
        source.mkdir()
        os.mkfifo(source / "fifo")
        with pytest.raises(ValueError, match="non-regular file"):
            StorageManager._collect_upload_files(source)

    def test_confined_single_file_missing_and_top_level_link(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        source = root / "single.bin"
        source.write_bytes(b"single")
        identity = root.stat()
        contract = {
            "confinement_root": str(root),
            "confinement_device": identity.st_dev,
            "confinement_inode": identity.st_ino,
        }
        s3 = FakeS3()
        result = _sync_with(manager, s3, "single.bin", direction="upload", **contract)
        assert result["files_uploaded"] == 1
        assert s3.objects["single.bin"].data == b"single"

        with pytest.raises(FileNotFoundError, match="not found"):
            _sync_with(manager, s3, "missing", direction="upload", **contract)

        linked = root / "linked"
        linked.symlink_to(source)
        with pytest.raises(ValueError, match="symbolic link"):
            _sync_with(manager, s3, "linked", direction="upload", **contract)

    def test_pinned_root_low_level_rejections(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        regular = root / "regular"
        regular.write_bytes(b"data")
        linked = root / "linked"
        linked.symlink_to(regular)
        fifo = root / "fifo"
        os.mkfifo(fifo)
        root_stat = root.stat()

        with _PinnedRoot(_ConfinementContract(root, root_stat.st_dev, root_stat.st_ino)) as pinned:
            with pytest.raises(ValueError, match="missing, linked, or not a regular file"):
                pinned.open_regular_file(("linked",))
            with pytest.raises(ValueError, match="not a regular file"):
                pinned.open_regular_file(("fifo",))

            parent_fd = pinned.open_directory(())
            assert parent_fd is not None
            try:
                with pytest.raises(ValueError, match="directory changed or is linked"):
                    pinned.open_child_directory(parent_fd, "regular", regular)
                with pytest.raises(ValueError, match="not a regular file"):
                    pinned.open_child_regular_file(parent_fd, "fifo", fifo)
            finally:
                os.close(parent_fd)

            assert not pinned.download_target_is_current(
                ("missing-parent", "file"), 0, NOW, evaluate_current=True
            )
            assert not pinned.download_target_is_current(
                ("missing",), 0, NOW, evaluate_current=True
            )
            with pytest.raises(ValueError, match="not a regular file"):
                pinned.download_target_is_current(("fifo",), 0, NOW, evaluate_current=True)

    def test_confined_walk_rejects_descendant_link_and_fifo(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        identity = root.stat()
        contract = {
            "confinement_root": str(root),
            "confinement_device": identity.st_dev,
            "confinement_inode": identity.st_ino,
        }
        external = tmp_path / "external"
        external.write_text("x", encoding="utf-8")
        linked_source = root / "linked-source"
        linked_source.mkdir()
        (linked_source / "child").symlink_to(external)
        with pytest.raises(ValueError, match="contains a symbolic link"):
            _sync_with(
                manager,
                FakeS3(),
                "linked-source",
                direction="upload",
                **contract,
            )

        fifo_source = root / "fifo-source"
        fifo_source.mkdir()
        os.mkfifo(fifo_source / "child")
        with pytest.raises(ValueError, match="non-regular file"):
            _sync_with(
                manager,
                FakeS3(),
                "fifo-source",
                direction="upload",
                **contract,
            )

    def test_skipped_upload_detects_local_changes_around_remote_probe(
        self,
        manager: StorageManager,
        tmp_path: Path,
    ) -> None:
        source = tmp_path / "source"
        source.write_bytes(b"same")
        digest = hashlib.sha256(b"same").hexdigest()
        s3 = FakeS3()
        s3.put("source", b"same", metadata={"gco-sync-sha256": digest})
        original_plan = manager._build_upload_plan

        def mutate_after_plan(*args: Any, **kwargs: Any):
            plan = original_plan(*args, **kwargs)
            source.write_bytes(b"different")
            return plan

        manager._build_upload_plan = mutate_after_plan  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="skipped local file changed after planning"):
            _sync_with(manager, s3, str(source), direction="upload")

        manager._build_upload_plan = original_plan  # type: ignore[method-assign]
        source.write_bytes(b"same")
        original_head = s3.head_object
        calls = 0

        def mutate_during_probe(**kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            response = original_head(**kwargs)
            calls += 1
            if calls == 2:
                source.write_bytes(b"diff")
            return response

        s3.head_object = mutate_during_probe  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="changed during revalidation"):
            _sync_with(manager, s3, str(source), direction="upload")
