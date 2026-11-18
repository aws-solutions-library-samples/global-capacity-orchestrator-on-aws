"""
DynamoDB-backed store for inference endpoint state.

Provides lifecycle-fenced CRUD operations for inference endpoints. Regional
monitors use the immutable ``lifecycle_id`` and deletion generation to ensure
that stale writers cannot mutate a replacement endpoint or recreate a record
after terminal deletion.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

DEFAULT_TABLE_NAME = "gco-inference-endpoints"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_lifecycle_token() -> str:
    """Return a cryptographically random immutable lifecycle token."""
    return secrets.token_hex(32)


# The suffixes the inference monitor appends to an endpoint name to name the
# endpoint's Kubernetes objects (canary, Mooncake roles and proxy, the generated
# admin Secret, the ConfigMaps), and the prefix KEDA gives the HPAs it creates.
_ENDPOINT_OBJECT_SUFFIXES = (
    "-canary",
    "-prefill",
    "-decode",
    "-proxy",
    "-admin",
    "-mooncake",
    "-pd-proxy",
    "-tls-proxy",
)
_KEDA_HPA_PREFIX = "keda-hpa-"

# Services and pod ``app`` labels carry the role names, so ``<name>-prefill``,
# the longest of them, must fit in a 63-character DNS-1035 label.
ENDPOINT_NAME_MAX_LENGTH = 63 - len("-prefill")
_ENDPOINT_NAME_PATTERN = re.compile(r"[a-z](?:[-a-z0-9]*[a-z0-9])?")


def endpoint_object_names(endpoint_name: str) -> frozenset[str]:
    """Every Kubernetes object name the inference monitor derives from ``endpoint_name``."""
    return frozenset(
        {
            endpoint_name,
            *(endpoint_name + suffix for suffix in _ENDPOINT_OBJECT_SUFFIXES),
            *(
                f"{_KEDA_HPA_PREFIX}{endpoint_name}{suffix}"
                for suffix in ("", "-prefill", "-decode")
            ),
        }
    )


def validate_endpoint_name(endpoint_name: str) -> None:
    """Refuse a name the endpoint's Kubernetes objects cannot carry.

    Raises:
        ValueError: Unless the name is a DNS-1035 label (lowercase letters,
            digits and ``-``, starting with a letter, ending with a letter or
            digit) of at most :data:`ENDPOINT_NAME_MAX_LENGTH` characters.
    """
    if len(endpoint_name) > ENDPOINT_NAME_MAX_LENGTH or not _ENDPOINT_NAME_PATTERN.fullmatch(
        endpoint_name
    ):
        raise ValueError(
            f"Invalid endpoint name {endpoint_name!r}: use lowercase letters, digits and '-', "
            "starting with a letter and ending with a letter or digit, at most "
            f"{ENDPOINT_NAME_MAX_LENGTH} characters (so '<name>-prefill' fits a "
            "63-character Kubernetes name)"
        )


def check_endpoint_name_collisions(endpoint_name: str, existing_names: Iterable[str]) -> None:
    """Refuse a name whose Kubernetes objects would share a name with another endpoint's.

    ``llama-canary`` beside ``llama`` would both own the Deployment
    ``llama-canary``, and so would ``foo-proxy`` beside a Mooncake ``foo``; the
    check runs in both directions over every derived name. An existing record
    of the same name is left to :meth:`InferenceEndpointStore.create_endpoint`,
    which reports it as already existing.

    Raises:
        ValueError: Naming the other endpoint and one shared object name.
    """
    owned = endpoint_object_names(endpoint_name)
    for other in sorted(set(existing_names) - {endpoint_name}):
        shared = owned & endpoint_object_names(other)
        if shared:
            raise ValueError(
                f"Endpoint name {endpoint_name!r} collides with endpoint {other!r}: both would "
                f"own the Kubernetes object {min(shared)!r}. Pick a name that is not another "
                "endpoint's name plus one of the suffixes "
                f"{', '.join(_ENDPOINT_OBJECT_SUFFIXES)} (or the reverse)"
            )


def _validate_endpoint_spec(spec: dict[str, Any]) -> None:
    """Reject endpoint shapes the reconciler cannot safely materialize."""
    if not isinstance(spec, dict):
        raise ValueError("Endpoint spec must be a mapping")
    if "mooncake" in spec and "canary" in spec:
        raise ValueError("Endpoint spec cannot combine 'mooncake' and 'canary' blocks")


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
        """Create one endpoint incarnation with immutable lifecycle identity."""
        _validate_endpoint_spec(spec)
        now = _utc_now_iso()
        regions = list(dict.fromkeys(target_regions))
        lifecycle_id = _new_lifecycle_token()
        region_generations = {region: _new_lifecycle_token() for region in regions}
        item: dict[str, Any] = {
            "endpoint_name": endpoint_name,
            "lifecycle_id": lifecycle_id,
            "desired_state": "deploying",
            "target_regions": regions,
            # Append-only membership for this lifecycle. Region removal changes
            # target_regions but never this authoritative cleanup set.
            "cleanup_regions": list(regions),
            # Membership changes rotate only the affected Region's token. A
            # terminal acknowledgement from an earlier remove/re-add cycle can
            # therefore never suppress cleanup for the current membership.
            "region_generations": region_generations,
            "namespace": namespace,
            "spec": _serialize_for_dynamo(spec),
            "ingress_path": f"/inference/{endpoint_name}",
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

    def get_endpoint(
        self,
        endpoint_name: str,
        *,
        consistent_read: bool = False,
    ) -> dict[str, Any] | None:
        """Get an endpoint by name, optionally using a strong read."""
        response = self._table.get_item(
            Key={"endpoint_name": endpoint_name},
            ConsistentRead=consistent_read,
        )
        item = response.get("Item")
        return _deserialize_from_dynamo(item) if isinstance(item, dict) else None

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

    def ensure_lifecycle_metadata(self, endpoint: dict[str, Any]) -> dict[str, Any] | None:
        """Conditionally backfill immutable lifecycle metadata on a legacy record.

        The migration is derived from one strong snapshot and conditioned on
        its ``updated_at`` value. Existing lifecycle and Region tokens are
        preserved, while current targets, prior cleanup members, and regions
        with historical status are unioned into the authoritative cleanup set.
        A concurrent mutation wins and makes this call return ``None`` so the
        caller retries from a fresh snapshot instead of overwriting it.
        """
        endpoint_name = endpoint.get("endpoint_name")
        updated_at = endpoint.get("updated_at")
        if not isinstance(endpoint_name, str) or not endpoint_name:
            raise ValueError("Endpoint lifecycle migration requires an endpoint name")
        if not isinstance(updated_at, str) or not updated_at:
            raise ValueError(f"Endpoint '{endpoint_name}' has no conditional migration timestamp")

        raw_status = endpoint.get("region_status")
        status_regions = list(raw_status) if isinstance(raw_status, dict) else []
        regions = list(
            dict.fromkeys(
                region
                for source in (
                    endpoint.get("cleanup_regions"),
                    endpoint.get("target_regions"),
                    status_regions,
                )
                if isinstance(source, list)
                for region in source
                if isinstance(region, str) and region
            )
        )
        lifecycle_value = endpoint.get("lifecycle_id")
        lifecycle_id = (
            lifecycle_value
            if isinstance(lifecycle_value, str) and lifecycle_value
            else _new_lifecycle_token()
        )
        raw_generations = endpoint.get("region_generations")
        existing_generations = raw_generations if isinstance(raw_generations, dict) else {}
        region_generations = {
            region: (
                existing_generations[region]
                if isinstance(existing_generations.get(region), str)
                and existing_generations[region]
                else _new_lifecycle_token()
            )
            for region in regions
        }

        metadata_complete = (
            endpoint.get("lifecycle_id") == lifecycle_id
            and endpoint.get("cleanup_regions") == regions
            and endpoint.get("region_generations") == region_generations
        )
        if metadata_complete:
            return endpoint

        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression=(
                    "SET lifecycle_id = if_not_exists(lifecycle_id, :lifecycle_id), "
                    "cleanup_regions = :cleanup_regions, "
                    "region_generations = :region_generations, updated_at = :updated_at"
                ),
                ExpressionAttributeValues={
                    ":lifecycle_id": lifecycle_id,
                    ":cleanup_regions": regions,
                    ":region_generations": region_generations,
                    ":updated_at": _utc_now_iso(),
                    ":expected_updated_at": updated_at,
                },
                ConditionExpression=(
                    "attribute_exists(endpoint_name) AND updated_at = :expected_updated_at"
                ),
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    @staticmethod
    def _conditioned_identity(
        expected_label: tuple[str, str] | None,
        expected_lifecycle_id: str | None,
    ) -> tuple[str, dict[str, str], dict[str, Any]]:
        condition = "attribute_exists(endpoint_name)"
        names: dict[str, str] = {}
        values: dict[str, Any] = {}
        if expected_label is not None:
            label_name, label_value = expected_label
            if not label_name or not label_value:
                raise ValueError("Expected endpoint label name and value must be non-empty")
            condition += " AND labels.#expected_label = :expected_label_value"
            names["#expected_label"] = label_name
            values[":expected_label_value"] = label_value
        if expected_lifecycle_id is not None:
            if not expected_lifecycle_id:
                raise ValueError("Expected lifecycle id must be non-empty")
            condition += " AND lifecycle_id = :expected_lifecycle_id"
            values[":expected_lifecycle_id"] = expected_lifecycle_id
        return condition, names, values

    def update_desired_state(
        self,
        endpoint_name: str,
        desired_state: str,
        *,
        expected_label: tuple[str, str] | None = None,
        expected_lifecycle_id: str | None = None,
        expected_desired_state: str | None = None,
    ) -> dict[str, Any] | None:
        """Conditionally update desired state without reviving deletion.

        Ordinary transitions require an immutable lifecycle identity and may
        only mutate a non-deleted record. The first transition to ``deleted``
        atomically creates an immutable deletion generation and snapshots the
        lifecycle's append-only cleanup regions; repeated deletes retain both.
        """
        if desired_state != "deleted" and not expected_lifecycle_id:
            raise ValueError("Ordinary state updates require an expected lifecycle id")
        condition, names, identity_values = self._conditioned_identity(
            expected_label, expected_lifecycle_id
        )
        values: dict[str, Any] = {
            ":s": desired_state,
            ":u": _utc_now_iso(),
            **identity_values,
        }
        update_expression = "SET desired_state = :s, updated_at = :u"
        if desired_state == "deleted":
            values[":deletion_generation"] = _new_lifecycle_token()
            update_expression += (
                ", deletion_generation = if_not_exists("
                "deletion_generation, :deletion_generation), "
                "deletion_regions = if_not_exists(deletion_regions, cleanup_regions)"
            )
        else:
            condition += " AND desired_state <> :deleted"
            values[":deleted"] = "deleted"
        if expected_desired_state is not None:
            condition += " AND desired_state = :expected_desired_state"
            values[":expected_desired_state"] = expected_desired_state
        kwargs: dict[str, Any] = {}
        if names:
            kwargs["ExpressionAttributeNames"] = names
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression=update_expression,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
                ReturnValues="ALL_NEW",
                **kwargs,
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def start_endpoint(self, endpoint_name: str) -> dict[str, Any] | None:
        """Start only a stopped endpoint; deletion is terminal for this lifecycle."""
        current = self.get_endpoint(endpoint_name, consistent_read=True)
        if current is None:
            return None
        migrated = self.ensure_lifecycle_metadata(current)
        if migrated is None:
            return None
        current = migrated
        lifecycle_id = current.get("lifecycle_id")
        if not isinstance(lifecycle_id, str) or not lifecycle_id:
            raise ValueError(f"Endpoint '{endpoint_name}' has no lifecycle identity")
        state = current.get("desired_state")
        if state == "deleted":
            raise ValueError(
                f"Endpoint '{endpoint_name}' is deleted; wait for purge and redeploy it with "
                "'gco inference deploy'."
            )
        if state != "stopped":
            raise ValueError(
                f"Endpoint '{endpoint_name}' is in '{state}' state; only stopped endpoints "
                "can be started."
            )
        return self.update_desired_state(
            endpoint_name,
            "running",
            expected_lifecycle_id=lifecycle_id,
            expected_desired_state="stopped",
        )

    def update_spec(
        self,
        endpoint_name: str,
        spec: dict[str, Any],
        *,
        expected_lifecycle_id: str,
        expected_updated_at: str | None = None,
    ) -> dict[str, Any] | None:
        """Conditionally update a live lifecycle's spec and trigger reconciliation."""
        _validate_endpoint_spec(spec)
        if not expected_lifecycle_id:
            raise ValueError("Spec updates require an expected lifecycle id")
        condition = (
            "attribute_exists(endpoint_name) AND lifecycle_id = :expected_lifecycle_id "
            "AND desired_state <> :deleted"
        )
        values: dict[str, Any] = {
            ":s": _serialize_for_dynamo(spec),
            ":u": _utc_now_iso(),
            ":ds": "deploying",
            ":deleted": "deleted",
            ":expected_lifecycle_id": expected_lifecycle_id,
        }
        if expected_updated_at is not None:
            condition += " AND updated_at = :expected_updated_at"
            values[":expected_updated_at"] = expected_updated_at
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET spec = :s, updated_at = :u, desired_state = :ds",
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def update_target_regions(
        self,
        endpoint_name: str,
        target_regions: list[str],
        cleanup_regions: list[str],
        region_generations: dict[str, str],
        *,
        expected_lifecycle_id: str,
        expected_updated_at: str,
    ) -> dict[str, Any] | None:
        """Conditionally update membership without dropping cleanup authority."""
        normalized_targets = list(dict.fromkeys(target_regions))
        requested_cleanup = list(dict.fromkeys(cleanup_regions))
        current = self.get_endpoint(endpoint_name, consistent_read=True)
        if (
            current is None
            or current.get("lifecycle_id") != expected_lifecycle_id
            or current.get("updated_at") != expected_updated_at
            or current.get("desired_state") == "deleted"
        ):
            return None

        historical_cleanup = list(
            dict.fromkeys(current.get("cleanup_regions") or current.get("target_regions") or [])
        )
        normalized_cleanup = list(
            dict.fromkeys([*historical_cleanup, *requested_cleanup, *normalized_targets])
        )
        current_generations = current.get("region_generations")
        merged_generations = (
            dict(current_generations) if isinstance(current_generations, dict) else {}
        )
        merged_generations.update(region_generations)
        for region in normalized_cleanup:
            token = merged_generations.get(region)
            if not isinstance(token, str) or not token:
                merged_generations[region] = _new_lifecycle_token()
        merged_generations = {region: merged_generations[region] for region in normalized_cleanup}
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression=(
                    "SET target_regions = :targets, cleanup_regions = :cleanup, "
                    "region_generations = :region_generations, updated_at = :u"
                ),
                ExpressionAttributeValues={
                    ":targets": normalized_targets,
                    ":cleanup": normalized_cleanup,
                    ":region_generations": merged_generations,
                    ":u": _utc_now_iso(),
                    ":expected_lifecycle_id": expected_lifecycle_id,
                    ":expected_updated_at": expected_updated_at,
                    ":deleted": "deleted",
                },
                ConditionExpression=(
                    "attribute_exists(endpoint_name) "
                    "AND lifecycle_id = :expected_lifecycle_id "
                    "AND updated_at = :expected_updated_at "
                    "AND desired_state <> :deleted"
                ),
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
        *,
        expected_lifecycle_id: str | None = None,
        expected_region_generation: str | None = None,
        expected_deletion_generation: str | None = None,
    ) -> bool:
        """Conditionally write one regional observation without upsert risk."""
        status_value: dict[str, Any] = {
            "state": state,
            "replicas_ready": replicas_ready,
            "replicas_desired": replicas_desired,
            "last_sync": _utc_now_iso(),
        }
        if expected_lifecycle_id is not None:
            status_value["lifecycle_id"] = expected_lifecycle_id
        if expected_region_generation is not None:
            status_value["region_generation"] = expected_region_generation
        if expected_deletion_generation is not None:
            status_value["deletion_generation"] = expected_deletion_generation
        if error:
            status_value["error"] = error
        if extra:
            status_value.update(extra)

        condition = "attribute_exists(endpoint_name)"
        names: dict[str, str] = {"#r": region}
        values: dict[str, Any] = {":s": status_value, ":u": _utc_now_iso()}
        if expected_lifecycle_id is not None:
            condition += " AND lifecycle_id = :expected_lifecycle_id"
            values[":expected_lifecycle_id"] = expected_lifecycle_id
        if expected_region_generation is not None:
            condition += " AND region_generations.#r = :expected_region_generation"
            values[":expected_region_generation"] = expected_region_generation
        if expected_deletion_generation is not None:
            # Only this generation's own terminal acknowledgement is final. A
            # Region removed before the delete holds a terminal acknowledgement
            # of its removal (state "deleted", no deletion generation) and
            # still has to acknowledge the deletion, or the purge never runs.
            condition += (
                " AND desired_state = :deleted "
                "AND deletion_generation = :expected_deletion_generation "
                "AND (attribute_not_exists(region_status.#r.#state) "
                "OR region_status.#r.#state <> :terminal_deleted "
                "OR attribute_not_exists(region_status.#r.deletion_generation) "
                "OR region_status.#r.deletion_generation <> :expected_deletion_generation)"
            )
            names["#state"] = "state"
            values[":deleted"] = "deleted"
            values[":terminal_deleted"] = "deleted"
            values[":expected_deletion_generation"] = expected_deletion_generation
        else:
            # Ordinary observations are never allowed to overwrite a terminal
            # deletion record. Lifecycle/Region tokens fence replacement and
            # membership races; this state predicate closes the remaining
            # same-lifecycle window between the first delete transition and
            # the final generation-scoped cleanup acknowledgement.
            condition += " AND desired_state <> :deleted"
            values[":deleted"] = "deleted"
        try:
            self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET region_status.#r = :s, updated_at = :u",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
            )
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                logger.info(
                    "Skipped stale regional status for %s/%s after lifecycle change",
                    endpoint_name,
                    region,
                )
                return False
            logger.error(
                "Failed to update region status for %s/%s: %s",
                endpoint_name,
                region,
                e,
            )
            return False

    def delete_endpoint(
        self,
        endpoint_name: str,
        *,
        expected_updated_at: str | None = None,
        expected_lifecycle_id: str | None = None,
        expected_deletion_generation: str | None = None,
    ) -> bool:
        """Delete only the freshly verified endpoint deletion generation."""
        condition = "attribute_exists(endpoint_name)"
        values: dict[str, Any] = {}
        if expected_updated_at is not None:
            condition += " AND desired_state = :deleted AND updated_at = :expected_updated_at"
            values.update({":deleted": "deleted", ":expected_updated_at": expected_updated_at})
        if expected_lifecycle_id is not None:
            condition += " AND lifecycle_id = :expected_lifecycle_id"
            values[":expected_lifecycle_id"] = expected_lifecycle_id
        if expected_deletion_generation is not None:
            condition += " AND deletion_generation = :expected_deletion_generation"
            values[":expected_deletion_generation"] = expected_deletion_generation
        kwargs: dict[str, Any] = {
            "Key": {"endpoint_name": endpoint_name},
            "ConditionExpression": condition,
        }
        if values:
            kwargs["ExpressionAttributeValues"] = values
        try:
            self._table.delete_item(**kwargs)
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise

    def scale_endpoint(
        self,
        endpoint_name: str,
        replicas: int,
        *,
        expected_lifecycle_id: str,
    ) -> dict[str, Any] | None:
        """Update classic static replicas only on a live, non-Mooncake lifecycle."""
        if not expected_lifecycle_id:
            raise ValueError("Scaling requires an expected lifecycle id")
        condition = (
            "attribute_exists(endpoint_name) "
            "AND lifecycle_id = :expected_lifecycle_id "
            "AND desired_state <> :deleted "
            "AND attribute_not_exists(#spec.#mooncake) "
            "AND (attribute_not_exists(#spec.#autoscaling.#enabled) "
            "OR #spec.#autoscaling.#enabled = :false)"
        )
        values: dict[str, Any] = {
            ":r": replicas,
            ":u": _utc_now_iso(),
            ":false": False,
            ":deleted": "deleted",
            ":expected_lifecycle_id": expected_lifecycle_id,
        }
        try:
            response = self._table.update_item(
                Key={"endpoint_name": endpoint_name},
                UpdateExpression="SET #spec.replicas = :r, updated_at = :u",
                ExpressionAttributeNames={
                    "#spec": "spec",
                    "#autoscaling": "autoscaling",
                    "#enabled": "enabled",
                    "#mooncake": "mooncake",
                },
                ExpressionAttributeValues=values,
                ConditionExpression=condition,
                ReturnValues="ALL_NEW",
            )
            return _deserialize_from_dynamo(response.get("Attributes", {}))
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise


def _serialize_for_dynamo(obj: Any) -> Any:
    """Convert Python objects to DynamoDB-compatible types recursively."""
    if isinstance(obj, dict):
        return {k: _serialize_for_dynamo(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize_for_dynamo(i) for i in obj]
    if isinstance(obj, (int, float)):
        return str(obj) if isinstance(obj, float) else obj
    return obj


def _deserialize_from_dynamo(item: dict[str, Any]) -> dict[str, Any]:
    """Convert a DynamoDB item back to plain Python types recursively."""
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
