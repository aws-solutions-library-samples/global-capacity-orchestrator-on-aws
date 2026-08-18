"""
DynamoDB-backed storage for job templates, webhooks, and job records.

This module provides persistent storage for:
- Job templates: Reusable job configurations with parameter substitution
- Webhooks: Event notification registrations
- Job records: Centralized job tracking with status updates

Tables are created in the global stack and accessed from all regional services.

Region Configuration:
    DynamoDB tables are deployed in the global region (e.g., us-east-2) but
    accessed from regional services (e.g., us-east-1). The region is determined
    by checking environment variables in this order:
    1. DYNAMODB_REGION - Explicitly set for DynamoDB access
    2. GLOBAL_REGION - The global stack's region
    3. AWS_REGION - Fallback to current region

Job Queue Architecture:
    1. Jobs are submitted to the jobs table with target_region and status="queued"
    2. Regional manifest processors poll for jobs targeting their region
    3. Processor claims job (status="claimed"), applies to K8s, updates status
    4. Status updates flow back to DynamoDB for global visibility
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import uuid
from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# <pyflowchart-code-diagram> BEGIN - auto-inserted, do not edit
# Generated at (UTC): 2026-08-14T03:46:22Z
# Flowchart(s) generated from this file:
#   * ``JobStore.claim_job`` -> ``diagrams/code_diagrams/gco/services/template_store.JobStore_claim_job.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/template_store.JobStore_claim_job.png``)
#   * ``JobStore.transition_job`` -> ``diagrams/code_diagrams/gco/services/template_store.JobStore_transition_job.html``
#     (PNG: ``diagrams/code_diagrams/gco/services/template_store.JobStore_transition_job.png``)
# Regenerate with ``python diagrams/code_diagrams/generate.py``.
# <pyflowchart-code-diagram> END


logger = logging.getLogger(__name__)

_DEFAULT_CLAIM_LEASE_SECONDS = 5 * 60
_MIN_CLAIM_LEASE_SECONDS = 30
_MAX_CLAIM_LEASE_SECONDS = 60 * 60
_MAX_LIST_EVALUATED_ITEMS = 20_000
_MAX_LEGACY_MIGRATION_EVALUATED_ITEMS = 1_000
_LEGACY_REGION_STATUS_INDEX = "region-status-index"
# One worker-facing GSI serves queue priority and lease recovery. Existing
# deployments gain only this index in the compatibility release because
# DynamoDB permits one GSI create/delete per table update.
_REGION_STATUS_WORK_INDEX = "region-status-work-index"
_TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _utc_now_iso() -> str:
    """Return current UTC time in ISO format with Z suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _claim_lease_expiry_iso(lease_seconds: int) -> str:
    """Return a bounded lease expiry for crash-safe regional claims."""
    return (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat().replace("+00:00", "Z")


class JobSubmissionConflict(RuntimeError):
    """A job ID or idempotency key was reused for a different submission."""


class JobStatus(StrEnum):
    """Job status values for the centralized job store."""

    QUEUED = "queued"  # Submitted, waiting for regional pickup
    CLAIMED = "claimed"  # Claimed by a regional processor
    APPLYING = "applying"  # Being applied to Kubernetes
    PENDING = "pending"  # Applied, waiting for pod scheduling
    RUNNING = "running"  # Pod(s) running
    SUCCEEDED = "succeeded"  # Job completed successfully
    FAILED = "failed"  # Job failed
    CANCELLED = "cancelled"  # Job was cancelled


_ALLOWED_JOB_TRANSITIONS: dict[str, frozenset[str]] = {
    JobStatus.QUEUED.value: frozenset({JobStatus.CLAIMED.value, JobStatus.CANCELLED.value}),
    JobStatus.CLAIMED.value: frozenset({JobStatus.APPLYING.value, JobStatus.FAILED.value}),
    JobStatus.APPLYING.value: frozenset({JobStatus.PENDING.value, JobStatus.FAILED.value}),
    JobStatus.PENDING.value: frozenset(
        {JobStatus.RUNNING.value, JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}
    ),
    JobStatus.RUNNING.value: frozenset({JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}),
    JobStatus.SUCCEEDED.value: frozenset(),
    JobStatus.FAILED.value: frozenset(),
    JobStatus.CANCELLED.value: frozenset(),
}


class TemplateStore:
    """DynamoDB-backed store for job templates."""

    def __init__(self, table_name: str | None = None, region: str | None = None):
        """Initialize the template store.

        Args:
            table_name: DynamoDB table name. Defaults to env var TEMPLATES_TABLE_NAME.
            region: AWS region for DynamoDB. Defaults to env var DYNAMODB_REGION,
                    then GLOBAL_REGION, then AWS_REGION.
        """
        self.table_name = table_name or os.getenv("TEMPLATES_TABLE_NAME", "gco-job-templates")
        # DynamoDB tables are in the global region, not the regional cluster region
        self.region = (
            region
            or os.getenv("DYNAMODB_REGION")
            or os.getenv("GLOBAL_REGION")
            or os.getenv("AWS_REGION", "us-east-1")
        )
        self._dynamodb = boto3.resource("dynamodb", region_name=self.region)
        self._table = self._dynamodb.Table(self.table_name)

    def list_templates(self) -> list[dict[str, Any]]:
        """List all templates."""
        try:
            response = self._table.scan(
                ProjectionExpression="template_name, description, created_at, updated_at"
            )
            items = response.get("Items", [])

            # Handle pagination
            while "LastEvaluatedKey" in response:
                response = self._table.scan(
                    ProjectionExpression="template_name, description, created_at, updated_at",
                    ExclusiveStartKey=response["LastEvaluatedKey"],
                )
                items.extend(response.get("Items", []))

            return [
                {
                    "name": item["template_name"],
                    "description": item.get("description"),
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
                for item in items
            ]
        except ClientError as e:
            logger.error(f"Failed to list templates: {e}")
            raise

    def get_template(self, name: str) -> dict[str, Any] | None:
        """Get a template by name."""
        try:
            response = self._table.get_item(Key={"template_name": name})
            item = response.get("Item")
            if not item:
                return None

            return {
                "name": item["template_name"],
                "description": item.get("description"),
                "manifest": json.loads(item["manifest"]),
                "parameters": json.loads(item.get("parameters", "{}")),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        except ClientError as e:
            logger.error(f"Failed to get template {name}: {e}")
            raise

    def create_template(
        self,
        name: str,
        manifest: dict[str, Any],
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a new template."""
        now = _utc_now_iso()

        item = {
            "template_name": name,
            "manifest": json.dumps(manifest),
            "parameters": json.dumps(parameters or {}),
            "created_at": now,
            "updated_at": now,
        }
        if description:
            item["description"] = description

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(template_name)",
            )
            return {
                "name": name,
                "description": description,
                "manifest": manifest,
                "parameters": parameters or {},
                "created_at": now,
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Template '{name}' already exists") from e
            logger.error(f"Failed to create template {name}: {e}")
            raise

    def update_template(
        self,
        name: str,
        manifest: dict[str, Any] | None = None,
        description: str | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Update an existing template."""
        now = _utc_now_iso()

        update_expr_parts = ["updated_at = :updated_at"]
        expr_values: dict[str, Any] = {":updated_at": now}

        if manifest is not None:
            update_expr_parts.append("manifest = :manifest")
            expr_values[":manifest"] = json.dumps(manifest)

        if description is not None:
            update_expr_parts.append("description = :description")
            expr_values[":description"] = description

        if parameters is not None:
            update_expr_parts.append("parameters = :parameters")
            expr_values[":parameters"] = json.dumps(parameters)

        try:
            response = self._table.update_item(
                Key={"template_name": name},
                UpdateExpression="SET " + ", ".join(update_expr_parts),
                ExpressionAttributeValues=expr_values,
                ConditionExpression="attribute_exists(template_name)",
                ReturnValues="ALL_NEW",
            )
            item = response.get("Attributes", {})
            return {
                "name": item["template_name"],
                "description": item.get("description"),
                "manifest": json.loads(item["manifest"]),
                "parameters": json.loads(item.get("parameters", "{}")),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            }
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            logger.error(f"Failed to update template {name}: {e}")
            raise

    def delete_template(self, name: str) -> bool:
        """Delete a template."""
        try:
            self._table.delete_item(
                Key={"template_name": name},
                ConditionExpression="attribute_exists(template_name)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            logger.error(f"Failed to delete template {name}: {e}")
            raise

    def template_exists(self, name: str) -> bool:
        """Check if a template exists."""
        try:
            response = self._table.get_item(
                Key={"template_name": name},
                ProjectionExpression="template_name",
            )
            return "Item" in response
        except ClientError as e:
            logger.error(f"Failed to check template existence {name}: {e}")
            raise


class WebhookStore:
    """DynamoDB-backed store for webhooks."""

    def __init__(self, table_name: str | None = None, region: str | None = None):
        """Initialize the webhook store.

        Args:
            table_name: DynamoDB table name. Defaults to env var WEBHOOKS_TABLE_NAME.
            region: AWS region for DynamoDB. Defaults to env var DYNAMODB_REGION,
                    then GLOBAL_REGION, then AWS_REGION.
        """
        self.table_name = table_name or os.getenv("WEBHOOKS_TABLE_NAME", "gco-webhooks")
        # DynamoDB tables are in the global region, not the regional cluster region
        self.region = (
            region
            or os.getenv("DYNAMODB_REGION")
            or os.getenv("GLOBAL_REGION")
            or os.getenv("AWS_REGION", "us-east-1")
        )
        self._dynamodb = boto3.resource("dynamodb", region_name=self.region)
        self._table = self._dynamodb.Table(self.table_name)

    def list_webhooks(
        self,
        namespace: str | None = None,
        *,
        include_secret: bool = False,
    ) -> list[dict[str, Any]]:
        """List webhooks, optionally filtered by namespace.

        Secrets are redacted by default because this method also backs the
        public list API. Internal delivery lookups explicitly opt in so HMAC
        signing still uses the configured secret.
        """
        try:
            if namespace:
                response = self._table.query(
                    IndexName="namespace-index",
                    KeyConditionExpression="namespace = :ns",
                    ExpressionAttributeValues={":ns": namespace},
                )
                items = response.get("Items", [])
            else:
                response = self._table.scan()
                items = response.get("Items", [])

                while "LastEvaluatedKey" in response:
                    response = self._table.scan(
                        ExclusiveStartKey=response["LastEvaluatedKey"],
                    )
                    items.extend(response.get("Items", []))

            webhooks: list[dict[str, Any]] = []
            for item in items:
                webhook = {
                    "id": item["webhook_id"],
                    "url": item["url"],
                    "events": json.loads(item.get("events", "[]")),
                    "namespace": item.get("namespace"),
                    "created_at": item.get("created_at"),
                }
                if include_secret and "secret" in item:
                    webhook["secret"] = item["secret"]
                webhooks.append(webhook)
            return webhooks
        except ClientError as e:
            logger.error(f"Failed to list webhooks: {e}")
            raise

    def get_webhook(self, webhook_id: str) -> dict[str, Any] | None:
        """Get a webhook by ID."""
        try:
            response = self._table.get_item(Key={"webhook_id": webhook_id})
            item = response.get("Item")
            if not item:
                return None

            return {
                "id": item["webhook_id"],
                "url": item["url"],
                "events": json.loads(item.get("events", "[]")),
                "namespace": item.get("namespace"),
                "secret": item.get("secret"),
                "created_at": item.get("created_at"),
            }
        except ClientError as e:
            logger.error(f"Failed to get webhook {webhook_id}: {e}")
            raise

    def create_webhook(
        self,
        webhook_id: str,
        url: str,
        events: list[str],
        namespace: str | None = None,
        secret: str | None = None,
    ) -> dict[str, Any]:
        """Create a new webhook."""
        now = _utc_now_iso()

        item: dict[str, Any] = {
            "webhook_id": webhook_id,
            "url": url,
            "events": json.dumps(events),
            "created_at": now,
        }
        if namespace:
            item["namespace"] = namespace
        if secret:
            item["secret"] = secret

        try:
            self._table.put_item(Item=item)
            return {
                "id": webhook_id,
                "url": url,
                "events": events,
                "namespace": namespace,
                "created_at": now,
            }
        except ClientError as e:
            logger.error(f"Failed to create webhook: {e}")
            raise

    def delete_webhook(self, webhook_id: str) -> bool:
        """Delete a webhook."""
        try:
            self._table.delete_item(
                Key={"webhook_id": webhook_id},
                ConditionExpression="attribute_exists(webhook_id)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            logger.error(f"Failed to delete webhook {webhook_id}: {e}")
            raise

    def get_webhooks_for_event(
        self, event: str, namespace: str | None = None
    ) -> list[dict[str, Any]]:
        """Get all webhooks subscribed to a specific event."""
        webhooks = self.list_webhooks(namespace=namespace, include_secret=True)
        return [w for w in webhooks if event in w.get("events", [])]


class JobStore:
    """DynamoDB-backed store for centralized job tracking.

    This store enables:
    - Global job submission with region targeting
    - Real-time status tracking across all regions
    - Job history and audit trail
    - Cross-region job queries without hitting K8s APIs
    """

    def __init__(
        self,
        table_name: str | None = None,
        region: str | None = None,
        claim_lease_seconds: int | None = None,
    ) -> None:
        """Initialize the store with bounded DynamoDB timeouts and claim leases."""
        self.table_name = table_name or os.getenv("JOBS_TABLE_NAME", "gco-jobs")
        self.region = (
            region
            or os.getenv("DYNAMODB_REGION")
            or os.getenv("GLOBAL_REGION")
            or os.getenv("AWS_REGION", "us-east-1")
        )
        configured_lease = claim_lease_seconds
        if configured_lease is None:
            try:
                configured_lease = int(
                    os.getenv("CENTRAL_QUEUE_LEASE_SECONDS", str(_DEFAULT_CLAIM_LEASE_SECONDS))
                )
            except ValueError:
                configured_lease = _DEFAULT_CLAIM_LEASE_SECONDS
        self.claim_lease_seconds = min(
            max(configured_lease, _MIN_CLAIM_LEASE_SECONDS),
            _MAX_CLAIM_LEASE_SECONDS,
        )
        self._dynamodb = boto3.resource(
            "dynamodb",
            region_name=self.region,
            config=Config(
                connect_timeout=3,
                read_timeout=10,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        self._table = self._dynamodb.Table(self.table_name)
        self._legacy_migration_cursors: dict[tuple[str, str], dict[str, Any]] = {}
        self._legacy_migration_completed_in_sweep: set[tuple[str, str]] = set()
        self._legacy_migration_next_status: dict[str, int] = {}

    @staticmethod
    def _is_conditional_failure(error: ClientError) -> bool:
        return bool(
            error.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
        )

    @staticmethod
    def _decode_json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except TypeError, ValueError:
                return default
        return value

    @classmethod
    def _history_with(
        cls,
        item: dict[str, Any],
        *,
        status: str,
        timestamp: str,
        message: str | None = None,
        error: str | None = None,
    ) -> str:
        history = cls._decode_json(item.get("status_history"), [])
        if not isinstance(history, list):
            history = []
        entry: dict[str, str] = {"status": status, "timestamp": timestamp}
        if message:
            entry["message"] = message
        if error:
            entry["error"] = error
        history.append(entry)
        return json.dumps(history, separators=(",", ":"))

    def _get_raw_job(self, job_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(Key={"job_id": job_id}, ConsistentRead=True)
        item = response.get("Item")
        return item if isinstance(item, dict) else None

    @staticmethod
    def _priority_sort_key(priority: int, submitted_at: str, job_id: str) -> str:
        """Sort higher priorities first and preserve FIFO order for ties."""
        return f"{100 - priority:03d}#{submitted_at}#{job_id}"

    @staticmethod
    def _region_status(region: str, status: str) -> str:
        return f"{region}#{status}"

    @staticmethod
    def _list_filter_identity(
        target_region: str | None,
        status: str | None,
        namespace: str | None,
    ) -> dict[str, str | None]:
        return {
            "target_region": target_region,
            "status": status,
            "namespace": namespace,
        }

    @classmethod
    def _encode_list_cursor(
        cls,
        key: dict[str, Any],
        filters: dict[str, str | None],
    ) -> str:
        payload = json.dumps(
            {"version": 1, "key": key, "filters": filters},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def _decode_list_cursor(
        cls,
        cursor: str,
        filters: dict[str, str | None],
    ) -> dict[str, Any]:
        if not cursor or len(cursor) > 2_048:
            raise ValueError("Invalid queue cursor")
        try:
            padding = "=" * (-len(cursor) % 4)
            payload = json.loads(base64.urlsafe_b64decode(cursor + padding))
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid queue cursor") from error
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise ValueError("Invalid queue cursor")
        if payload.get("filters") != filters:
            raise ValueError("Queue cursor does not match the requested filters")
        key = payload.get("key")
        if (
            not isinstance(key, dict)
            or set(key) != {"job_id"}
            or not isinstance(key.get("job_id"), str)
            or not key["job_id"]
        ):
            raise ValueError("Invalid queue cursor")
        return key

    @staticmethod
    def _legacy_priority(item: dict[str, Any]) -> int:
        value = item.get("priority", 0)
        try:
            priority = int(value) if not isinstance(value, bool) else 0
        except TypeError, ValueError:
            priority = 0
        return min(max(priority, 0), 100)

    @staticmethod
    def _migration_snapshot_conditions(
        item: dict[str, Any],
        fields: Collection[str],
        names: dict[str, str],
        values: dict[str, Any],
    ) -> list[str]:
        """Build optimistic-lock predicates for fields used by migration."""
        conditions: list[str] = []
        for index, field_name in enumerate(fields):
            name_token = f"#snapshot_{index}"
            names[name_token] = field_name
            if field_name in item:
                value_token = f":snapshot_{index}"
                values[value_token] = item[field_name]
                conditions.append(f"{name_token} = {value_token}")
            else:
                conditions.append(f"attribute_not_exists({name_token})")
        return conditions

    def _migrate_legacy_record(self, item: dict[str, Any], region: str, status: str) -> str:
        """Repair one old-writer record or fail it when adoption is unsafe.

        The worker reads through the legacy target-region/status index during a
        rolling upgrade, so every derived worker key may be missing *or stale*.
        Updates carry optimistic predicates for every source field used to
        derive those keys. A concurrent status transition, lease renewal, or
        identity repair therefore wins instead of being overwritten by this
        migration's older snapshot.
        """
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            logger.error("Ignoring legacy queue record without a job_id")
            return "skipped"

        priority = self._legacy_priority(item)
        submitted_at = str(item.get("submitted_at") or item.get("updated_at") or "")
        priority_sort = self._priority_sort_key(priority, submitted_at, job_id)
        snapshot_fields = ["priority", "submitted_at", "updated_at"]

        unsafe_reason: str | None = None
        if status in {JobStatus.CLAIMED.value, JobStatus.APPLYING.value}:
            lease_fields = (
                "claimed_by",
                "claim_token",
                "claim_generation",
                "lease_expires_at",
            )
            snapshot_fields.extend(lease_fields)
            try:
                generation = int(item.get("claim_generation", 0))
            except TypeError, ValueError:
                generation = 0
            if not (
                item.get("claimed_by")
                and item.get("claim_token")
                and generation > 0
                and item.get("lease_expires_at")
            ):
                unsafe_reason = (
                    "Pre-upgrade transient queue record lacks complete lease fencing and "
                    "cannot be safely replayed"
                )
        elif status in {JobStatus.PENDING.value, JobStatus.RUNNING.value}:
            identity_fields = ("k8s_job_name", "k8s_job_namespace", "k8s_job_uid")
            snapshot_fields.extend(identity_fields)
            if not all(item.get(field) for field in identity_fields):
                unsafe_reason = (
                    "Pre-upgrade active queue record lacks deterministic Kubernetes identity and "
                    "cannot be safely adopted"
                )

        if unsafe_reason is None and status in {
            JobStatus.CLAIMED.value,
            JobStatus.APPLYING.value,
        }:
            work_sort = str(item["lease_expires_at"])
        else:
            work_sort = priority_sort
        expected_region_status = self._region_status(region, status)

        if unsafe_reason is None and (
            item.get("region_status") == expected_region_status
            and item.get("priority_sort") == priority_sort
            and item.get("work_sort") == work_sort
        ):
            return "skipped"

        values: dict[str, Any] = {
            ":expected": status,
            ":target_region": region,
            ":priority_sort": priority_sort,
            ":work_sort": work_sort,
        }
        names = {"#status": "status"}
        conditions = [
            "attribute_exists(job_id)",
            "target_region = :target_region",
            "#status = :expected",
            *self._migration_snapshot_conditions(item, snapshot_fields, names, values),
        ]
        if unsafe_reason is None:
            values[":region_status"] = expected_region_status
            conditions.append(
                "(attribute_not_exists(region_status) OR "
                "attribute_not_exists(priority_sort) OR "
                "attribute_not_exists(work_sort) OR "
                "region_status <> :region_status OR "
                "priority_sort <> :priority_sort OR work_sort <> :work_sort)"
            )
            update_expression = (
                "SET region_status = :region_status, priority_sort = :priority_sort, "
                "work_sort = :work_sort"
            )
            outcome = "migrated"
        else:
            now = _utc_now_iso()
            update_expression = (
                "SET #status = :failed, region_status = :region_status, "
                "priority_sort = :priority_sort, work_sort = :work_sort, "
                "updated_at = :now, completed_at = :now, "
                "error_message = :error, status_history = :history "
                "REMOVE claimed_by, claim_token, lease_expires_at"
            )
            values.update(
                {
                    ":failed": JobStatus.FAILED.value,
                    ":region_status": self._region_status(region, JobStatus.FAILED.value),
                    ":now": now,
                    ":error": unsafe_reason,
                    ":history": self._history_with(
                        item,
                        status=JobStatus.FAILED.value,
                        timestamp=now,
                        message="Record fenced during queue schema migration",
                        error=unsafe_reason,
                    ),
                }
            )
            outcome = "failed"

        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=update_expression,
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
            return outcome
        except ClientError as error:
            if self._is_conditional_failure(error):
                return "skipped"
            raise

    def migrate_legacy_records_for_region(
        self,
        region: str,
        evaluation_limit: int = _MAX_LEGACY_MIGRATION_EVALUATED_ITEMS,
    ) -> dict[str, int | bool]:
        """Incrementally repair records written by pre-work-index workers.

        Every bounded invocation reserves a fair share for each unfinished
        status partition instead of allowing a large queued backlog to starve
        lease recovery and active-job reconciliation. The starting partition
        rotates when a budget is smaller than the number of statuses. Completed
        sweeps reset so a mixed-version worker's later write is repaired on a
        subsequent pass.
        """
        budget = min(max(int(evaluation_limit), 1), 10_000)
        statuses = (
            JobStatus.QUEUED.value,
            JobStatus.CLAIMED.value,
            JobStatus.APPLYING.value,
            JobStatus.PENDING.value,
            JobStatus.RUNNING.value,
        )
        sweep_keys = {(region, status) for status in statuses}
        completed_in_sweep = self._legacy_migration_completed_in_sweep
        stats: dict[str, int | bool] = {
            "evaluated": 0,
            "migrated": 0,
            "failed": 0,
            "complete": False,
        }

        start = self._legacy_migration_next_status.get(region, 0) % len(statuses)
        ordered_statuses = statuses[start:] + statuses[:start]
        attempted: list[str] = []
        for position, status in enumerate(ordered_statuses):
            if int(stats["evaluated"]) >= budget:
                break
            migration_key = (region, status)
            if migration_key in completed_in_sweep:
                continue

            unfinished = sum(
                (region, candidate) not in completed_in_sweep
                for candidate in ordered_statuses[position:]
            )
            status_budget = max(
                1,
                (budget - int(stats["evaluated"]) + unfinished - 1) // unfinished,
            )
            status_evaluated = 0
            attempted.append(status)
            while int(stats["evaluated"]) < budget and status_evaluated < status_budget:
                remaining = min(
                    budget - int(stats["evaluated"]),
                    status_budget - status_evaluated,
                    100,
                )
                kwargs: dict[str, Any] = {
                    "IndexName": _LEGACY_REGION_STATUS_INDEX,
                    "KeyConditionExpression": (
                        "target_region = :target_region AND #status = :status"
                    ),
                    "ExpressionAttributeNames": {"#status": "status"},
                    "ExpressionAttributeValues": {
                        ":target_region": region,
                        ":status": status,
                    },
                    "Limit": remaining,
                }
                cursor = self._legacy_migration_cursors.get(migration_key)
                if cursor:
                    kwargs["ExclusiveStartKey"] = cursor
                response = self._table.query(**kwargs)
                items = response.get("Items", [])
                scanned = int(response.get("ScannedCount", 0))
                if scanned <= 0 and items:
                    scanned = len(items)
                stats["evaluated"] = int(stats["evaluated"]) + scanned
                status_evaluated += scanned
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    outcome = self._migrate_legacy_record(item, region, status)
                    if outcome in {"migrated", "failed"}:
                        stats[outcome] = int(stats[outcome]) + 1

                next_cursor = response.get("LastEvaluatedKey")
                if not isinstance(next_cursor, dict) or not next_cursor:
                    completed_in_sweep.add(migration_key)
                    self._legacy_migration_cursors.pop(migration_key, None)
                    break
                self._legacy_migration_cursors[migration_key] = next_cursor
                if scanned <= 0:
                    break

        if attempted:
            self._legacy_migration_next_status[region] = (statuses.index(attempted[-1]) + 1) % len(
                statuses
            )

        sweep_complete = sweep_keys.issubset(completed_in_sweep)
        if sweep_complete:
            completed_in_sweep.difference_update(sweep_keys)
            self._legacy_migration_next_status.pop(region, None)
        stats["complete"] = sweep_complete
        return stats

    def _query_worker_index(
        self,
        *,
        index_name: str,
        region: str,
        status: str,
        limit: int,
        range_attribute: str | None = None,
        upper_bound: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read one worker index partition with correct DynamoDB pagination."""
        items: list[dict[str, Any]] = []
        exclusive_start_key: dict[str, Any] | None = None
        while len(items) < limit:
            key_condition = "region_status = :region_status"
            values = {":region_status": self._region_status(region, status)}
            if range_attribute is not None:
                assert upper_bound is not None
                key_condition += f" AND {range_attribute} <= :upper_bound"
                values[":upper_bound"] = upper_bound
            kwargs: dict[str, Any] = {
                "IndexName": index_name,
                "KeyConditionExpression": key_condition,
                "ExpressionAttributeValues": values,
                "Limit": limit - len(items),
                "ScanIndexForward": True,
            }
            if exclusive_start_key:
                kwargs["ExclusiveStartKey"] = exclusive_start_key
            response = self._table.query(**kwargs)
            items.extend(item for item in response.get("Items", []) if isinstance(item, dict))
            exclusive_start_key = response.get("LastEvaluatedKey")
            if not exclusive_start_key:
                break
        return items[:limit]

    def _query_region_status(
        self,
        region: str,
        status: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read the unified worker index in priority order."""
        pages = (
            self._query_worker_index(
                index_name=_REGION_STATUS_WORK_INDEX,
                region=region,
                status=status,
                limit=limit,
            ),
        )
        items_by_job_id: dict[str, dict[str, Any]] = {}
        for page in pages:
            for item in page:
                job_id = item.get("job_id")
                if isinstance(job_id, str) and job_id:
                    items_by_job_id.setdefault(job_id, item)

        def priority_order(item: dict[str, Any]) -> tuple[str, str]:
            job_id = str(item.get("job_id") or "")
            priority_sort = item.get("priority_sort")
            if not isinstance(priority_sort, str) or not priority_sort:
                priority_sort = self._priority_sort_key(
                    self._legacy_priority(item),
                    str(item.get("submitted_at") or item.get("updated_at") or ""),
                    job_id,
                )
            return priority_sort, job_id

        return sorted(items_by_job_id.values(), key=priority_order)[:limit]

    def _query_expired_claims(
        self,
        region: str,
        status: str,
        expires_at_or_before: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Read expired claims from the unified worker index."""
        pages = (
            self._query_worker_index(
                index_name=_REGION_STATUS_WORK_INDEX,
                region=region,
                status=status,
                limit=limit,
                range_attribute="work_sort",
                upper_bound=expires_at_or_before,
            ),
        )
        items_by_job_id: dict[str, dict[str, Any]] = {}
        for page in pages:
            for item in page:
                job_id = item.get("job_id")
                if not isinstance(job_id, str) or not job_id:
                    continue
                existing = items_by_job_id.get(job_id)
                if existing is None or str(item.get("lease_expires_at") or "") > str(
                    existing.get("lease_expires_at") or ""
                ):
                    # Keep the newest value if a malformed/mock page repeats a
                    # job. Real GSI query pages contain one projection per key.
                    items_by_job_id[job_id] = item
        return sorted(
            items_by_job_id.values(),
            key=lambda item: (
                str(item.get("lease_expires_at") or ""),
                str(item.get("job_id") or ""),
            ),
        )[:limit]

    def submit_job(
        self,
        job_id: str,
        manifest: dict[str, Any],
        target_region: str,
        namespace: str = "gco-jobs",
        priority: int = 0,
        labels: dict[str, str] | None = None,
        submitted_by: str | None = None,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        spot_max_price: str | None = None,
        spot_instance_type: str | None = None,
    ) -> dict[str, Any]:
        """Submit a job exactly once, replaying only identical idempotent requests.

        ``spot_max_price`` (USD/hour, serialized as a string to avoid float
        items in DynamoDB) and ``spot_instance_type`` together form the
        optional spot price gate: the regional queue worker will not dispatch
        the job until the instance type's current spot price in the target
        region drops to or below the threshold.
        """
        now = _utc_now_iso()
        job_name = manifest.get("metadata", {}).get("name", job_id)
        priority_sort = self._priority_sort_key(priority, now, job_id)
        item: dict[str, Any] = {
            "job_id": job_id,
            "job_name": job_name,
            "target_region": target_region,
            "namespace": namespace,
            "status": JobStatus.QUEUED.value,
            "region_status": self._region_status(target_region, JobStatus.QUEUED.value),
            "priority": priority,
            "priority_sort": priority_sort,
            "work_sort": priority_sort,
            "manifest": json.dumps(manifest, separators=(",", ":"), sort_keys=True),
            "submitted_at": now,
            "updated_at": now,
            "claim_generation": 0,
            "status_history": json.dumps(
                [{"status": JobStatus.QUEUED.value, "timestamp": now, "message": "Job submitted"}],
                separators=(",", ":"),
            ),
        }
        if labels:
            item["labels"] = json.dumps(labels, separators=(",", ":"), sort_keys=True)
        if submitted_by:
            item["submitted_by"] = submitted_by
        if idempotency_key:
            item["idempotency_key"] = idempotency_key
            item["request_hash"] = request_hash or ""
        if spot_max_price and spot_instance_type:
            item["spot_max_price"] = spot_max_price
            item["spot_instance_type"] = spot_instance_type

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(job_id)",
            )
            return self._parse_job_item(item)
        except ClientError as error:
            if not self._is_conditional_failure(error):
                logger.error("Failed to submit job %s: %s", job_id, error)
                raise

        existing = self._get_raw_job(job_id)
        if (
            idempotency_key
            and existing
            and existing.get("idempotency_key") == idempotency_key
            and existing.get("request_hash") == (request_hash or "")
        ):
            replay = self._parse_job_item(existing)
            replay["idempotent_replay"] = True
            return replay
        raise JobSubmissionConflict("job ID or idempotency key is already in use")

    def claim_job(
        self,
        job_id: str,
        target_region: str,
        claimed_by: str,
    ) -> dict[str, Any] | None:
        """Claim a queued job with a unique token and monotonic fencing generation."""
        item = self._get_raw_job(job_id)
        if (
            item is None
            or item.get("status") != JobStatus.QUEUED.value
            or item.get("target_region") != target_region
        ):
            return None

        now = _utc_now_iso()
        lease_expires_at = _claim_lease_expiry_iso(self.claim_lease_seconds)
        claim_token = uuid.uuid4().hex
        generation = int(item.get("claim_generation", 0)) + 1
        history = self._history_with(
            item,
            status=JobStatus.CLAIMED.value,
            timestamp=now,
            message=f"Claimed by {claimed_by}",
        )
        try:
            response = self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #status = :claimed, region_status = :region_status, "
                    "claimed_by = :claimed_by, claim_token = :claim_token, "
                    "claim_generation = :generation, claimed_at = :now, "
                    "updated_at = :now, lease_expires_at = :lease_expires_at, "
                    "work_sort = :work_sort, status_history = :history"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND #status = :queued AND "
                    "target_region = :target_region AND updated_at = :expected_updated_at"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":claimed": JobStatus.CLAIMED.value,
                    ":queued": JobStatus.QUEUED.value,
                    ":region_status": self._region_status(target_region, JobStatus.CLAIMED.value),
                    ":target_region": target_region,
                    ":claimed_by": claimed_by,
                    ":claim_token": claim_token,
                    ":generation": generation,
                    ":now": now,
                    ":expected_updated_at": item.get("updated_at"),
                    ":lease_expires_at": lease_expires_at,
                    ":work_sort": lease_expires_at,
                    ":history": history,
                },
                ReturnValues="ALL_NEW",
            )
            return self._parse_job_item(response.get("Attributes", {}), include_internal=True)
        except ClientError as error:
            if self._is_conditional_failure(error):
                return None
            logger.error("Failed to claim job %s: %s", job_id, error)
            raise

    def renew_claim(
        self,
        job_id: str,
        target_region: str,
        claimed_by: str,
        claim_token: str,
        claim_generation: int,
    ) -> bool:
        """Renew an unexpired claim; an expired or fenced owner cannot regain it."""
        now = _utc_now_iso()
        lease_expires_at = _claim_lease_expiry_iso(self.claim_lease_seconds)
        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET lease_expires_at = :lease_expires_at, work_sort = :work_sort, "
                    "lease_renewed_at = :now"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND target_region = :target_region AND "
                    "#status IN (:claimed, :applying) AND claimed_by = :claimed_by AND "
                    "claim_token = :claim_token AND claim_generation = :generation AND "
                    "lease_expires_at > :now"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":target_region": target_region,
                    ":claimed": JobStatus.CLAIMED.value,
                    ":applying": JobStatus.APPLYING.value,
                    ":claimed_by": claimed_by,
                    ":claim_token": claim_token,
                    ":generation": claim_generation,
                    ":now": now,
                    ":lease_expires_at": lease_expires_at,
                    ":work_sort": lease_expires_at,
                },
            )
            return True
        except ClientError as error:
            if self._is_conditional_failure(error):
                return False
            logger.error("Failed to renew claim for job %s: %s", job_id, error)
            raise

    def transition_job(
        self,
        job_id: str,
        *,
        target_region: str,
        expected_status: JobStatus | str,
        status: JobStatus | str,
        message: str | None = None,
        error: str | None = None,
        k8s_job_name: str | None = None,
        k8s_job_namespace: str | None = None,
        k8s_job_uid: str | None = None,
        claimed_by: str | None = None,
        claim_token: str | None = None,
        claim_generation: int | None = None,
        expected_k8s_uid: str | None = None,
        workload_not_created: bool | None = None,
    ) -> dict[str, Any] | None:
        """Apply one fenced compare-and-set lifecycle transition.

        ``None`` means another actor won the race or the caller lost its lease.
        Terminal records are immutable because the transition matrix has no
        outgoing terminal edges.
        """
        expected = (
            expected_status.value if isinstance(expected_status, JobStatus) else expected_status
        )
        destination = status.value if isinstance(status, JobStatus) else status
        if destination not in _ALLOWED_JOB_TRANSITIONS.get(expected, frozenset()):
            raise ValueError(f"Invalid job transition: {expected} -> {destination}")
        if workload_not_created is not None:
            if workload_not_created is not True:
                raise ValueError("workload_not_created proof must be exactly true")
            if expected != JobStatus.APPLYING.value:
                raise ValueError("workload_not_created proof is valid only from the applying state")
            if destination != JobStatus.FAILED.value:
                raise ValueError("workload_not_created proof is valid only for failed jobs")
            if any((k8s_job_name, k8s_job_namespace, k8s_job_uid)):
                raise ValueError("workload_not_created proof cannot accompany Kubernetes identity")

        item = self._get_raw_job(job_id)
        if (
            item is None
            or item.get("status") != expected
            or item.get("target_region") != target_region
        ):
            return None
        if workload_not_created is True and any(
            attribute in item for attribute in ("k8s_job_name", "k8s_job_namespace", "k8s_job_uid")
        ):
            raise ValueError(
                "workload_not_created proof requires a record without Kubernetes identity"
            )

        claim_is_required = expected in {JobStatus.CLAIMED.value, JobStatus.APPLYING.value}
        if claim_is_required:
            if claimed_by is None or claim_token is None or claim_generation is None:
                raise ValueError(f"Transition from {expected} requires complete claim fencing")
            if (
                item.get("claimed_by") != claimed_by
                or item.get("claim_token") != claim_token
                or int(item.get("claim_generation", -1)) != claim_generation
            ):
                return None
        if expected_k8s_uid is not None and str(item.get("k8s_job_uid") or "") != str(
            expected_k8s_uid
        ):
            return None

        now = _utc_now_iso()
        priority_sort = str(
            item.get("priority_sort")
            or self._priority_sort_key(
                self._legacy_priority(item),
                str(item.get("submitted_at") or item.get("updated_at") or now),
                job_id,
            )
        )
        work_sort = (
            str(item.get("lease_expires_at") or priority_sort)
            if destination in {JobStatus.CLAIMED.value, JobStatus.APPLYING.value}
            else priority_sort
        )
        update_parts = [
            "#status = :destination",
            "region_status = :region_status",
            "priority_sort = :priority_sort",
            "work_sort = :work_sort",
            "updated_at = :now",
            "status_history = :history",
        ]
        remove_parts: list[str] = []
        values: dict[str, Any] = {
            ":destination": destination,
            ":expected": expected,
            ":region_status": self._region_status(target_region, destination),
            ":priority_sort": priority_sort,
            ":work_sort": work_sort,
            ":target_region": target_region,
            ":now": now,
            ":expected_updated_at": item.get("updated_at"),
            ":history": self._history_with(
                item,
                status=destination,
                timestamp=now,
                message=message,
                error=error,
            ),
        }
        conditions = [
            "attribute_exists(job_id)",
            "#status = :expected",
            "target_region = :target_region",
            "updated_at = :expected_updated_at",
        ]

        if claim_is_required:
            conditions.extend(
                [
                    "claimed_by = :claimed_by",
                    "claim_token = :claim_token",
                    "claim_generation = :generation",
                    "lease_expires_at > :now",
                ]
            )
            values.update(
                {
                    ":claimed_by": claimed_by,
                    ":claim_token": claim_token,
                    ":generation": claim_generation,
                }
            )
        if expected_k8s_uid is not None:
            conditions.append("k8s_job_uid = :expected_k8s_uid")
            values[":expected_k8s_uid"] = expected_k8s_uid
        if workload_not_created is True:
            update_parts.append("workload_not_created = :workload_not_created")
            values[":workload_not_created"] = True
            conditions.extend(
                [
                    "attribute_not_exists(workload_not_created)",
                    "attribute_not_exists(k8s_job_name)",
                    "attribute_not_exists(k8s_job_namespace)",
                    "attribute_not_exists(k8s_job_uid)",
                ]
            )

        for attribute, value, placeholder in (
            ("k8s_job_name", k8s_job_name, ":k8s_job_name"),
            ("k8s_job_namespace", k8s_job_namespace, ":k8s_job_namespace"),
            ("k8s_job_uid", k8s_job_uid, ":k8s_job_uid"),
        ):
            if value:
                update_parts.append(f"{attribute} = {placeholder}")
                values[placeholder] = value

        if error:
            update_parts.append("error_message = :error")
            values[":error"] = error
        elif destination != JobStatus.FAILED.value:
            remove_parts.append("error_message")

        if destination in _TERMINAL_JOB_STATUSES:
            update_parts.append("completed_at = :now")
        if destination not in {JobStatus.CLAIMED.value, JobStatus.APPLYING.value}:
            remove_parts.extend(["claimed_by", "claim_token", "lease_expires_at"])

        update_expression = "SET " + ", ".join(update_parts)
        if remove_parts:
            update_expression += " REMOVE " + ", ".join(dict.fromkeys(remove_parts))

        try:
            response = self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=update_expression,
                ConditionExpression=" AND ".join(conditions),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            return self._parse_job_item(response.get("Attributes", {}))
        except ClientError as transition_error:
            if self._is_conditional_failure(transition_error):
                return None
            logger.error("Failed to transition job %s: %s", job_id, transition_error)
            raise

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Get a job by ID."""
        try:
            response = self._table.get_item(Key={"job_id": job_id})
            item = response.get("Item")
            if not item:
                return None
            return self._parse_job_item(item)
        except ClientError as e:
            logger.error(f"Failed to get job {job_id}: {e}")
            raise

    def list_jobs_page(
        self,
        target_region: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        """Return one bounded scan page plus an opaque continuation cursor."""
        limit = min(max(int(limit), 1), 1_000)
        filters = self._list_filter_identity(target_region, status, namespace)
        filter_parts: list[str] = []
        values: dict[str, Any] = {}
        names: dict[str, str] = {}
        if target_region:
            filter_parts.append("target_region = :region")
            values[":region"] = target_region
        if status:
            filter_parts.append("#status = :status")
            values[":status"] = status
            names["#status"] = "status"
        if namespace:
            filter_parts.append("#namespace = :namespace")
            values[":namespace"] = namespace
            names["#namespace"] = "namespace"

        items: list[dict[str, Any]] = []
        evaluated = 0
        exclusive_start_key = self._decode_list_cursor(cursor, filters) if cursor else None
        next_key: dict[str, Any] | None = None
        partial = False
        try:
            while len(items) < limit and evaluated < _MAX_LIST_EVALUATED_ITEMS:
                page_budget = min(
                    max((limit - len(items)) * 4, 100),
                    _MAX_LIST_EVALUATED_ITEMS - evaluated,
                )
                kwargs: dict[str, Any] = {"Limit": page_budget}
                if filter_parts:
                    kwargs["FilterExpression"] = " AND ".join(filter_parts)
                    kwargs["ExpressionAttributeValues"] = values
                if names:
                    kwargs["ExpressionAttributeNames"] = names
                if exclusive_start_key:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key
                response = self._table.scan(**kwargs)
                page = [item for item in response.get("Items", []) if isinstance(item, dict)]
                remaining = limit - len(items)
                selected = page[:remaining]
                items.extend(selected)
                evaluated += int(response.get("ScannedCount", page_budget))

                if len(page) > remaining and selected:
                    last_job_id = selected[-1].get("job_id")
                    if isinstance(last_job_id, str) and last_job_id:
                        next_key = {"job_id": last_job_id}
                    else:
                        next_key = response.get("LastEvaluatedKey")
                    break

                response_key = response.get("LastEvaluatedKey")
                if not isinstance(response_key, dict) or not response_key:
                    next_key = None
                    break
                next_key = response_key
                exclusive_start_key = response_key
        except ClientError as error:
            logger.error("Failed to list jobs: %s", error)
            raise

        if next_key and evaluated >= _MAX_LIST_EVALUATED_ITEMS:
            partial = True
            logger.warning(
                "Job listing reached the %d-item evaluation budget before exhausting the table",
                _MAX_LIST_EVALUATED_ITEMS,
            )
        parsed = [self._parse_job_item(item) for item in items]
        parsed.sort(key=lambda job: job.get("submitted_at") or "", reverse=True)
        next_cursor = self._encode_list_cursor(next_key, filters) if next_key else None
        return parsed, next_cursor, partial

    def list_jobs(
        self,
        target_region: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List the first bounded page of matching jobs."""
        jobs, _, _ = self.list_jobs_page(
            target_region=target_region,
            status=status,
            namespace=namespace,
            limit=limit,
        )
        return jobs

    def get_queued_jobs_for_region(self, region: str, limit: int = 10) -> list[dict[str, Any]]:
        """Return the highest-priority queued jobs, FIFO within equal priority."""
        try:
            items = self._query_region_status(region, JobStatus.QUEUED.value, limit)
            return [self._parse_job_item(item) for item in items]
        except ClientError as error:
            logger.error("Failed to get queued jobs for %s: %s", region, error)
            raise

    def record_spot_gate_observation(
        self,
        job_id: str,
        *,
        observed_price: str,
        checked_at: str | None = None,
    ) -> bool:
        """Persist a spot gate observation on a still-queued job.

        Deliberately leaves ``updated_at`` untouched: ``claim_job`` fences on
        ``updated_at``, and a gate observation must never invalidate a
        concurrent claim attempt or count as queue-state churn. Conditional on
        the job still being queued so a late observation cannot decorate a
        claimed/terminal record. Returns whether the write happened.
        """
        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET spot_gate_checked_at = :checked_at, "
                    "spot_gate_observed_price = :observed_price"
                ),
                ConditionExpression="attribute_exists(job_id) AND #status = :queued",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":checked_at": checked_at or _utc_now_iso(),
                    ":observed_price": observed_price,
                    ":queued": JobStatus.QUEUED.value,
                },
            )
            return True
        except ClientError as error:
            if self._is_conditional_failure(error):
                return False
            logger.error("Failed to record spot gate observation for %s: %s", job_id, error)
            raise

    def get_active_jobs_for_region(self, region: str, limit: int = 100) -> list[dict[str, Any]]:
        """Return a total-bounded, fair sample of pending and running jobs."""
        jobs: list[dict[str, Any]] = []
        remaining = limit
        statuses = (JobStatus.RUNNING.value, JobStatus.PENDING.value)
        try:
            for index, status in enumerate(statuses):
                statuses_left = len(statuses) - index
                allocation = remaining if statuses_left == 1 else max(1, remaining // statuses_left)
                page = self._query_region_status(region, status, allocation)
                jobs.extend(self._parse_job_item(item) for item in page)
                remaining -= len(page)
                if remaining <= 0:
                    break
        except ClientError as error:
            logger.error("Failed to get active jobs for %s: %s", region, error)
            raise
        return jobs[:limit]

    def requeue_expired_jobs(self, region: str, limit: int = 100) -> int:
        """Fence expired claims and return them to the queue for deterministic adoption."""
        now = _utc_now_iso()
        candidates: list[dict[str, Any]] = []
        remaining = limit
        statuses = (JobStatus.CLAIMED.value, JobStatus.APPLYING.value)
        try:
            for index, status in enumerate(statuses):
                statuses_left = len(statuses) - index
                allocation = remaining if statuses_left == 1 else max(1, remaining // statuses_left)
                page = self._query_expired_claims(region, status, now, allocation)
                candidates.extend(page)
                remaining -= len(page)
                if remaining <= 0:
                    break
        except ClientError as error:
            logger.error("Failed to find expired jobs for %s: %s", region, error)
            raise

        candidates.sort(key=lambda item: str(item.get("lease_expires_at") or ""))
        recovered = 0
        for item in candidates:
            if recovered >= limit:
                break
            lease_expiry = item.get("lease_expires_at")
            if lease_expiry is None or str(lease_expiry) > now:
                continue
            job_id = item.get("job_id")
            owner = item.get("claimed_by")
            token = item.get("claim_token")
            generation = item.get("claim_generation")
            expected_status = item.get("status")
            expected_updated_at = item.get("updated_at")
            if not all(
                [job_id, owner, token, generation is not None, expected_status, expected_updated_at]
            ):
                logger.error("Refusing to recover unfenced queue record %s", job_id or "<missing>")
                continue

            history = self._history_with(
                item,
                status=JobStatus.QUEUED.value,
                timestamp=now,
                message="Expired worker claim fenced and recovered",
            )
            priority_sort = str(
                item.get("priority_sort")
                or self._priority_sort_key(
                    self._legacy_priority(item),
                    str(item.get("submitted_at") or item.get("updated_at") or now),
                    str(job_id),
                )
            )
            try:
                self._table.update_item(
                    Key={"job_id": job_id},
                    UpdateExpression=(
                        "SET #status = :queued, region_status = :region_status, "
                        "priority_sort = :priority_sort, work_sort = :priority_sort, "
                        "updated_at = :now, status_history = :history "
                        "REMOVE claimed_by, claim_token, lease_expires_at"
                    ),
                    ConditionExpression=(
                        "attribute_exists(job_id) AND #status = :expected AND "
                        "target_region = :region AND claimed_by = :owner AND "
                        "claim_token = :token AND claim_generation = :generation AND "
                        "updated_at = :expected_updated_at AND lease_expires_at <= :now"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":queued": JobStatus.QUEUED.value,
                        ":region_status": self._region_status(region, JobStatus.QUEUED.value),
                        ":priority_sort": priority_sort,
                        ":expected": expected_status,
                        ":region": region,
                        ":owner": owner,
                        ":token": token,
                        ":generation": generation,
                        ":expected_updated_at": expected_updated_at,
                        ":now": now,
                        ":history": history,
                    },
                )
            except ClientError as error:
                if self._is_conditional_failure(error):
                    continue
                logger.error("Failed to recover expired job %s: %s", job_id, error)
                raise
            recovered += 1
        return recovered

    def get_job_count_summary(
        self,
        max_evaluated: int = _MAX_LIST_EVALUATED_ITEMS,
    ) -> tuple[dict[str, dict[str, int]], int, bool]:
        """Return bounded region/status counts and whether the result is complete."""
        budget = min(max(int(max_evaluated), 1), 100_000)
        counts: dict[str, dict[str, int]] = {}
        evaluated = 0
        exclusive_start_key: dict[str, Any] | None = None
        truncated = False
        try:
            while evaluated < budget:
                kwargs: dict[str, Any] = {
                    "ProjectionExpression": "target_region, #status",
                    "ExpressionAttributeNames": {"#status": "status"},
                    "Limit": min(1_000, budget - evaluated),
                }
                if exclusive_start_key:
                    kwargs["ExclusiveStartKey"] = exclusive_start_key
                response = self._table.scan(**kwargs)
                page = response.get("Items", [])
                evaluated += int(response.get("ScannedCount", len(page)))
                for item in page:
                    if not isinstance(item, dict):
                        continue
                    region = str(item.get("target_region") or "unknown")
                    item_status = str(item.get("status") or "unknown")
                    region_counts = counts.setdefault(region, {})
                    region_counts[item_status] = region_counts.get(item_status, 0) + 1
                next_key = response.get("LastEvaluatedKey")
                if not isinstance(next_key, dict) or not next_key:
                    exclusive_start_key = None
                    break
                exclusive_start_key = next_key
            truncated = exclusive_start_key is not None
        except ClientError as error:
            logger.error("Failed to get job counts: %s", error)
            raise
        return counts, evaluated, truncated

    def get_job_counts_by_region(self) -> dict[str, dict[str, int]]:
        """Return bounded job counts; use ``get_job_count_summary`` for completeness metadata."""
        counts, _, truncated = self.get_job_count_summary()
        if truncated:
            logger.warning(
                "Queue statistics reached the %d-item evaluation budget and are partial",
                _MAX_LIST_EVALUATED_ITEMS,
            )
        return counts

    def cancel_job(self, job_id: str, reason: str | None = None) -> bool:
        """Cancel only an unclaimed queued job using the same atomic history CAS."""
        item = self._get_raw_job(job_id)
        if item is None or item.get("status") != JobStatus.QUEUED.value:
            return False
        now = _utc_now_iso()
        history = self._history_with(
            item,
            status=JobStatus.CANCELLED.value,
            timestamp=now,
            message=reason or "Cancelled by user",
        )
        try:
            self._table.update_item(
                Key={"job_id": job_id},
                UpdateExpression=(
                    "SET #status = :cancelled, region_status = :region_status, "
                    "updated_at = :now, completed_at = :now, cancelled_at = :now, "
                    "cancel_reason = :reason, status_history = :history"
                ),
                ConditionExpression=(
                    "attribute_exists(job_id) AND #status = :queued AND "
                    "target_region = :region AND updated_at = :expected_updated_at"
                ),
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":cancelled": JobStatus.CANCELLED.value,
                    ":queued": JobStatus.QUEUED.value,
                    ":region_status": self._region_status(
                        str(item.get("target_region")), JobStatus.CANCELLED.value
                    ),
                    ":region": item.get("target_region"),
                    ":expected_updated_at": item.get("updated_at"),
                    ":now": now,
                    ":reason": reason or "Cancelled by user",
                    ":history": history,
                },
            )
            return True
        except ClientError as error:
            if self._is_conditional_failure(error):
                return False
            logger.error("Failed to cancel job %s: %s", job_id, error)
            raise

    def _parse_job_item(
        self,
        item: dict[str, Any],
        *,
        include_internal: bool = False,
    ) -> dict[str, Any]:
        """Parse a DynamoDB item without exposing reusable claim tokens to APIs."""
        parsed = {
            "job_id": item.get("job_id"),
            "job_name": item.get("job_name"),
            "target_region": item.get("target_region"),
            "namespace": item.get("namespace"),
            "status": item.get("status"),
            "priority": int(item.get("priority", 0)),
            "manifest": self._decode_json(item.get("manifest"), {}),
            "labels": self._decode_json(item.get("labels"), {}),
            "submitted_at": item.get("submitted_at"),
            "submitted_by": item.get("submitted_by"),
            "claimed_by": item.get("claimed_by"),
            "claimed_at": item.get("claimed_at"),
            "claim_generation": int(item.get("claim_generation", 0)),
            "lease_expires_at": item.get("lease_expires_at"),
            "completed_at": item.get("completed_at"),
            "updated_at": item.get("updated_at"),
            "k8s_job_name": item.get("k8s_job_name"),
            "k8s_job_namespace": item.get("k8s_job_namespace"),
            "k8s_job_uid": item.get("k8s_job_uid"),
            "workload_not_created": item.get("workload_not_created"),
            "error_message": item.get("error_message"),
            "status_history": self._decode_json(item.get("status_history"), []),
        }
        # Optional spot price gate fields — present only for price-capped
        # jobs, so ungated records keep their historical shape.
        if item.get("spot_max_price") is not None:
            parsed["spot_max_price"] = str(item.get("spot_max_price"))
            parsed["spot_instance_type"] = item.get("spot_instance_type")
            parsed["spot_gate_checked_at"] = item.get("spot_gate_checked_at")
            observed = item.get("spot_gate_observed_price")
            parsed["spot_gate_observed_price"] = str(observed) if observed is not None else None
        if include_internal:
            parsed["claim_token"] = item.get("claim_token")
        return parsed


# Singleton instances for use in the API
_template_store: TemplateStore | None = None
_webhook_store: WebhookStore | None = None
_job_store: JobStore | None = None


def get_template_store() -> TemplateStore:
    """Get or create the template store singleton."""
    global _template_store
    if _template_store is None:
        _template_store = TemplateStore()
    return _template_store


def get_webhook_store() -> WebhookStore:
    """Get or create the webhook store singleton."""
    global _webhook_store
    if _webhook_store is None:
        _webhook_store = WebhookStore()
    return _webhook_store


def get_job_store() -> JobStore:
    """Get or create the job store singleton."""
    global _job_store
    if _job_store is None:
        _job_store = JobStore()
    return _job_store
