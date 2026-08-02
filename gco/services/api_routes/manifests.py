"""Manifest submission, validation, and resource management endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from gco.models import ManifestSubmissionRequest
from gco.services.api_shared import ManifestSubmissionAPIRequest, _check_processor

router = APIRouter(prefix="/api/v1/manifests", tags=["Manifests"])
logger = logging.getLogger(__name__)


@router.post("")
async def submit_manifests(request: ManifestSubmissionAPIRequest) -> Response:
    """Submit Kubernetes manifests for processing."""
    from gco.services.manifest_api import manifest_metrics

    processor = _check_processor()

    try:
        logger.info(f"Received manifest submission request with {len(request.manifests)} manifests")

        try:
            submission_request = ManifestSubmissionRequest(
                manifests=request.manifests,
                namespace=request.namespace,
                dry_run=request.dry_run,
                validate=request.validate_manifests,
            )
        except ValueError as e:
            # Client-side validation failures (empty manifests, unparseable
            # payloads, etc.) should surface as 400, not 500.
            logger.info(f"Rejected manifest submission as invalid input: {e}")
            raise HTTPException(status_code=400, detail=str(e)) from e

        response = await processor.process_manifest_submission(submission_request)

        if manifest_metrics and not request.dry_run:
            try:
                successful = sum(1 for r in response.resources if r.is_successful())
                failed = sum(1 for r in response.resources if not r.is_successful())
                validation_failures = sum(
                    1
                    for r in response.resources
                    if r.status == "failed" and "validation" in (r.message or "").lower()
                )
                manifest_metrics.publish_submission_metrics(
                    total_submissions=len(response.resources),
                    successful_submissions=successful,
                    failed_submissions=failed,
                    validation_failures=validation_failures,
                )
            except Exception as e:
                logger.warning(f"Failed to publish manifest metrics: {e}")

        api_response: dict[str, Any] = {
            "success": response.success,
            "cluster_id": response.cluster_id,
            "region": response.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "summary": response.get_summary(),
            "resources": [
                {
                    "api_version": r.api_version,
                    "kind": r.kind,
                    "name": r.name,
                    "namespace": r.namespace,
                    "status": r.status,
                    "message": r.message,
                }
                for r in response.resources
            ],
        }

        if response.errors:
            api_response["errors"] = response.errors

        status_code = 200 if response.success else 400
        return JSONResponse(status_code=status_code, content=api_response)

    except HTTPException:
        # Already a well-formed HTTP error — don't demote to 500.
        raise
    except Exception as e:
        logger.error(f"Error processing manifest submission: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.post("/validate")
async def validate_manifests(request: ManifestSubmissionAPIRequest) -> Response:
    """Validate manifests without applying them."""
    processor = _check_processor()

    try:
        logger.info(f"Validating {len(request.manifests)} manifests")

        validation_results = []
        overall_valid = True

        for i, manifest in enumerate(request.manifests):
            is_valid, error_msg = processor.validate_manifest(manifest, request.namespace)

            result: dict[str, Any] = {
                "manifest_index": i,
                "valid": is_valid,
                "api_version": manifest.get("apiVersion", "unknown"),
                "kind": manifest.get("kind", "unknown"),
                "name": manifest.get("metadata", {}).get("name", f"manifest-{i + 1}"),
                "namespace": manifest.get("metadata", {}).get(
                    "namespace", request.namespace or "gco-jobs"
                ),
            }

            if not is_valid:
                result["error"] = error_msg
                overall_valid = False

            validation_results.append(result)

        response = {
            "valid": overall_valid,
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "total_manifests": len(request.manifests),
            "valid_manifests": sum(1 for r in validation_results if r["valid"]),
            "invalid_manifests": sum(1 for r in validation_results if not r["valid"]),
            "results": validation_results,
        }

        return JSONResponse(status_code=200, content=response)

    except Exception as e:
        logger.error(f"Error validating manifests: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.get("/{namespace}/{name}")
async def get_resource_status(
    namespace: str,
    name: str,
    api_version: str = Query("apps/v1", description="Kubernetes API version"),
    kind: str = Query("Deployment", description="Resource kind"),
) -> Response:
    """Get the status of a specific resource."""
    processor = _check_processor()

    try:
        resource_info = await processor.get_resource_status(
            api_version=api_version, kind=kind, name=name, namespace=namespace
        )

        if resource_info is None:
            raise HTTPException(status_code=500, detail="Failed to retrieve resource information")
        if resource_info.get("forbidden", False):
            raise HTTPException(
                status_code=403,
                detail=resource_info.get("error", "Resource access denied"),
            )

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "resource": resource_info,
        }

        status_code = 200 if resource_info.get("exists", False) else 404
        return JSONResponse(status_code=status_code, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting resource status: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e


@router.delete("/{namespace}/{name}")
async def delete_resource(
    namespace: str,
    name: str,
    api_version: str = Query("apps/v1", description="Kubernetes API version"),
    kind: str = Query("Deployment", description="Resource kind"),
) -> Response:
    """Delete a specific resource from the cluster."""
    processor = _check_processor()

    try:
        resource_status = await processor.delete_resource(
            api_version=api_version, kind=kind, name=name, namespace=namespace
        )

        if resource_status.status == "forbidden":
            raise HTTPException(
                status_code=403,
                detail=resource_status.message or "Resource access denied",
            )

        response = {
            "success": resource_status.is_successful(),
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "resource": {
                "api_version": resource_status.api_version,
                "kind": resource_status.kind,
                "name": resource_status.name,
                "namespace": resource_status.namespace,
                "status": resource_status.status,
                "message": resource_status.message,
            },
        }

        status_code = 200 if resource_status.is_successful() else 400
        return JSONResponse(status_code=status_code, content=response)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting resource: {e}")
        raise HTTPException(status_code=500, detail=f"Internal server error: {e!s}") from e
