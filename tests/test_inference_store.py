"""
Tests for gco/services/inference_store.InferenceEndpointStore.

Covers the DynamoDB CRUD surface for inference endpoints: create_endpoint
happy path (with and without labels/created_by), duplicate detection via
ConditionalCheckFailedException surfacing as ValueError, propagation of
other ClientErrors, get_endpoint hit/miss, automatic Decimal→int
coercion on deserialization, and list_endpoints scan. Uses a boto3
resource patch so tests run against MagicMock tables instead of a
real DynamoDB.
"""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "test"}}, "op")


@pytest.fixture
def mock_table():
    table = MagicMock()
    with patch("boto3.resource") as mock_resource:
        mock_resource.return_value.Table.return_value = table
        yield table


@pytest.fixture
def store(mock_table):
    from gco.services.inference_store import InferenceEndpointStore

    return InferenceEndpointStore(table_name="test-table", region="us-east-1")


# ---- create_endpoint ----


class TestCreateEndpoint:
    def test_creates_item(self, store, mock_table):
        result = store.create_endpoint(
            endpoint_name="my-ep",
            spec={"image": "nginx", "replicas": 2},
            target_regions=["us-east-1"],
        )
        mock_table.put_item.assert_called_once()
        assert result["endpoint_name"] == "my-ep"
        assert result["desired_state"] == "deploying"
        assert result["ingress_path"] == "/inference/my-ep"
        assert len(result["lifecycle_id"]) == 64
        assert result["cleanup_regions"] == ["us-east-1"]
        assert set(result["region_generations"]) == {"us-east-1"}
        assert len(result["region_generations"]["us-east-1"]) == 64

    def test_with_labels_and_created_by(self, store, mock_table):
        result = store.create_endpoint(
            endpoint_name="ep2",
            spec={},
            target_regions=["us-west-2"],
            labels={"team": "ml"},
            created_by="alice",
        )
        assert result["labels"] == {"team": "ml"}
        assert result["created_by"] == "alice"

    def test_duplicate_raises_value_error(self, store, mock_table):
        mock_table.put_item.side_effect = _client_error("ConditionalCheckFailedException")
        with pytest.raises(ValueError, match="already exists"):
            store.create_endpoint("dup", spec={}, target_regions=["us-east-1"])

    def test_other_client_error_propagates(self, store, mock_table):
        mock_table.put_item.side_effect = _client_error("InternalServerError")
        with pytest.raises(ClientError):
            store.create_endpoint("ep", spec={}, target_regions=["us-east-1"])

    def test_rejects_mooncake_canary_before_put(self, store, mock_table):
        invalid_spec = {
            "image": "image:v1",
            "mooncake": {"mode": "store"},
            "canary": {"image": "image:v2", "weight": 10},
        }

        with pytest.raises(
            ValueError,
            match="Endpoint spec cannot combine 'mooncake' and 'canary' blocks",
        ):
            store.create_endpoint("invalid", invalid_spec, ["us-east-1"])

        mock_table.put_item.assert_not_called()


# ---- get_endpoint ----


class TestGetEndpoint:
    def test_returns_item(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {"endpoint_name": "ep1", "desired_state": "running"}
        }
        result = store.get_endpoint("ep1")
        assert result["endpoint_name"] == "ep1"

    def test_returns_none_when_missing(self, store, mock_table):
        mock_table.get_item.return_value = {}
        assert store.get_endpoint("nope") is None

    def test_deserializes_decimals(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {"endpoint_name": "ep", "spec": {"replicas": Decimal("3")}}
        }
        result = store.get_endpoint("ep")
        assert result["spec"]["replicas"] == 3
        assert isinstance(result["spec"]["replicas"], int)


# ---- list_endpoints ----


