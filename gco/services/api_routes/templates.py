"""Job template CRUD and job-from-template endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from gco.models import ManifestSubmissionRequest
from gco.services.api_shared import (
    JobFromTemplateRequest,
    JobTemplateRequest,
    _apply_template_parameters,
    _check_namespace,
    _check_processor,
    internal_server_error,
)

if TYPE_CHECKING:
    from gco.services.template_store import TemplateStore

router = APIRouter(tags=["Templates"])
logger = logging.getLogger(__name__)


def _get_template_store() -> TemplateStore:
    from gco.services.manifest_api import template_store

    if template_store is None:
        raise HTTPException(status_code=503, detail="Template store not initialized")
    return template_store


@router.get("/api/v1/templates")
async def list_templates() -> Response:
    """List all job templates."""
    store = _get_template_store()
    try:
        templates = store.list_templates()
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "count": len(templates),
                "templates": templates,
            },
        )
    except Exception as e:
        raise internal_server_error("listing templates", e) from e


@router.post("/api/v1/templates")
async def create_template(request: JobTemplateRequest) -> Response:
    """Create a new job template."""
    store = _get_template_store()
    try:
        template = store.create_template(
            name=request.name,
            manifest=request.manifest,
            description=request.description,
            parameters=request.parameters,
        )
        return JSONResponse(
            status_code=201,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "message": "Template created successfully",
                "template": template,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise internal_server_error("creating template", e) from e


@router.get("/api/v1/templates/{name}")
async def get_template(name: str) -> Response:
    """Get a specific job template."""
    store = _get_template_store()
    try:
        template = store.get_template(name)
        if template is None:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
        return JSONResponse(
            status_code=200,
            content={"timestamp": datetime.now(UTC).isoformat(), "template": template},
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error("getting template", e) from e


@router.delete("/api/v1/templates/{name}")
async def delete_template(name: str) -> Response:
    """Delete a job template."""
    store = _get_template_store()
    try:
        deleted = store.delete_template(name)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Template '{name}' not found")
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "message": f"Template '{name}' deleted successfully",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error("deleting template", e) from e


@router.post("/api/v1/jobs/from-template/{name}")
async def create_job_from_template(name: str, request: JobFromTemplateRequest) -> Response:
    """Create a job from a template with parameter substitution."""
    processor = _check_processor()
    store = _get_template_store()

    template = store.get_template(name)
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{name}' not found")

    _check_namespace(request.namespace, processor)

    try:
        parameters = {**template.get("parameters", {}), **(request.parameters or {})}
        parameters["name"] = request.name

        manifest = _apply_template_parameters(template["manifest"], parameters)

        if "metadata" not in manifest:
            manifest["metadata"] = {}
        manifest["metadata"]["namespace"] = request.namespace
        manifest["metadata"]["name"] = request.name

        if "labels" not in manifest["metadata"]:
            manifest["metadata"]["labels"] = {}
        manifest["metadata"]["labels"]["gco.io/template"] = name

        submission_request = ManifestSubmissionRequest(
            manifests=[manifest], namespace=request.namespace, dry_run=False, validate=True
        )

        result = await processor.process_manifest_submission(submission_request)

        response = {
            "cluster_id": processor.cluster_id,
            "region": processor.region,
            "timestamp": datetime.now(UTC).isoformat(),
            "template": name,
            "job_name": request.name,
            "namespace": request.namespace,
            "success": result.success,
            "parameters_applied": parameters,
            "errors": result.errors,
        }

        status_code = 201 if result.success else 400
        return JSONResponse(status_code=status_code, content=response)

    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error("creating job from template", e) from e
