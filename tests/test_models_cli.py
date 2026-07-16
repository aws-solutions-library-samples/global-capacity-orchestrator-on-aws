"""
Tests for cli/models.ModelManager — S3 model weight management.

Exercises the bucket-name lookup from SSM Parameter Store (with
in-process caching and RuntimeError surfacing when the parameter is
missing), the region-aware _get_s3_client helper, and the upload
method across both single-file and directory inputs. Uses tmp_path
to build real files while mocking the S3 client, so the test fleet
is fast and deterministic.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli.models import ModelManager, get_model_manager


class TestModelManager:
    """Tests for ModelManager."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.global_region = "us-east-2"
        config.project_name = "gco"
        return config

    @pytest.fixture
    def manager(self, mock_config):
        with patch("cli.models.get_config", return_value=mock_config):
            mgr = ModelManager(config=mock_config)
        return mgr

    # -- _get_bucket_name --

    def test_get_bucket_name_from_ssm(self, manager):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "gco-model-bucket-abc123"}}
        with patch("cli.models.boto3.client", return_value=mock_ssm):
            name = manager._get_bucket_name()
        assert name == "gco-model-bucket-abc123"
        mock_ssm.get_parameter.assert_called_once_with(Name="/gco/model-bucket-name")

    def test_get_bucket_name_cached(self, manager):
        manager._bucket_name = "cached-bucket"
        assert manager._get_bucket_name() == "cached-bucket"

    def test_get_bucket_name_ssm_error_raises(self, manager):
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = Exception("SSM error")
        with (
            patch("cli.models.boto3.client", return_value=mock_ssm),
            pytest.raises(RuntimeError, match="Model bucket not found"),
        ):
            manager._get_bucket_name()

    # -- _get_s3_client --

    def test_get_s3_client(self, manager):
        mock_s3 = MagicMock()
        with patch("cli.models.boto3.client", return_value=mock_s3) as mock_boto:
            result = manager._get_s3_client()
        mock_boto.assert_called_once_with("s3", region_name="us-east-2")
        assert result is mock_s3

    # -- upload: single file --

    def test_upload_single_file(self, manager, tmp_path):
        test_file = tmp_path / "model.bin"
        test_file.write_text("data")

        mock_s3 = MagicMock()
        manager._bucket_name = "test-bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.upload(str(test_file), "my-model")

        assert result["model_name"] == "my-model"
        assert result["s3_uri"] == "s3://test-bucket/models/my-model"
        assert result["files_uploaded"] == 1
        assert result["bucket"] == "test-bucket"
        assert result["prefix"] == "models/my-model"
        mock_s3.upload_file.assert_called_once_with(
            str(test_file), "test-bucket", "models/my-model/model.bin"
        )

    # -- upload: directory --

    def test_upload_directory(self, manager, tmp_path):
        model_dir = tmp_path / "llama"
        model_dir.mkdir()
        (model_dir / "weights.bin").write_text("w")
        (model_dir / "config.json").write_text("c")

        mock_s3 = MagicMock()
        manager._bucket_name = "test-bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.upload(str(model_dir), "llama3")

        assert result["files_uploaded"] == 2
        assert result["model_name"] == "llama3"
        assert mock_s3.upload_file.call_count == 2

    # -- upload: nested directory --

    def test_upload_nested_directory(self, manager, tmp_path):
        model_dir = tmp_path / "model"
        model_dir.mkdir()
        sub = model_dir / "subdir"
        sub.mkdir()
        (model_dir / "a.bin").write_text("a")
        (sub / "b.bin").write_text("b")

        mock_s3 = MagicMock()
        manager._bucket_name = "test-bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.upload(str(model_dir), "nested", prefix="weights")

        assert result["files_uploaded"] == 2
        assert result["prefix"] == "weights/nested"

    # -- upload: path not found --

    def test_upload_path_not_found(self, manager):
        manager._bucket_name = "test-bucket"
        mock_s3 = MagicMock()
        with (
            patch.object(manager, "_get_s3_client", return_value=mock_s3),
            pytest.raises(FileNotFoundError, match="Path not found"),
        ):
            manager.upload("/nonexistent/path", "model")

    # -- upload: custom prefix --

    def test_upload_custom_prefix(self, manager, tmp_path):
        test_file = tmp_path / "f.bin"
        test_file.write_text("x")

        mock_s3 = MagicMock()
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.upload(str(test_file), "m", prefix="custom")

        assert result["prefix"] == "custom/m"
        assert result["s3_uri"] == "s3://bucket/custom/m"

    # -- list_models --

    def test_list_models_empty(self, manager):
        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {}
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.list_models()

        assert result == []

    def test_list_models_with_results(self, manager):
        mock_s3 = MagicMock()
        mock_s3.list_objects_v2.return_value = {
            "CommonPrefixes": [
                {"Prefix": "models/llama3/"},
                {"Prefix": "models/mistral/"},
            ]
        }
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Size": 1024**3, "Key": "k1"}, {"Size": 1024**3, "Key": "k2"}]}
        ]
        mock_s3.get_paginator.return_value = paginator
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            result = manager.list_models()

        assert len(result) == 2
        assert result[0]["model_name"] == "llama3"
        assert result[0]["files"] == 2
        assert result[0]["total_size_gb"] == 2.0
        assert result[1]["model_name"] == "mistral"

    # -- get_model_uri --

    def test_get_model_uri(self, manager):
        manager._bucket_name = "bucket"
        uri = manager.get_model_uri("llama3")
        assert uri == "s3://bucket/models/llama3"

    def test_get_model_uri_custom_prefix(self, manager):
        manager._bucket_name = "bucket"
        uri = manager.get_model_uri("m", prefix="weights")
        assert uri == "s3://bucket/weights/m"

    # -- delete_model --

    def test_delete_model_with_objects(self, manager):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Versions": [
                    {"Key": "models/m/a.bin", "VersionId": "v2"},
                    {"Key": "models/m/b.bin", "VersionId": "v1"},
                ],
                "DeleteMarkers": [],
            }
        ]
        mock_s3.get_paginator.return_value = paginator
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            deleted = manager.delete_model("m")

        assert deleted == 2
        mock_s3.get_paginator.assert_called_once_with("list_object_versions")
        mock_s3.delete_objects.assert_called_once_with(
            Bucket="bucket",
            Delete={
                "Objects": [
                    {"Key": "models/m/a.bin", "VersionId": "v2"},
                    {"Key": "models/m/b.bin", "VersionId": "v1"},
                ]
            },
        )

    def test_delete_model_empty(self, manager):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [{"Versions": [], "DeleteMarkers": []}]
        mock_s3.get_paginator.return_value = paginator
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            deleted = manager.delete_model("empty")

        assert deleted == 0
        mock_s3.delete_objects.assert_not_called()

    def test_delete_model_multiple_pages(self, manager):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Versions": [{"Key": "models/m/a.bin", "VersionId": "v1"}],
                "DeleteMarkers": [],
            },
            {
                "Versions": [{"Key": "models/m/b.bin", "VersionId": "v3"}],
                "DeleteMarkers": [],
            },
        ]
        mock_s3.get_paginator.return_value = paginator
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            deleted = manager.delete_model("m")

        assert deleted == 2
        assert mock_s3.delete_objects.call_count == 2

    def test_delete_model_removes_prior_versions_and_delete_markers(self, manager):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Versions": [
                    {"Key": "models/m/weights.bin", "VersionId": "current"},
                    {"Key": "models/m/weights.bin", "VersionId": "prior"},
                ],
                "DeleteMarkers": [{"Key": "models/m/config.json", "VersionId": "marker"}],
            }
        ]
        mock_s3.get_paginator.return_value = paginator
        manager._bucket_name = "bucket"

        with patch.object(manager, "_get_s3_client", return_value=mock_s3):
            deleted = manager.delete_model("m")

        assert deleted == 2
        identifiers = mock_s3.delete_objects.call_args.kwargs["Delete"]["Objects"]
        assert identifiers == [
            {"Key": "models/m/weights.bin", "VersionId": "current"},
            {"Key": "models/m/weights.bin", "VersionId": "prior"},
            {"Key": "models/m/config.json", "VersionId": "marker"},
        ]

    def test_delete_model_raises_after_per_object_errors_and_continues(self, manager):
        mock_s3 = MagicMock()
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {
                "Versions": [
                    {"Key": "models/m/a.bin", "VersionId": "v1"},
                    {"Key": "models/m/b.bin", "VersionId": "v1"},
                ],
                "DeleteMarkers": [],
            },
            {
                "Versions": [{"Key": "models/m/c.bin", "VersionId": "v2"}],
                "DeleteMarkers": [],
            },
        ]
        mock_s3.get_paginator.return_value = paginator
        mock_s3.delete_objects.side_effect = [
            {
                "Deleted": [{"Key": "models/m/a.bin", "VersionId": "v1"}],
                "Errors": [
                    {
                        "Key": "models/m/b.bin",
                        "VersionId": "v1",
                        "Code": "AccessDenied",
                        "Message": "denied",
                    }
                ],
            },
            {"Deleted": [{"Key": "models/m/c.bin", "VersionId": "v2"}]},
        ]
        manager._bucket_name = "bucket"

        with (
            patch.object(manager, "_get_s3_client", return_value=mock_s3),
            pytest.raises(RuntimeError, match=r"models/m/b\.bin.*AccessDenied.*denied"),
        ):
            manager.delete_model("m")

        assert mock_s3.delete_objects.call_count == 2


class TestGetModelManager:
    """Tests for the factory function."""

    def test_get_model_manager_default(self):
        with patch("cli.models.get_config") as mock_cfg:
            mock_cfg.return_value = MagicMock()
            mgr = get_model_manager()
        assert isinstance(mgr, ModelManager)

    def test_get_model_manager_with_config(self):
        config = MagicMock()
        mgr = get_model_manager(config)
        assert mgr.config is config
