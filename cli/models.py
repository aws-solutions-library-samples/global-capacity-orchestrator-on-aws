"""
Model weight management for GCO CLI.

Provides functionality to upload, list, and manage model weights
in the central S3 model bucket. Models uploaded here are automatically
available to inference endpoints across all regions via init container sync.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import boto3

from .config import GCOConfig, get_config

logger = logging.getLogger(__name__)


class ModelManager:
    """Manages model weights in the central S3 bucket."""

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()
        self._bucket_name: str | None = None

    def _get_bucket_name(self) -> str:
        """Discover the model bucket name from SSM."""
        if self._bucket_name:
            return self._bucket_name

        from gco.services.aws_ssm import get_ssm_parameter

        try:
            self._bucket_name = get_ssm_parameter(
                f"/{self.config.project_name}/model-bucket-name",
                region=self.config.global_region,
            )
            return self._bucket_name
        except Exception as e:
            raise RuntimeError(
                "Model bucket not found. Deploy the global stack first "
                "with 'gco stacks deploy gco-global'."
            ) from e

    def _get_s3_client(self) -> Any:
        """Get S3 client for the global region."""
        return boto3.client("s3", region_name=self.config.global_region)

    def upload(
        self,
        local_path: str,
        model_name: str,
        prefix: str = "models",
    ) -> dict[str, Any]:
        """
        Upload model weights to S3.

        Args:
            local_path: Local file or directory path
            model_name: Name for the model in the bucket
            prefix: S3 prefix (default: "models")

        Returns:
            Upload result with S3 URI and file count
        """
        bucket = self._get_bucket_name()
        s3 = self._get_s3_client()
        s3_prefix = f"{prefix}/{model_name}"

        local = Path(local_path)
        uploaded = 0

        if local.is_file():
            key = f"{s3_prefix}/{local.name}"
            s3.upload_file(str(local), bucket, key)
            uploaded = 1
        elif local.is_dir():
            for root, _dirs, files in os.walk(local):
                for fname in files:
                    file_path = Path(root) / fname
                    relative = file_path.relative_to(local)
                    key = f"{s3_prefix}/{relative}"
                    s3.upload_file(str(file_path), bucket, key)
                    uploaded += 1
        else:
            raise FileNotFoundError(f"Path not found: {local_path}")

        s3_uri = f"s3://{bucket}/{s3_prefix}"
        return {
            "model_name": model_name,
            "s3_uri": s3_uri,
            "bucket": bucket,
            "prefix": s3_prefix,
            "files_uploaded": uploaded,
        }

    def list_models(self, prefix: str = "models") -> list[dict[str, Any]]:
        """List all models in the bucket."""
        bucket = self._get_bucket_name()
        s3 = self._get_s3_client()

        # List top-level "directories" under the prefix
        response = s3.list_objects_v2(
            Bucket=bucket,
            Prefix=f"{prefix}/",
            Delimiter="/",
        )

        models = []
        for cp in response.get("CommonPrefixes", []):
            model_prefix = cp["Prefix"]
            model_name = model_prefix.rstrip("/").split("/")[-1]

            # Get total size and file count
            total_size = 0
            file_count = 0
            paginator = s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=bucket, Prefix=model_prefix):
                for obj in page.get("Contents", []):
                    total_size += obj.get("Size", 0)
                    file_count += 1

            models.append(
                {
                    "model_name": model_name,
                    "s3_uri": f"s3://{bucket}/{model_prefix.rstrip('/')}",
                    "files": file_count,
                    "total_size_gb": round(total_size / (1024**3), 2),
                }
            )

        return models

    def get_model_uri(self, model_name: str, prefix: str = "models") -> str:
        """Get the S3 URI for a model."""
        bucket = self._get_bucket_name()
        return f"s3://{bucket}/{prefix}/{model_name}"

    def delete_model(self, model_name: str, prefix: str = "models") -> int:
        """Delete every version and delete marker for a model prefix.

        The central model bucket is versioned. Deleting only the current
        objects creates delete markers and leaves prior versions behind, which
        can prevent later bucket removal and retain model data unexpectedly.
        """
        bucket = self._get_bucket_name()
        s3 = self._get_s3_client()
        s3_prefix = f"{prefix}/{model_name}/"

        deleted_keys: set[str] = set()
        deletion_errors: list[str] = []
        paginator = s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
            versioned_objects = []
            for item in [*page.get("Versions", []), *page.get("DeleteMarkers", [])]:
                key = item.get("Key")
                version_id = item.get("VersionId")
                if not key:
                    continue
                identifier = {"Key": key}
                if version_id is not None:
                    identifier["VersionId"] = version_id
                versioned_objects.append(identifier)

            # S3 accepts at most 1,000 identifiers per DeleteObjects request.
            for start in range(0, len(versioned_objects), 1000):
                batch = versioned_objects[start : start + 1000]
                response = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                errors = response.get("Errors", []) if isinstance(response, dict) else []
                errors = [error for error in errors if isinstance(error, dict)]

                for identifier in batch:
                    failed = any(
                        error.get("Key") == identifier["Key"]
                        and (
                            error.get("VersionId") is None
                            or error.get("VersionId") == identifier.get("VersionId")
                        )
                        for error in errors
                    )
                    if not failed:
                        deleted_keys.add(identifier["Key"])

                for error in errors:
                    key = error.get("Key", "<unknown key>")
                    version_id = error.get("VersionId")
                    target = f"{key} (version {version_id})" if version_id else str(key)
                    code = error.get("Code", "UnknownError")
                    message = error.get("Message", "no error message")
                    deletion_errors.append(f"{target}: {code}: {message}")

        if deletion_errors:
            details = "; ".join(deletion_errors)
            raise RuntimeError(
                f"Failed to delete {len(deletion_errors)} model object version(s): {details}"
            )

        return len(deleted_keys)


class RegionalBucketManager:
    """Uploads local files to a region's general-purpose regional bucket.

    Mirrors :class:`ModelManager` but targets the per-region
    ``gco-regional-shared-<account>-<region>`` bucket instead of the central
    model bucket. The bucket name is always resolved from the *target
    region's own* SSM parameter store, never the global region's or any other
    region's, so an upload only ever writes to the bucket that lives in the
    region the caller named.
    """

    def __init__(self, config: GCOConfig | None = None):
        self.config = config or get_config()

    def _get_bucket_name(self, region: str) -> str:
        """Resolve the regional bucket name from the target region's SSM store.

        Reads ``/<project_name>/regional-shared-bucket/name`` from the
        parameter store in ``region``. The regional bucket is always
        provisioned, so this parameter is present once the region's stack is
        deployed. A missing parameter means the region has not been deployed
        yet and is treated as a hard "bucket not found" failure.
        """
        from gco.services.aws_ssm import get_ssm_parameter_optional
        from gco.stacks.constants import regional_shared_ssm_parameter_prefix

        name = get_ssm_parameter_optional(
            f"{regional_shared_ssm_parameter_prefix(self.config.project_name)}/name",
            region=region,
        )
        if not name:
            raise RuntimeError(
                f"Regional bucket not found in region '{region}'. Deploy that "
                f"region's stack first with 'gco stacks deploy'."
            )
        return name

    def _get_s3_client(self, region: str) -> Any:
        """Get an S3 client scoped to the target region."""
        return boto3.client("s3", region_name=region)

    def upload(
        self,
        local_path: str,
        region: str,
        *,
        prefix: str = "uploads",
    ) -> dict[str, Any]:
        """
        Upload local files or a directory to a region's regional bucket.

        Args:
            local_path: Local file or directory path
            region: Target region whose regional bucket receives the objects
            prefix: S3 prefix for uploaded objects (default: "uploads")

        Returns:
            Upload result with the region, bucket, S3 URI, and file count

        Raises:
            RuntimeError: If the target region's bucket cannot be resolved (no
                objects are written) or if an object fails mid-upload (the
                upload stops and the offending object is named).
            FileNotFoundError: If ``local_path`` does not exist.
        """
        local = Path(local_path)
        if not local.exists():
            raise FileNotFoundError(f"Path not found: {local_path}")

        # Resolve the bucket before writing anything so an undeployed region
        # fails fast without partial uploads.
        bucket = self._get_bucket_name(region)
        s3 = self._get_s3_client(region)
        uploaded = 0

        files: list[tuple[Path, str]]
        if local.is_file():
            files = [(local, local.name)]
        else:
            files = []
            for root, _dirs, names in os.walk(local):
                for fname in names:
                    walk_path = Path(root) / fname
                    rel = walk_path.relative_to(local)
                    files.append((walk_path, str(rel)))

        for file_path, relative in files:
            key = f"{prefix}/{relative}"
            try:
                s3.upload_file(str(file_path), bucket, key)
            except Exception as e:
                raise RuntimeError(
                    f"Upload did not complete: failed to write object "
                    f"'s3://{bucket}/{key}' to region '{region}': {e}"
                ) from e
            uploaded += 1

        s3_uri = f"s3://{bucket}/{prefix}"
        return {
            "region": region,
            "bucket": bucket,
            "s3_uri": s3_uri,
            "files_uploaded": uploaded,
        }

    def populate_kv_cache(
        self,
        local_path: str,
        region: str,
        endpoint_name: str,
    ) -> dict[str, Any]:
        """Upload data into an endpoint's Mooncake KV-cache cold tier.

        Writes ``local_path`` to the region's general-purpose bucket under the
        cold-tier key prefix the per-region monitor reads from for this endpoint
        (``mooncake-kv/<endpoint_name>/``), so an endpoint deployed with the
        cold tier enabled warm-starts its prefix cache from the uploaded
        objects. Resolution and upload mechanics are exactly those of
        :meth:`upload`; the returned mapping additionally carries the endpoint
        name.

        Args:
            local_path: Local file or directory to upload.
            region: Region whose general-purpose bucket backs the cold tier.
            endpoint_name: The endpoint whose cold-tier prefix receives the data.

        Returns:
            The :meth:`upload` result with an added ``endpoint`` key.
        """
        from gco.stacks.constants import MOONCAKE_COLD_TIER_KEY_PREFIX

        prefix = f"{MOONCAKE_COLD_TIER_KEY_PREFIX}/{endpoint_name}"
        result = self.upload(local_path, region, prefix=prefix)
        result["endpoint"] = endpoint_name
        return result


def get_model_manager(config: GCOConfig | None = None) -> ModelManager:
    """Factory function for ModelManager."""
    return ModelManager(config)


def get_regional_bucket_manager(
    config: GCOConfig | None = None,
) -> RegionalBucketManager:
    """Factory function for RegionalBucketManager."""
    return RegionalBucketManager(config)