class TestListEndpoints:
    def test_returns_all(self, store, mock_table):
        mock_table.scan.return_value = {
            "Items": [
                {"endpoint_name": "a", "created_at": "2026-01-01"},
                {"endpoint_name": "b", "created_at": "2026-01-02"},
            ]
        }
        result = store.list_endpoints()
        assert len(result) == 2
        assert result[0]["endpoint_name"] == "b"  # sorted desc by created_at

    def test_filter_by_state(self, store, mock_table):
        mock_table.scan.return_value = {
            "Items": [
                {"endpoint_name": "a", "desired_state": "running", "created_at": "2026-01-01"},
                {"endpoint_name": "b", "desired_state": "deleting", "created_at": "2026-01-02"},
            ]
        }
        result = store.list_endpoints(desired_state="running")
        assert len(result) == 1
        assert result[0]["endpoint_name"] == "a"

    def test_filter_by_region(self, store, mock_table):
        mock_table.scan.return_value = {
            "Items": [
                {
                    "endpoint_name": "a",
                    "target_regions": ["us-east-1"],
                    "created_at": "2026-01-01",
                },
                {
                    "endpoint_name": "b",
                    "target_regions": ["eu-west-1"],
                    "created_at": "2026-01-02",
                },
            ]
        }
        result = store.list_endpoints(target_region="eu-west-1")
        assert len(result) == 1
        assert result[0]["endpoint_name"] == "b"


# ---- update_desired_state ----


