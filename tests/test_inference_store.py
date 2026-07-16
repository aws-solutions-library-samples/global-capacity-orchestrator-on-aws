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
        result = store.update_desired_state("ep", "running")
        assert result["desired_state"] == "running"

    def test_returns_none_when_not_found(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.update_desired_state("nope", "running") is None


# ---- update_spec ----


class TestUpdateSpec:
    def test_updates_spec_and_resets_state(self, store, mock_table):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "desired_state": "deploying"}
        }
        result = store.update_spec("ep", {"image": "new:v2"})
        assert result["desired_state"] == "deploying"
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":ds"] == "deploying"

    def test_returns_none_when_not_found(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.update_spec("nope", {}) is None

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
            store.update_spec("invalid", invalid_spec)

        mock_table.update_item.assert_not_called()


# ---- delete_endpoint ----


class TestDeleteEndpoint:
    def test_returns_true_on_success(self, store, mock_table):
        assert store.delete_endpoint("ep") is True
        mock_table.delete_item.assert_called_once()

    def test_returns_false_when_not_found(self, store, mock_table):
        mock_table.delete_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.delete_endpoint("nope") is False


# ---- scale_endpoint ----


class TestScaleEndpoint:
    def test_updates_replicas(self, store, mock_table):
        mock_table.update_item.return_value = {
            "Attributes": {"endpoint_name": "ep", "spec": {"replicas": Decimal("5")}}
        }
        result = store.scale_endpoint("ep", 5)
        assert result["spec"]["replicas"] == 5

    def test_returns_none_when_not_found(self, store, mock_table):
        mock_table.update_item.side_effect = _client_error("ConditionalCheckFailedException")
        assert store.scale_endpoint("nope", 3) is None


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
