"""Webhook registration and management endpoints."""

from __future__ import annotations

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, Response

from gco.services.api_shared import WebhookRequest, internal_server_error
from gco.services.webhook_dispatcher import validate_webhook_url

if TYPE_CHECKING:
    from gco.services.template_store import WebhookStore

router = APIRouter(prefix="/api/v1/webhooks", tags=["Webhooks"])
logger = logging.getLogger(__name__)


def _get_webhook_store() -> WebhookStore:
    from gco.services.manifest_api import webhook_store

    if webhook_store is None:
        raise HTTPException(status_code=503, detail="Webhook store not initialized")
    return webhook_store


@router.get("")
async def list_webhooks(namespace: str | None = None) -> Response:
    """List all registered webhooks."""
    store = _get_webhook_store()
    try:
        webhooks_list = store.list_webhooks(namespace=namespace)
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "count": len(webhooks_list),
                "webhooks": webhooks_list,
            },
        )
    except Exception as e:
        raise internal_server_error("listing webhooks", e) from e


@router.post("")
async def create_webhook(request: WebhookRequest) -> Response:
    """Register a new webhook for job events."""
    store = _get_webhook_store()

    # Validate the URL at registration time so misconfigured webhooks are
    # rejected immediately instead of failing silently at every job event.
    # Uses the same validator as the delivery path (validate_webhook_url)
    # so the two never drift: HTTPS-only, no RFC1918/link-local/loopback,
    # optional allowed_domains from WEBHOOK_ALLOWED_DOMAINS env var.
    allowed_domains_str = os.getenv("WEBHOOK_ALLOWED_DOMAINS", "")
    allowed_domains = [d.strip() for d in allowed_domains_str.split(",") if d.strip()]
    is_valid, error = validate_webhook_url(request.url, allowed_domains=allowed_domains or None)
    if not is_valid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid webhook URL: {error}",
        )

    webhook_id = str(uuid.uuid4())[:8]

    try:
        webhook = store.create_webhook(
            webhook_id=webhook_id,
            url=request.url,
            events=[e.value for e in request.events],
            namespace=request.namespace,
            secret=request.secret,
        )
        return JSONResponse(
            status_code=201,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "message": "Webhook registered successfully",
                "webhook": webhook,
            },
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except Exception as e:
        raise internal_server_error("creating webhook", e) from e


@router.delete("/{webhook_id}")
async def delete_webhook(webhook_id: str) -> Response:
    """Delete a webhook."""
    store = _get_webhook_store()
    try:
        deleted = store.delete_webhook(webhook_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Webhook '{webhook_id}' not found")
        return JSONResponse(
            status_code=200,
            content={
                "timestamp": datetime.now(UTC).isoformat(),
                "message": f"Webhook '{webhook_id}' deleted successfully",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_server_error("deleting webhook", e) from e
