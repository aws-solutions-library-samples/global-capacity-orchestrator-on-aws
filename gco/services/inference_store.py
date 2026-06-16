"""
DynamoDB-backed store for inference endpoint state.

Provides CRUD operations for inference endpoints. The inference_monitor
in each regional cluster polls this table to reconcile desired state
with actual Kubernetes resources.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "gco-inference-endpoints"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class InferenceEndpointStore:
    """DynamoDB store for inference endpoint desired state."""

    def __init__(self, table_name: str | None = None, region: str | None = None):
        self.table_name = table_name or os.getenv(
            "INFERENCE_ENDPOINTS_TABLE_NAME", DEFAULT_TABLE_NAME
        )
        self._region = region or os.getenv("DYNAMODB_REGION") or os.getenv("REGION", "us-east-1")
        self._dynamodb = boto3.resource("dynamodb", region_name=self._region)
        self._table = self._dynamodb.Table(self.table_name)

    def create_endpoint(
        self,
        endpoint_name: str,
        spec: dict[str, Any],
        target_regions: list[str],
        namespace: str = "gco-inference",
        labels: dict[str, str] | None = None,
        created_by: str | None = None,
    ) -> dict[str, Any]:
        """Create a new inference endpoint entry."""
        now = _utc_now_iso()
        ingress_path = f"/inference/{endpoint_name}"

        item: dict[str, Any] = {
            "endpoint_name": endpoint_name,
            "desired_state": "deploying",
            "target_regions": target_regions,
            "namespace": namespace,
            "spec": _serialize_for_dynamo(spec),
            "ingress_path": ingress_path,
            "created_at": now,
            "updated_at": now,
            "region_status": {},
        }
        if labels:
            item["labels"] = labels
        if created_by:
            item["created_by"] = created_by

        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(endpoint_name)",
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ValueError(f"Endpoint '{endpoint_name}' already exists") from e
            raise

        return item

    def get_endpoint(self, endpoint_name: str) -> dict[str, Any] | None:
        """Get an endpoint by name."""
        response = self._table.get_item(Key={"endpoint_name": endpoint_name})
        item = response.get("Item")
        if item:
            return _deserialize_from_dynamo(item)
        return None

    def list_endpoints(
        self,
        desired_state: str | None = None,
        target_region: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all endpoints, optionally filtered."""
        response = self._table.scan()
        items = [_deserialize_from_dynamo(i) for i in response.get("Items", [])]

        if desired_state:
            items = [i for i in items if i.get("desired_state") == desired_state]
        if target_region:
            items = [i for i in items if target_region in i.get("target_regions", [])]

        return sorted(items, key=lambda x: x.get("created_at", ""), reverse=True)

    def update_desired_state(self, endpoint_name: str, desired_state: str) -> dict[str, Any] | None:
        """Update the desired state of an endpoint."""
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET desired_state = :s, updated_at = :u",
                ExpressionAttributeValues={
                    ":s": desired_state,
                    ":u": _utc_now_iso(),
                },
                ConditionExpression="attribute_exists(endpoint_name)",
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def update_spec(self, endpoint_name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Update the spec of an endpoint (triggers re-reconciliation)."""
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET spec = :s, updated_at = :u, desired_state = :ds",
                ExpressionAttributeValues={
                    ":s": _serialize_for_dynamo(spec),
                    ":u": _utc_now_iso(),
                    ":ds": "deploying",
                },
                ConditionExpression="attribute_exists(endpoint_name)",
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def update_region_status(
        self,
        endpoint_name: str,
        region: str,
        state: str,
        replicas_ready: int = 0,
        replicas_desired: int = 0,
        error: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Update the sync status for a specific region.

        ``extra`` carries optional, additive sub-status that a richer endpoint
        shape needs — for example a role-keyed breakdown of a split topology
        (``{"roles": {...}, "store": {...}}``). Its keys are merged into the
        stored status alongside the flat fields, so a consumer that only reads
        the flat shape is unaffected.
        """
        status_value: dict[str, Any] = {
            "state": state,
            "replicas_ready": replicas_ready,
            "replicas_desired": replicas_desired,
            "last_sync": _utc_now_iso(),
        }
        if error:
            status_value["error"] = error
        if extra:
            status_value.update(extra)

        try:
            self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET region_status.#r = :s, updated_at = :u",
                ExpressionAttributeNames={"#r": region},
                ExpressionAttributeValues={
                    ":s": status_value,
                    ":u": _utc_now_iso(),
                },
            )
        except ClientError as e:
            logger.error(
                "Failed to update region status for %s/%s: %s",
                endpoint_name,
                region,
                e,
            )

    def delete_endpoint(self, endpoint_name: str) -> bool:
        """Delete an endpoint record entirely."""
        try:
            self._table.delete_item(
                Key={"endpoint_name": endpoint_name},
                ConditionExpression="attribute_exists(endpoint_name)",
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def scale_endpoint(self, endpoint_name: str, replicas: int) -> dict[str, Any] | None:
        """Update the replica count in the spec."""
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET spec.replicas = :r, updated_at = :u",
                ExpressionAttributeValues={
                    ":r": replicas,
                    ":u": _utc_now_iso(),
                },
                ConditionExpression="attribute_exists(endpoint_name)",
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise


def _serialize_for_dynamo(obj: Any) -> Any:
    """Convert Python objects to DynamoDB-compatible types.

    Recurses through nested dicts and lists, so an arbitrarily deep
    configuration block carried on an endpoint spec (for example a nested
    topology/store/transfer block, including list-valued sub-fields) is
    converted in place rather than only at the top level.

    Type handling:
    - Integers are kept as integers, so whole-number counts are preserved.
    - Floats are rendered as their decimal-string form, avoiding binary-float
      rounding on store/reload.
    - Strings, booleans, and None pass through unchanged. Byte-size values
      authored as base-10 integer decimal strings therefore stay strings and
      are never routed through a float, so they reload exactly as written
      without float-to-Decimal coercion.
    """
    if isinstance(obj, dict):
        return {k: _serialize_for_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_for_dynamo(i) for i in obj]
    if isinstance(obj, (int, float)):
        return str(obj) if isinstance(obj, float) else obj
    return obj


def _deserialize_from_dynamo(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB item back to plain Python types.

    Recurses through nested dicts and lists to mirror the nested structure
    produced by :func:`_serialize_for_dynamo`. DynamoDB returns numbers as
    Decimal: whole values become int (so integer counts survive the
    round-trip) and fractional values become float. Strings are left
    untouched, so a byte-size value stored as a decimal string returns as the
    same string.
    """
    from decimal import Decimal

    def convert(v: Any) -> Any:
        if isinstance(v, Decimal):
            return int(v) if v == int(v) else float(v)
        if isinstance(v, dict):
            return {k: convert(val) for k, val in v.items()}
        if isinstance(v, list):
            return [convert(i) for i in v]
        return v

    result: dict[str, Any] = convert(item)
    return result


def get_inference_endpoint_store() -> InferenceEndpointStore:
    """Factory function for InferenceEndpointStore."""
    return InferenceEndpointStore()