class TestUpdateDesiredState:
    def test_updates_and_returns(self, store, mock_table):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "desired_state": "running"}
        }
        result = store.update_desired_state(
            "ep",
            "running",
            expected_lifecycle_id="life-1",
            expected_desired_state="deploying",
        )
        assert result["desired_state"] == "running"
        kwargs = mock_table.update_item.call_args.kwargs
        assert "lifecycle_id = :expected_lifecycle_id" in kwargs["ConditionExpression"]
        assert "desired_state <> :deleted" in kwargs["ConditionExpression"]
        assert "desired_state = :expected_desired_state" in kwargs["ConditionExpression"]

    def test_returns_none_when_condition_fails(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.update_desired_state("nope", "running", expected_lifecycle_id="life-1") is None

    def test_ordinary_state_requires_lifecycle_identity(self, store, mock_table):
        with pytest.raises(ValueError, match="lifecycle id"):
            store.update_desired_state("ep", "stopped")
        mock_table.update_item.assert_not_called()


# ---- update_spec ----


class TestUpdateSpec:
    def test_updates_spec_and_resets_state(self, store, mock_table):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "desired_state": "deploying"}
        }
        result = store.update_spec(
            "ep",
            {"image": "new:v2"},
            expected_lifecycle_id="life-1",
            expected_updated_at="snapshot",
        )
        assert result["desired_state"] == "deploying"
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":ds"] == "deploying"
        assert "lifecycle_id = :expected_lifecycle_id" in call_kwargs["ConditionExpression"]
        assert "desired_state <> :deleted" in call_kwargs["ConditionExpression"]
        assert "updated_at = :expected_updated_at" in call_kwargs["ConditionExpression"]

    def test_returns_none_when_condition_fails(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.update_spec("nope", {}, expected_lifecycle_id="life-1") is None

    def test_rejects_mooncake_canary_before_update(self, store, mock_table):
        invalid_spec = {
            "image": "image:v1",
            "mooncake": {"mode": "both"},
            "canary": {"image": "image:v2", "weight": 25},
        }

        with pytest.raises(
            ValueError,
            match="Endpoint spec cannot combine 'mooncake' and 'canary' blocks",
        ):
            store.update_spec("invalid", invalid_spec, expected_lifecycle_id="life-1")

        mock_table.update_item.assert_not_called()


# ---- delete_endpoint ----


class TestDeleteEndpoint:
    def test_returns_true_on_success(self, store, mock_table):
        assert store.delete_endpoint("ep") is True
        mock_table.delete_item.assert_called_once()

    def test_guards_verified_cleanup_snapshot(self, store, mock_table):
        assert (
            store.delete_endpoint(
                "ep",
                expected_updated_at="2026-01-03T00:00:00+00:00",
            )
            is True
        )
        mock_table.delete_item.assert_called_once_with(
            Key={"endpoint_name": "ep"},
            ConditionExpression=(
                "attribute_exists(endpoint_name) "
                "AND desired_state = :deleted "
                "AND updated_at = :expected_updated_at"
            ),
            ExpressionAttributeValues={
                ":deleted": "deleted",
                ":expected_updated_at": "2026-01-03T00:00:00+00:00",
            },
        )

    def test_returns_false_when_not_found(self, store, mock_table):
        mock_table.delete_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.delete_endpoint("nope") is False


# ---- scale_endpoint ----


class TestScaleEndpoint:
    def test_updates_replicas_with_defensive_guards(self, store, mock_table):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "spec": {"replicas": Decimal("5")}}
        }
        result = store.scale_endpoint("ep", 5, expected_lifecycle_id="life-1")
        assert result["spec"]["replicas"] == 5
        kwargs = mock_table.update_item.call_args.kwargs
        assert "lifecycle_id = :expected_lifecycle_id" in kwargs["ConditionExpression"]
        assert "desired_state <> :deleted" in kwargs["ConditionExpression"]
        assert "attribute_not_exists(#spec.#mooncake)" in kwargs["ConditionExpression"]
        assert kwargs["ExpressionAttributeNames"]["#mooncake"] == "mooncake"

    def test_returns_none_when_condition_fails(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.scale_endpoint("nope", 3, expected_lifecycle_id="life-1") is None


# ---- update_region_status ----


class TestUpdateRegionStatus:
    def test_updates_status(self, store, mock_table):
        store.update_region_status(
            "ep", "us-east-1", "synced", replicas_ready=2, replicas_desired=2
        )
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeNames"]["#r"] == "us-east-1"

    def test_includes_error_when_provided(self, store, mock_table):
        store.update_region_status("ep", "us-east-1", "error", error="OOM")
        call_kwargs = mock_table.update_item.call_args[1]
        status = call_kwargs["ExpressionAttributeValues"][":s"]
        assert status["error"] == "OOM"

    def test_logs_on_failure(self, store, mock_table, caplog):
        mock_table.update_item.side_effect = _client_error("InternalServerError")
        import logging

        with caplog.at_level(logging.ERROR):
            store.update_region_status("ep", "us-east-1", "error")
        assert "Failed to update region status" in caplog.text


# ---- serialization helpers ----


class TestSerialization:
    def test_serialize_converts_floats(self):
        from gco.services.inference_store import _serialize_for_dynamo

        result = _serialize_for_dynamo({"rate": 0.5, "count": 3})
        assert result["rate"] == "0.5"
        assert result["count"] == 3

    def test_serialize_nested(self):
        from gco.services.inference_store import _serialize_for_dynamo

        result = _serialize_for_dynamo({"a": {"b": [1.5, 2]}})
        assert result["a"]["b"] == ["1.5", 2]

    def test_deserialize_decimals(self):
        from gco.services.inference_store import _deserialize_from_dynamo

        result = _deserialize_from_dynamo(
            {"count": Decimal("3"), "rate": Decimal("0.5"), "nested": {"x": Decimal("10")}}
        )
        assert result["count"] == 3
        assert isinstance(result["count"], int)
        assert result["rate"] == 0.5
        assert isinstance(result["rate"], float)
        assert result["nested"]["x"] == 10


# ---- factory ----


class TestFactory:
    def test_get_inference_endpoint_store(self):
        with patch("boto3.resource"):
            from gco.services.inference_store import get_inference_endpoint_store

            store = get_inference_endpoint_store()
            assert store.table_name == "gco-inference-endpoints"


class TestLifecycleMetadata:
    def test_legacy_backfill_unions_targets_cleanup_and_historical_status(self, store, mock_table):
        endpoint = {
            "endpoint_name": "legacy",
            "updated_at": "before",
            "target_regions": ["us-east-1"],
            "cleanup_regions": ["eu-west-1"],
            "region_status": {"ap-southeast-1": {"state": "running"}},
        }
        mock_table.update_item.return_value = {
            "Attributes": {
                **endpoint,
                "lifecycle_id": "life",
                "cleanup_regions": ["eu-west-1", "us-east-1", "ap-southeast-1"],
                "region_generations": {
                    "eu-west-1": "one",
                    "us-east-1": "two",
                    "ap-southeast-1": "three",
                },
            }
        }

        result = store.ensure_lifecycle_metadata(endpoint)

        assert result and result["lifecycle_id"] == "life"
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["ConditionExpression"] == (
            "attribute_exists(endpoint_name) AND updated_at = :expected_updated_at"
        )
        assert kwargs["ExpressionAttributeValues"][":cleanup_regions"] == [
            "eu-west-1",
            "us-east-1",
            "ap-southeast-1",
        ]
        assert set(kwargs["ExpressionAttributeValues"][":region_generations"]) == {
            "eu-west-1",
            "us-east-1",
            "ap-southeast-1",
        }

    def test_complete_metadata_is_a_zero_write_noop(self, store, mock_table):
        endpoint = {
            "endpoint_name": "current",
            "updated_at": "now",
            "lifecycle_id": "life",
            "target_regions": ["us-east-1"],
            "cleanup_regions": ["us-east-1"],
            "region_generations": {"us-east-1": "region-1"},
            "region_status": {},
        }

        assert store.ensure_lifecycle_metadata(endpoint) is endpoint
        mock_table.update_item.assert_not_called()

    def test_target_update_persists_complete_region_generation_map(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "endpoint_name": "ep",
                "desired_state": "running",
                "lifecycle_id": "life",
                "updated_at": "before",
                "target_regions": ["us-east-1"],
                "cleanup_regions": ["us-east-1", "eu-west-1"],
                "region_generations": {
                    "us-east-1": "old-east",
                    "eu-west-1": "old-west",
                },
            }
        }
        mock_table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}

        store.update_target_regions(
            "ep",
            ["us-east-1"],
            ["us-east-1", "eu-west-1"],
            {"us-east-1": "east-generation", "eu-west-1": "west-generation"},
            expected_lifecycle_id="life",
            expected_updated_at="before",
        )

        values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":region_generations"] == {
            "us-east-1": "east-generation",
            "eu-west-1": "west-generation",
        }

    def test_target_update_preserves_omitted_historical_cleanup_regions(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "endpoint_name": "ep",
                "desired_state": "running",
                "lifecycle_id": "life",
                "updated_at": "before",
                "target_regions": ["us-east-1"],
                "cleanup_regions": ["us-east-1", "eu-west-1"],
                "region_generations": {
                    "us-east-1": "east-generation",
                    "eu-west-1": "historical-generation",
                },
            }
        }
        mock_table.update_item.return_value = {"Attributes": {"endpoint_name": "ep"}}

        store.update_target_regions(
            "ep",
            ["us-east-1", "ap-southeast-1"],
            ["us-east-1", "ap-southeast-1"],
            {
                "us-east-1": "east-generation",
                "ap-southeast-1": "new-generation",
            },
            expected_lifecycle_id="life",
            expected_updated_at="before",
        )

        values = mock_table.update_item.call_args.kwargs["ExpressionAttributeValues"]
        assert values[":cleanup"] == ["us-east-1", "eu-west-1", "ap-southeast-1"]
        assert values[":region_generations"]["eu-west-1"] == "historical-generation"

    def test_region_status_is_conditioned_on_membership_generation(self, store, mock_table):
        assert store.update_region_status(
            "ep",
            "us-east-1",
            "running",
            expected_lifecycle_id="life",
            expected_region_generation="region-generation",
        )

        kwargs = mock_table.update_item.call_args.kwargs
        assert "attribute_exists(endpoint_name)" in kwargs["ConditionExpression"]
        assert "lifecycle_id = :expected_lifecycle_id" in kwargs["ConditionExpression"]
        assert (
            "region_generations.#r = :expected_region_generation" in kwargs["ConditionExpression"]
        )
        assert "desired_state <> :deleted" in kwargs["ConditionExpression"]
        status = kwargs["ExpressionAttributeValues"][":s"]
        assert status["lifecycle_id"] == "life"
        assert status["region_generation"] == "region-generation"


