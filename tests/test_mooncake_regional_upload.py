"""
Tests for ``RegionalBucketManager`` bucket resolution and upload error paths.

``RegionalBucketManager`` (in ``cli/models.py``) uploads local files to a
region's general-purpose ``gco-regional-shared-<account>-<region>`` bucket. It
must resolve the bucket name from the *target* region's own
``/gco/regional-shared-bucket/name`` SSM parameter — never the global region's
or another region's — write every object only to that bucket, fail without
writing anything when the parameter is absent, and stop with a non-zero result
naming the offending object when a write fails partway through.

These tests stub the SSM lookup and the boto3 S3 client with ``unittest.mock``
so no AWS calls are made.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli.models import RegionalBucketManager


@pytest.fixture
def config():
    """A config whose global region differs from any target region.

    The differing global region lets a test prove resolution never falls back
    to the global region: if the manager ever reached for the global bucket the
    SSM call would carry ``us-east-2`` instead of the caller's target region.
    """
    cfg = MagicMock()
    cfg.global_region = "us-east-2"
    cfg.project_name = "gco"
    return cfg


def _make_file(tmp_path, name="weights.bin", content="data"):
    f = tmp_path / name
    f.write_text(content)
    return f


# =============================================================================
# Target-region resolution
# =============================================================================


class TestTargetRegionResolution:
    def test_resolves_target_regions_own_name_parameter(self, config, tmp_path):
        """The bucket name comes from the target region's own SSM parameter."""
        f = _make_file(tmp_path)
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value="gco-regional-shared-111122223333-eu-west-1",
            ) as mock_ssm,
            patch("cli.models.boto3.client", return_value=s3) as mock_boto,
        ):
            result = mgr.upload(str(f), region="eu-west-1")

        # The lookup targets the caller's region and the regional namespace,
        # not the global region.
        mock_ssm.assert_called_once_with(
            "/gco/regional-shared-bucket/name",
            region="eu-west-1",
        )
        assert config.global_region not in [
            call.kwargs.get("region") for call in mock_ssm.call_args_list
        ]

        # The S3 client is scoped to the target region.
        mock_boto.assert_called_once_with("s3", region_name="eu-west-1")

        # Every object lands in the resolved regional bucket only.
        assert result["region"] == "eu-west-1"
        assert result["bucket"] == "gco-regional-shared-111122223333-eu-west-1"
        assert result["s3_uri"] == (
            "s3://gco-regional-shared-111122223333-eu-west-1/uploads"
        )
        assert result["files_uploaded"] == 1

        upload_bucket = s3.upload_file.call_args.args[1]
        assert upload_bucket == "gco-regional-shared-111122223333-eu-west-1"

    def test_each_region_resolves_its_own_bucket(self, config, tmp_path):
        """Uploading to two regions resolves each region's own parameter."""
        f = _make_file(tmp_path)
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        def fake_ssm(_name, *, region):
            return f"gco-regional-shared-111122223333-{region}"

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                side_effect=fake_ssm,
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            first = mgr.upload(str(f), region="us-west-2")
            second = mgr.upload(str(f), region="ap-northeast-1")

        assert first["bucket"] == "gco-regional-shared-111122223333-us-west-2"
        assert second["bucket"] == "gco-regional-shared-111122223333-ap-northeast-1"

    def test_custom_prefix_is_used_for_keys(self, config, tmp_path):
        """A caller-supplied prefix is applied to uploaded object keys."""
        f = _make_file(tmp_path, name="model.safetensors")
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value="gco-regional-shared-111122223333-eu-west-1",
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            result = mgr.upload(str(f), region="eu-west-1", prefix="checkpoints")

        key = s3.upload_file.call_args.args[2]
        assert key == "checkpoints/model.safetensors"
        assert result["s3_uri"].endswith("/checkpoints")


# =============================================================================
# Absent-parameter rejection
# =============================================================================


class TestAbsentParameterRejection:
    def test_missing_parameter_raises_and_writes_nothing(self, config, tmp_path):
        """An absent SSM parameter aborts the upload before any object is written."""
        f = _make_file(tmp_path)
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value=None,
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            with pytest.raises(RuntimeError) as exc:
                mgr.upload(str(f), region="eu-west-1")

        assert "eu-west-1" in str(exc.value)
        s3.upload_file.assert_not_called()

    def test_empty_parameter_value_is_treated_as_absent(self, config, tmp_path):
        """An empty parameter value is rejected the same as a missing one."""
        f = _make_file(tmp_path)
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value="",
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            with pytest.raises(RuntimeError):
                mgr.upload(str(f), region="eu-west-1")

        s3.upload_file.assert_not_called()


# =============================================================================
# Mid-upload failure exit behavior
# =============================================================================


class TestMidUploadFailure:
    def test_failure_stops_and_names_offending_object(self, config, tmp_path):
        """A write failure stops the upload and names the object that failed."""
        # A directory with several files so the failure lands mid-walk.
        (tmp_path / "a.bin").write_text("a")
        (tmp_path / "b.bin").write_text("b")
        (tmp_path / "c.bin").write_text("c")

        mgr = RegionalBucketManager(config)
        s3 = MagicMock()

        # Succeed on the first object, fail on the second.
        s3.upload_file.side_effect = [None, OSError("connection reset"), None]

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value="gco-regional-shared-111122223333-eu-west-1",
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            with pytest.raises(RuntimeError) as exc:
                mgr.upload(str(tmp_path), region="eu-west-1")

        message = str(exc.value)
        # The error explains the upload did not finish and names the bucket.
        assert "did not complete" in message
        assert "gco-regional-shared-111122223333-eu-west-1" in message
        # The underlying cause is surfaced.
        assert "connection reset" in message

        # The walk stopped at the failing object: only two writes attempted,
        # never the third.
        assert s3.upload_file.call_count == 2

    def test_failure_surfaces_as_exception(self, config, tmp_path):
        """A single-file write failure surfaces as a raised error."""
        f = _make_file(tmp_path)
        mgr = RegionalBucketManager(config)
        s3 = MagicMock()
        s3.upload_file.side_effect = OSError("access denied")

        with (
            patch(
                "gco.services.aws_ssm.get_ssm_parameter_optional",
                return_value="gco-regional-shared-111122223333-eu-west-1",
            ),
            patch("cli.models.boto3.client", return_value=s3),
        ):
            with pytest.raises(RuntimeError):
                mgr.upload(str(f), region="eu-west-1")


# =============================================================================
# Missing local path
# =============================================================================


def test_missing_local_path_raises_before_resolution(config):
    """A nonexistent local path fails before any bucket resolution or write."""
    mgr = RegionalBucketManager(config)

    with (
        patch("gco.services.aws_ssm.get_ssm_parameter_optional") as mock_ssm,
        patch("cli.models.boto3.client") as mock_boto,
    ):
        with pytest.raises(FileNotFoundError):
            mgr.upload("/no/such/path", region="eu-west-1")

    mock_ssm.assert_not_called()
    mock_boto.assert_not_called()
