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
        """Delete a model and all its files from S3."""
        bucket = self._get_bucket_name()
        s3 = self._get_s3_client()
        s3_prefix = f"{prefix}/{model_name}/"

        # List and delete all objects
        deleted = 0
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=s3_prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if objects:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})
                deleted += len(objects)

        return deleted


def get_model_manager(config: GCOConfig | None = None) -> ModelManager:
    """Factory function for ModelManager."""
    return ModelManager(config)