class _ConditionalInterleavingTable:
    """Minimal stateful evaluator for status-write/purge race boundaries."""

    def __init__(self, item):
        self.item = item

    def update_item(self, **kwargs):
        values = kwargs["ExpressionAttributeValues"]
        item = self.item
        valid = item is not None
        if valid and ":expected_lifecycle_id" in values:
            valid = item.get("lifecycle_id") == values[":expected_lifecycle_id"]
        if valid and ":expected_region_generation" in values:
            region = kwargs["ExpressionAttributeNames"]["#r"]
            valid = (
                item.get("region_generations", {}).get(region)
                == values[":expected_region_generation"]
            )
        if valid and ":expected_deletion_generation" in values:
            region = kwargs["ExpressionAttributeNames"]["#r"]
            valid = (
                item.get("desired_state") == "deleted"
                and item.get("deletion_generation") == values[":expected_deletion_generation"]
                and item.get("region_status", {}).get(region, {}).get("state") != "deleted"
            )
        elif valid and ":deleted" in values:
            valid = item.get("desired_state") != values[":deleted"]
        if not valid:
            raise _client_error("ConditionalCheckFailedException")
        region = kwargs["ExpressionAttributeNames"]["#r"]
        item.setdefault("region_status", {})[region] = values[":s"]
        item["updated_at"] = values[":u"]
        return {}

    def delete_item(self, **kwargs):
        values = kwargs.get("ExpressionAttributeValues", {})
        item = self.item
        valid = item is not None
        checks = {
            ":expected_updated_at": "updated_at",
            ":expected_lifecycle_id": "lifecycle_id",
            ":expected_deletion_generation": "deletion_generation",
        }
        for value_key, item_key in checks.items():
            if value_key in values:
                valid = valid and item.get(item_key) == values[value_key]
        if ":deleted" in values:
            valid = valid and item.get("desired_state") == values[":deleted"]
        if not valid:
            raise _client_error("ConditionalCheckFailedException")
        self.item = None
        return {}


def _stateful_store(item):
    from gco.services.inference_store import InferenceEndpointStore

    store = object.__new__(InferenceEndpointStore)
    store._table = _ConditionalInterleavingTable(item)
    return store


class TestCrossMonitorInterleavings:
    def test_purge_first_rejects_late_status_without_upserting(self):
        store = _stateful_store(
            {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "snapshot",
                "region_status": {},
            }
        )

        assert store.delete_endpoint(
            "ep",
            expected_updated_at="snapshot",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        assert not store.update_region_status(
            "ep",
            "eu-west-1",
            "deleting",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        assert store._table.item is None

    def test_status_first_invalidates_stale_purge_snapshot(self):
        store = _stateful_store(
            {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "snapshot",
                "region_status": {},
            }
        )

        assert store.update_region_status(
            "ep",
            "eu-west-1",
            "deleting",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        assert not store.delete_endpoint(
            "ep",
            expected_updated_at="snapshot",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        assert store._table.item is not None

    def test_terminal_deletion_ack_rejects_same_generation_error_regression(self):
        store = _stateful_store(
            {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "terminal",
                "region_status": {
                    "eu-west-1": {
                        "state": "deleted",
                        "lifecycle_id": "life-1",
                        "deletion_generation": "delete-1",
                        "absence_observations": 2,
                    }
                },
            }
        )

        assert not store.update_region_status(
            "ep",
            "eu-west-1",
            "error",
            error="stale leader lost authority",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        assert store._table.item["updated_at"] == "terminal"
        assert store._table.item["region_status"]["eu-west-1"]["state"] == "deleted"

    def test_terminal_transition_rejects_same_lifecycle_ordinary_status(self):
        store = _stateful_store(
            {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "region_generations": {"eu-west-1": "region-1"},
                "updated_at": "terminal",
                "region_status": {"eu-west-1": {"state": "deleting"}},
            }
        )

        assert not store.update_region_status(
            "ep",
            "eu-west-1",
            "running",
            expected_lifecycle_id="life-1",
            expected_region_generation="region-1",
        )
        assert store._table.item["region_status"]["eu-west-1"] == {"state": "deleting"}

    def test_old_monitor_cannot_mutate_same_name_replacement(self):
        store = _stateful_store(
            {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "life-1",
                "deletion_generation": "delete-1",
                "updated_at": "snapshot",
                "region_status": {},
            }
        )
        assert store.delete_endpoint(
            "ep",
            expected_updated_at="snapshot",
            expected_lifecycle_id="life-1",
            expected_deletion_generation="delete-1",
        )
        store._table.item = {
            "endpoint_name": "ep",
            "desired_state": "running",
            "lifecycle_id": "life-2",
            "region_generations": {"eu-west-1": "region-2"},
            "updated_at": "replacement",
            "region_status": {},
        }

        assert not store.update_region_status(
            "ep",
            "eu-west-1",
            "running",
            expected_lifecycle_id="life-1",
            expected_region_generation="region-1",
        )
        assert store._table.item["region_status"] == {}


class TestStartLifecycleRotation:
    def test_deleted_start_is_terminal_and_preserves_cleanup_authority(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "endpoint_name": "ep",
                "desired_state": "deleted",
                "lifecycle_id": "old-life",
                "target_regions": ["us-east-1", "eu-west-1"],
                "cleanup_regions": ["us-east-1", "eu-west-1", "ap-southeast-1"],
                "deletion_regions": ["us-east-1", "eu-west-1", "ap-southeast-1"],
                "deletion_generation": "delete-generation",
                "region_generations": {
                    "us-east-1": "old-east",
                    "eu-west-1": "old-west",
                    "ap-southeast-1": "old-history",
                },
                "updated_at": "before",
            }
        }

        with pytest.raises(ValueError, match=r"deleted.*redeploy"):
            store.start_endpoint("ep")

        mock_table.update_item.assert_not_called()

    def test_stopped_start_requires_same_lifecycle_and_source_state(self, store, mock_table):
        mock_table.get_item.return_value = {
            "Item": {
                "endpoint_name": "ep",
                "desired_state": "stopped",
                "lifecycle_id": "life-1",
                "target_regions": ["us-east-1"],
                "cleanup_regions": ["us-east-1", "eu-west-1"],
                "region_generations": {
                    "us-east-1": "east",
                    "eu-west-1": "history",
                },
                "updated_at": "before",
            }
        }
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "desired_state": "running"}
        }

        result = store.start_endpoint("ep")

        assert result and result["desired_state"] == "running"
        kwargs = mock_table.update_item.call_args.kwargs
        assert kwargs["ExpressionAttributeValues"][":expected_lifecycle_id"] == "life-1"
        assert kwargs["ExpressionAttributeValues"][":expected_desired_state"] == "stopped"
        assert "desired_state <> :deleted" in kwargs["ConditionExpression"]

    def test_repeated_delete_uses_if_not_exists_for_generation_and_membership(
        self, store, mock_table
    ):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "desired_state": "deleted"}
        }

        assert store.update_desired_state("ep", "deleted", expected_lifecycle_id="life")
        expression = mock_table.update_item.call_args.kwargs["UpdateExpression"]
        assert "deletion_generation = if_not_exists(" in expression
        assert "deletion_regions = if_not_exists(deletion_regions, cleanup_regions)" in expression
